"""Single live pipeline for real-code coordinator/worker scenario evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import blind_runtime, escalation_controller, hook_broker
from .client import HarnessTimeoutError
from .coordination import analyze_coordination
from .escalation_resumption import analyze_escalation_resumption
from .evidence import (
    collect_evidence,
    paginate,
    retain_native_runtime,
    retain_tree,
    write_bundle,
)
from .inventory import (
    PluginSnapshot,
    blueprint_config,
    file_sha256,
    snapshot_blueprints,
    snapshot_plugins,
    traced_plugin_config,
)
from .judge import review_evidence, write_review_markdown
from .judge_projection import build_judge_projection
from .llm_backend import UsageTracker, record_native_session_usage
from .redaction import Redactor, write_bytes, write_json, write_text
from .runtime import (
    BlindHarnessRuntime,
    HarnessRuntime,
    blind_preflight,
    launch_blind_harness,
    launch_harness,
    runtime_environment,
)
from .scenarios import (
    Scenario,
    load_scenarios,
    materialize_scenario,
    snapshot_tree,
    source_diff,
    tree_digest,
    verify_fixture_unchanged,
)
from .settings import (
    KEY_ENV,
    ProviderSettings,
    ScenarioSettings,
    profile_config,
)
from .tool_health import analyze_tool_health
from .user_actor import next_user_reply

_JSON = dict[str, Any]
MetricAnalyzer = Callable[..., tuple[_JSON, list[_JSON]]]

# The live scenario runtime stays on the host by default. Only the
# escalation-resumption scenario opts into the blind boundary by default:
# its escalation controller always runs in blind mode. Every other
# scenario keeps the plain host-visible runtime even when bwrap is
# installed. ``runtime_isolation`` is an explicit test/internal override
# of these scenario defaults, never a live configuration option; preflight
# failures still never fall back to a host launch.
DEFAULT_RUNTIME_ISOLATION = "host"
BLIND_DEFAULT_SCENARIOS = (escalation_controller.SCENARIO_ID,)
RUNTIME_ISOLATIONS = ("blind", "host")


def validate_runtime_isolation(runtime_isolation: str | None) -> str | None:
    """Validate an explicit runtime isolation override (test/internal only)."""
    if runtime_isolation is None:
        return None
    isolation = runtime_isolation.strip().lower()
    if isolation not in RUNTIME_ISOLATIONS:
        raise ValueError(
            "runtime_isolation must be one of " + ", ".join(RUNTIME_ISOLATIONS)
        )
    return isolation


def resolve_runtime_isolation(
    runtime_isolation: str | None, scenario_id: str | None = None
) -> str:
    """Resolve and validate the effective runtime isolation for one scenario.

    An explicit ``runtime_isolation`` is a test/internal override: when
    given it wins after validation. Otherwise only the escalation-resumption
    scenario defaults to the blind bwrap boundary (its controller mode is
    blind); every other scenario keeps the host default. Blind stays
    fail-closed: preflight failures abort instead of degrading to host.
    """
    explicit = validate_runtime_isolation(runtime_isolation)
    if explicit is not None:
        return explicit
    if scenario_id in BLIND_DEFAULT_SCENARIOS:
        return "blind"
    return DEFAULT_RUNTIME_ISOLATION


def _blind_hook_config(plugins: list[PluginSnapshot], proxy: Path) -> str:
    """Hook config lines routing every hook through the public proxy.

    Inside the sandbox the only broker-facing artifact is the stateless
    public proxy; the actual private RTK/controller/guard wrappers run on the
    host through the broker and never appear in the sandbox.
    """
    lines = ["\n[hooks]"]
    for plugin in plugins:
        lines.append(
            f"{plugin.hook} = [{json.dumps(shlex.join([str(proxy), plugin.id]))}]"
        )
        if plugin.hook == "pre_tool_transform":
            lines.append("pre_tool_transform_timeout_ms = 2000")
        elif plugin.hook == "post_tool_transform":
            lines.append("post_tool_transform_timeout_ms = 2000")
    return "\n".join(lines) + "\n"


def _blind_broker_operations(
    hook_commands: list[_JSON],
    guard_environment: dict[str, str],
    controller_environment: dict[str, str],
) -> dict[str, hook_broker.BrokerOperation]:
    """Map each traced hook wrapper to a private host broker operation.

    The traced wrappers, their trace directories, and the guard/controller
    environment stay host-side: the sandbox request can only name the
    operation, never a command, path, or private variable. Composite
    controller hooks keep single-writer semantics through the shared
    serialized lock.
    """
    operations: dict[str, hook_broker.BrokerOperation] = {}
    for record in hook_commands:
        env: dict[str, str] = {}
        serialized = False
        if record.get("composite"):
            env.update(controller_environment)
            env.update(guard_environment)
            serialized = True
        elif record["plugin"] == "delegation-guard":
            env.update(guard_environment)
            serialized = True
        operations[record["plugin"]] = hook_broker.BrokerOperation(
            name=record["plugin"],
            command=tuple(record["traced_command"]),
            env=env,
            serialized=serialized,
        )
    return operations


METRIC_ANALYZERS: dict[str, MetricAnalyzer] = {
    "tool_health": analyze_tool_health,
    "coordination": analyze_coordination,
    "escalation_resumption": analyze_escalation_resumption,
}
_METRIC_CLEAN_STATUSES = {"clean", "healthy", "pass"}


def _evaluator_identity() -> _JSON:
    package = Path(__file__).resolve().parent
    root = package.parent
    paths = sorted(package.glob("*.py"))
    paths += [root / "pyproject.toml", root / "uv.lock"]
    files = {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in paths
        if path.is_file()
    }
    return {
        "files": files,
        "sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _acceptance_environment(root: Path, scenario_id: str) -> dict[str, str]:
    target = root / "cargo-target" / scenario_id
    target.mkdir(parents=True, exist_ok=True)
    environment = runtime_environment(
        root / "acceptance" / scenario_id,
        {
            "CARGO_TARGET_DIR": str(target),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    for key in (
        "HOME",
        "JCODE_HOME",
        "JCODE_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    ):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    return environment


def run_acceptance(
    scenario: Scenario,
    workspace: Path,
    output: Path,
    environment_root: Path,
    timeout: int,
    redactor: Redactor,
) -> _JSON:
    """Run acceptance independently, even when the coordinator turn failed."""

    result: _JSON = {
        "citation": f"scenario:{scenario.id}:acceptance",
        "status": "not run",
        "argv": list(scenario.accept),
        "cwd": str(workspace),
        "exit_status": None,
        "timed_out": False,
        "timeout_seconds": timeout,
        "duration_seconds": 0.0,
    }
    if not workspace.is_dir():
        result["error"] = "materialized workspace is unavailable"
        write_json(output / "acceptance.json", result, redactor)
        return result
    started = time.monotonic()
    stdout = b""
    stderr = b""
    try:
        process = subprocess.Popen(
            list(scenario.accept),
            cwd=workspace,
            env=_acceptance_environment(environment_root, scenario.id),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            result["exit_status"] = process.returncode
            result["status"] = "pass" if process.returncode == 0 else "fail"
        except subprocess.TimeoutExpired as error:
            result["timed_out"] = True
            result["status"] = "error"
            result["error"] = "acceptance command exceeded its timeout"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            remaining_out, remaining_err = process.communicate()
            stdout = (error.output or b"") + (remaining_out or b"")
            stderr = (error.stderr or b"") + (remaining_err or b"")
            result["exit_status"] = process.returncode
    except OSError as error:
        result["status"] = "error"
        result["error"] = f"acceptance command could not start: {error.strerror}"
    result["duration_seconds"] = time.monotonic() - started
    write_bytes(output / "raw" / "logs" / "acceptance.stdout.bin", stdout, redactor)
    write_bytes(output / "raw" / "logs" / "acceptance.stderr.bin", stderr, redactor)
    write_text(
        output / "acceptance.log",
        "\n".join(
            [
                json.dumps(redactor.value(result), ensure_ascii=False),
                "----- stdout -----",
                redactor.data(stdout).decode("utf-8", errors="replace"),
                "----- stderr -----",
                redactor.data(stderr).decode("utf-8", errors="replace"),
            ]
        ),
    )
    result["stdout"] = redactor.data(stdout).decode("utf-8", errors="replace")
    result["stderr"] = redactor.data(stderr).decode("utf-8", errors="replace")
    write_json(output / "acceptance.json", result, redactor)
    return result


def _list_sessions(client: Any, phase: str, gaps: list[str]) -> list[_JSON]:
    try:
        return [dict(item.raw) for item in client.list_sessions()]
    except Exception as error:
        gaps.append(f"{phase} ListSessions failed: {error}")
        return []


def _list_tools(
    client: Any,
    session_id: str,
    phase: str,
    gaps: list[str],
) -> _JSON | None:
    try:
        return {
            "session_id": session_id,
            "phase": phase,
            "tools": [dict(item.raw) for item in client.list_tools(session_id)],
        }
    except Exception as error:
        gaps.append(f"{phase} ListTools failed for {session_id}: {error}")
        return None


def _incomplete_review(
    output: Path, reason: str, redactor: Redactor
) -> tuple[_JSON, _JSON]:
    review = {
        "summary": f"Diagnostic review incomplete: {reason}",
        "findings": [],
        "review": "incomplete",
        "review_complete": False,
        "reviewed_sessions": [],
        "unexercised_workers": [],
    }
    coverage = {
        "complete": False,
        "evidence_complete": False,
        "evidence_gaps": [reason],
        "raw_record_count": 0,
        "record_count": 0,
        "excluded_by_default": [],
        "retrieved_citations": [],
        "pages": [],
        "synthesis": "not-run-no-native-session",
        "judge_calls": 0,
    }
    write_json(output / "review.json", review, redactor)
    write_json(output / "judge-coverage.json", coverage, redactor)
    return review, coverage


def _worker_coverage(blueprints: list[_JSON], sessions: list[_JSON]) -> _JSON:
    exercised = sorted(
        {
            str(session["worker"])
            for session in sessions
            if session.get("worker") not in {None, "coordinator", "unknown"}
        }
    )
    available = sorted(
        str(blueprint["name"])
        for blueprint in blueprints
        if blueprint["name"] != "coordinator"
    )
    unknown = sorted(
        {
            str(session["session_id"])
            for session in sessions
            if session.get("worker") == "unknown"
            and (
                session.get("parent_session_id") is not None
                or session.get("parent_id") is not None
            )
        }
    )
    return {
        "available": available,
        "sessions": [
            {
                "session_id": session["session_id"],
                "worker": session["worker"],
                "parent_session_id": session.get("parent_session_id"),
            }
            for session in sessions
        ],
        "exercised": exercised,
        "not_exercised": [name for name in available if name not in exercised],
        "unidentified_sessions": unknown,
    }


def _run_scenario(
    *,
    scenario: Scenario,
    binary: Path,
    bridge: Path,
    settings: ProviderSettings,
    settings_manifest: _JSON,
    output: Path,
    blueprints: list[_JSON],
    blueprint_issues: list[str],
    blueprint_dir: Path,
    plugins: list[PluginSnapshot],
    temporary_root: Path,
    redactor: Redactor,
    run_tracker: UsageTracker,
    runtime_isolation: str = "host",
    staging_roots: list[Path] | None = None,
) -> _JSON:
    started = time.monotonic()
    blind = runtime_isolation == "blind"
    timeouts = settings.timeouts()
    output.mkdir(parents=True)
    scenario_tracker = UsageTracker()
    workspace = temporary_root / "workspaces" / scenario.id
    fixture_digest = tree_digest(scenario.fixture)
    pristine = materialize_scenario(scenario, workspace)
    pristine_workspace = temporary_root / "pristine" / scenario.id
    pristine_copy = materialize_scenario(scenario, pristine_workspace)
    if pristine_copy != pristine:
        raise RuntimeError("pristine fixture copies differ before evaluation")
    # Blind staging happens up front: the public workspace copy replaces the
    # private host workspace as the agent's live tree, while the fixture, the
    # pristine copy, acceptance, and all instrumentation stay on the host.
    staging: blind_runtime.StagingPlan | None = None
    hook_proxy: Path | None = None
    if blind:
        base = blind_runtime.public_staging_base()
        staging_root = Path(
            tempfile.mkdtemp(prefix=f"jcode-blind-{scenario.id}-", dir=str(base))
        )
        # Registered so the caller's finally can remove the public root even
        # on a hard failure that bypasses the normal post-acceptance removal.
        if staging_roots is not None:
            staging_roots.append(staging_root)
        staging = blind_runtime.plan_staging(
            staging_root, jcode_binary=binary, bridge=bridge
        )
        hook_proxy = hook_broker.install_public_proxy(staging.bin / "hook-proxy")
    instrumentation = temporary_root / "instrumentation" / scenario.id
    trace_root = instrumentation / "hook-traces"
    guard_snapshot = next(
        (plugin for plugin in plugins if plugin.id == "delegation-guard"), None
    )
    # Scenario-local composite wiring: the controller runs first inside one
    # hook command and the actual installed guard runs as its subprocess.
    # The native runtime applies the first usable replacement and shows every
    # declared command the original envelope, so two native entries cannot
    # chain; the guard entry is replaced, never duplicated.
    composite_controller: Path | None = None
    controller_environment: dict[str, str] = {}
    controller_provenance: _JSON | None = None
    if scenario.id == escalation_controller.SCENARIO_ID:
        if guard_snapshot is None:
            raise ValueError(
                "scenario escalation-resumption requires the delegation-guard"
                " plugin: the controller composes with the actual guard"
            )
        composite_controller = Path(escalation_controller.__file__).resolve()
        # In blind mode the agent edits the staged public workspace copy, so
        # the host-side controller must watch that tree, not the private one.
        controller_workspace = staging.workspace if staging is not None else workspace
        controller_environment = {
            escalation_controller.STATE_ENV: str(instrumentation),
            escalation_controller.WORKSPACE_ENV: str(controller_workspace),
            escalation_controller.GUARD_ENV: str(guard_snapshot.executable),
            escalation_controller.GUARD_TIMEOUT_ENV: "1000",
            # The controller mode is always set explicitly, never inherited
            # from the caller's environment: the manifest and the effective
            # provenance below record the effective blind mode of the run.
            escalation_controller.MODE_ENV: escalation_controller.MODE_BLIND,
        }
        controller_provenance = {
            "active": True,
            "scenario": scenario.id,
            "mode": escalation_controller.MODE_BLIND,
            "controller_module": str(composite_controller),
            "controller_sha256": file_sha256(composite_controller),
            "state_dir": str(instrumentation),
            "events_path": str(instrumentation / "events.jsonl"),
            "guard_events_path": str(
                instrumentation / escalation_controller.GUARD_EVENTS_NAME
            ),
            "workspace": str(controller_workspace),
            "guard_executable": str(guard_snapshot.executable),
            "guard_sha256": file_sha256(guard_snapshot.executable),
            "threshold": settings.delegation_guard.threshold,
            "guard_timeout_ms": 1000,
            "environment": dict(controller_environment),
        }
    hook_config, hook_commands = traced_plugin_config(
        plugins,
        instrumentation / "wrappers",
        trace_root,
        composite_controller=composite_controller,
    )
    guard_installed = any(plugin.id == "delegation-guard" for plugin in plugins)
    guard_environment = (
        settings.delegation_guard_environment() if guard_installed else {}
    )
    broker_operations: dict[str, hook_broker.BrokerOperation] = {}
    if blind and staging is not None and hook_proxy is not None:
        # The wrappers stay private on the host and execute only through the
        # broker; the sandbox sees just the generic proxy command.
        hook_config = _blind_hook_config(plugins, hook_proxy)
        for record in hook_commands:
            record["blind_command"] = [str(hook_proxy), record["plugin"]]
            record["broker_operation"] = record["plugin"]
        broker_operations = _blind_broker_operations(
            hook_commands, guard_environment, controller_environment
        )
    model = settings.model_for("coordinator")
    swarm_model = settings.model_for("swarm")
    routed_swarm_model = (
        "inherit" if swarm_model == model else "worker_acceptance:" + swarm_model
    )
    config = (
        "[features]\nmemory = false\n"
        + blueprint_config(
            blueprint_dir,
            blueprints,
            routed_swarm_model,
            settings.limits.swarm_timeout_minutes,
            {
                name: (
                    "inherit"
                    if settings.model_for_worker(name) == model
                    else "worker_acceptance:" + settings.model_for_worker(name)
                )
                for name in settings.worker_models
            },
            coordinator_effort=settings.effort_for("coordinator"),
            swarm_effort=settings.effort_for("swarm"),
            worker_efforts=settings.worker_effort,
        )
        + hook_config
        + "\n"
        + profile_config(settings)
    )
    effective = tomllib.loads(config)
    effective["delegation_guard"] = {
        "active": guard_installed,
        **settings.delegation_guard.manifest(),
        **({"environment": dict(guard_environment)} if guard_environment else {}),
    }
    if controller_provenance is not None:
        effective["escalation_controller"] = controller_provenance
    effective["timeouts"] = {
        "scenario": scenario.id,
        **timeouts.manifest(),
    }
    effective["settings"] = settings_manifest
    if blind and staging is not None:
        effective["filesystem_isolation"] = {
            "mode": "blind",
            "runtime": "bwrap",
            "staging_root": str(staging.root),
            "public_mounts": [str(directory) for directory in staging.dirs],
            "sandbox_home": str(staging.home),
            "sandbox_workspace": str(staging.workspace),
            "hook_transport": "host-broker",
            "broker_endpoint": str(staging.runtime / "hook-broker.sock"),
            "private_host_only": [
                str(instrumentation),
                str(trace_root),
            ],
        }
    write_json(output / "effective" / "config.json", effective, redactor)
    write_json(
        output / "effective" / "hooks.json",
        {"commands": hook_commands},
        redactor,
    )
    environment = {
        KEY_ENV: settings.api_key,
        "JCODE_SWARM_ENABLED": "true",
        "JCODE_SWARM_MODEL": routed_swarm_model,
        "JCODE_SWARM_SPAWN_MODE": "headless",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if not blind:
        environment["CARGO_TARGET_DIR"] = str(
            temporary_root / "cargo-target" / scenario.id
        )
    # Explicit guard env; never inherited from the caller's environment.
    # In blind mode guard and controller variables never enter the sandbox:
    # they are private broker operation env on the host side only.
    if not blind:
        environment.update(guard_environment)
        # Explicit scenario-local controller env; never inherited either.
        environment.update(controller_environment)
    events: list[_JSON] = []
    session_listing: list[_JSON] = []
    tool_snapshots: list[_JSON] = []
    retention_gaps: list[str] = list(blueprint_issues)
    retained_files: list[_JSON] = []
    coordinator_id: str | None = None
    response_text = ""
    conversation: list[_JSON] = []
    simulated_user_calls = 0
    termination = "not-started"
    execution: _JSON = {
        "status": "completed",
        "error": None,
        "session_id": None,
        "stop_reason": None,
        "applied_timeouts": timeouts.manifest(),
        "deadline_exceeded": False,
    }
    runtime_holder: dict[str, Any] = {}
    final_retained = False
    # In blind mode this becomes the staged public workspace after launch;
    # until then it points at the private host materialized workspace.
    eval_workspace = workspace

    def retain_final() -> None:
        nonlocal final_retained
        if final_retained or "root" not in runtime_holder:
            return
        gaps, copied = retain_native_runtime(
            home=runtime_holder["home"],
            runtime_dir=runtime_holder["root"] / "runtime",
            raw_dir=output / "raw",
            redactor=redactor,
        )
        retention_gaps.extend(gaps)
        retained_files.extend(copied)
        final_retained = True

    try:
        launch_context: AbstractContextManager[HarnessRuntime | BlindHarnessRuntime]
        if blind and staging is not None:
            launch_context = launch_blind_harness(
                staging=staging,
                config=config,
                profile="worker_acceptance",
                model=model,
                workspace=workspace,
                environment=environment,
                broker_operations=broker_operations,
                broker_log=instrumentation / "hook-broker.log",
                turn_timeout=timeouts.turn_timeout_seconds,
                shutdown_gaps=retention_gaps,
                created=lambda root: runtime_holder.update(
                    home=root / "runtime" / "jcode", root=root
                ),
                stopped=retain_final,
            )
        else:
            launch_context = launch_harness(
                jcode=binary,
                bridge=bridge,
                config=config,
                profile="worker_acceptance",
                model=model,
                workspace=workspace,
                environment=environment,
                turn_timeout=timeouts.turn_timeout_seconds,
                shutdown_gaps=retention_gaps,
                created=lambda root: runtime_holder.update(
                    home=root / "jcode", root=root
                ),
                stopped=retain_final,
            )
        with launch_context as runtime:
            runtime_holder.update(home=runtime.home, root=runtime.root)
            # In blind mode the agent's live tree is the staged public copy;
            # acceptance, snapshots, and diffs must all follow it.
            eval_workspace = runtime.workspace
            try:
                session = runtime.client.create_session(working_dir=eval_workspace)
                coordinator_id = session.session_id
                execution["session_id"] = coordinator_id
                initial_tools = _list_tools(
                    runtime.client,
                    coordinator_id,
                    "coordinator-initial",
                    retention_gaps,
                )
                if initial_tools is not None:
                    tool_snapshots.append(initial_tools)
                prompt = scenario.prompt
                # The execution budget starts at the first coordinator task:
                # it covers every coordinator turn, including delegated worker
                # waits, and excludes build, independent acceptance, and judge.
                execution_started = time.monotonic()

                def remaining_execution() -> float | None:
                    if timeouts.execution_timeout_seconds is None:
                        return None
                    return timeouts.execution_timeout_seconds - (
                        time.monotonic() - execution_started
                    )

                for turn_index in range(scenario.simulated_user.max_turns):
                    deadline_remaining = remaining_execution()
                    if deadline_remaining is not None and deadline_remaining <= 0:
                        execution["status"] = "timeout"
                        execution["error"] = (
                            "scenario execution deadline of "
                            f"{timeouts.execution_timeout_seconds}s exceeded "
                            f"before coordinator turn {turn_index}"
                        )
                        execution["deadline_exceeded"] = True
                        termination = "execution-deadline"
                        break
                    # A delegated worker waits inside its coordinator turn, so
                    # the per-turn budget min(turn limit, remaining execution)
                    # also bounds worker waits by the overall deadline. The
                    # budget is always positive here: an exhausted deadline
                    # breaks above instead of producing a negative wait, and
                    # a timed-out turn never retries silently.
                    turn_budget: float = timeouts.turn_timeout_seconds
                    if deadline_remaining is not None:
                        turn_budget = min(turn_budget, deadline_remaining)
                    conversation.append(
                        {
                            "role": "user",
                            "origin": (
                                "scenario" if turn_index == 0 else "simulated-user"
                            ),
                            "turn": turn_index,
                            "content": prompt,
                            "done": False,
                            "sent": True,
                        }
                    )
                    try:
                        response = session.send_message(
                            prompt,
                            capture_events=True,
                            event_sink=events,
                            timeout=turn_budget,
                        )
                        response_text = response.text
                        conversation.append(
                            {
                                "role": "assistant",
                                "origin": "coordinator",
                                "turn": turn_index,
                                "content": response.text,
                            }
                        )
                        if not response.completed_normally:
                            execution["status"] = "runtime error"
                            execution["stop_reason"] = response.stop_reason
                            execution["error"] = (
                                response.error_message or response.stop_message
                            )
                            termination = "abnormal-stop"
                            break
                    except HarnessTimeoutError as error:
                        execution["status"] = "timeout"
                        execution["error"] = str(error)
                        termination = "coordinator-timeout"
                        break
                    except Exception as error:
                        execution["status"] = "runtime error"
                        execution["error"] = str(error)
                        termination = "coordinator-error"
                        break
                    try:
                        simulated_user_calls += 1
                        reply = next_user_reply(
                            settings=settings,
                            scenario=scenario,
                            conversation=conversation,
                            output=output
                            / "raw"
                            / "simulated-user"
                            / f"turn-{turn_index:06d}",
                            usage_tracker=scenario_tracker,
                        )
                    except Exception as error:
                        execution["status"] = "runtime error"
                        execution["error"] = f"simulated user: {error}"
                        termination = "simulated-user-error"
                        break
                    if (
                        reply.done
                        or turn_index + 1 == scenario.simulated_user.max_turns
                    ):
                        conversation.append(
                            {
                                "role": "user",
                                "origin": "simulated-user",
                                "turn": turn_index,
                                "content": reply.message,
                                "done": reply.done,
                                "sent": False,
                            }
                        )
                    if reply.done:
                        execution["status"] = "completed"
                        termination = "simulated-user-done"
                        break
                    if turn_index + 1 == scenario.simulated_user.max_turns:
                        execution["status"] = "runtime error"
                        execution["error"] = (
                            "simulated-user turn cap reached before the agent "
                            "announced completion"
                        )
                        termination = "turn-cap"
                        break
                    prompt = reply.message
                session_listing = _list_sessions(
                    runtime.client, "final/error", retention_gaps
                )
            except Exception as error:
                execution["status"] = "runtime error"
                execution["error"] = str(error)
    except Exception as error:
        execution["status"] = "runtime error"
        execution["error"] = str(error)
    retain_final()
    if instrumentation.is_dir():
        gaps, copied = retain_tree(
            instrumentation, output / "raw", redactor, skip_private=False
        )
        retention_gaps.extend(gaps)
        retained_files.extend(copied)
    write_json(output / "raw" / "retention.json", retained_files, redactor)
    write_text(
        output / "events.ndjson",
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        redactor,
    )
    execution["duration_seconds"] = time.monotonic() - started
    execution["response_text"] = response_text
    execution["termination"] = termination
    execution["coordinator_turns"] = sum(
        item["origin"] == "coordinator" for item in conversation
    )
    write_json(output / "conversation.json", conversation, redactor)

    after_execution = snapshot_tree(eval_workspace)
    acceptance = run_acceptance(
        scenario,
        eval_workspace,
        output,
        temporary_root,
        settings.limits.acceptance_timeout_seconds,
        redactor,
    )
    final = snapshot_tree(eval_workspace)
    acceptance_generated = sorted(
        name
        for name in set(after_execution) | set(final)
        if after_execution.get(name) != final.get(name)
    )
    acceptance["generated_or_modified_files"] = acceptance_generated
    write_json(output / "acceptance.json", acceptance, redactor)
    diff = source_diff(eval_workspace, pristine_workspace, pristine, final)
    write_text(output / "changes.patch", diff, redactor)
    try:
        verify_fixture_unchanged(scenario, fixture_digest)
    except (OSError, ValueError, RuntimeError) as error:
        retention_gaps.append(str(error))
    # The staged public tree has served acceptance and retention; remove it
    # so no evaluation copy lingers in a public location. A hard failure that
    # bypasses this point is backstopped by the caller's per-scenario finally.
    if staging is not None:
        shutil.rmtree(staging.root, ignore_errors=True)

    bundle = collect_evidence(
        raw_dir=output / "raw",
        coordinator_session_id=coordinator_id,
        outer_events=events,
        session_listing=session_listing,
        tool_snapshots=tool_snapshots,
        retention_gaps=retention_gaps,
    )
    if any(session.get("worker") == "unknown" for session in bundle.sessions):
        bundle.gaps.append("a native session has no usable worker blueprint identity")
    record_native_session_usage(
        raw_dir=output / "raw",
        settings=settings,
        coordinator_session_id=coordinator_id,
        tracker=scenario_tracker,
    )
    workers = _worker_coverage(blueprints, bundle.sessions)
    metric_reports: dict[str, _JSON] = {}
    metric_records: list[_JSON] = []
    for metric_name in scenario.metrics:
        report, supplemental_records = METRIC_ANALYZERS[metric_name](
            bundle.records,
            trace_root=output / "raw" / "hook-traces",
            evidence_gaps=bundle.gaps,
        )
        metric_reports[metric_name] = report
        metric_records.extend(supplemental_records)
    health = metric_reports.get(
        "tool_health",
        {
            "status": "not requested",
            "calls": [],
            "hooks": [],
            "issues": [],
            "missing_evidence": [],
        },
    )
    scenario_evidence = {
        "citation": f"scenario:{scenario.id}:task",
        "id": scenario.id,
        "prompt": scenario.prompt,
        "accept": list(scenario.accept),
        "metrics": list(scenario.metrics),
        "judge_criteria": list(scenario.judge.criteria),
        "simulated_user": {
            "max_turns": scenario.simulated_user.max_turns,
            "instructions": scenario.simulated_user.instructions,
        },
    }
    supplemental: list[_JSON] = [
        {
            "id": scenario_evidence["citation"],
            "kind": "scenario",
            "data": scenario_evidence,
        },
        *[
            {
                "id": blueprint["citation"],
                "kind": "blueprint",
                "data": blueprint,
            }
            for blueprint in blueprints
        ],
        {
            "id": acceptance["citation"],
            "kind": "acceptance",
            "data": acceptance,
        },
        {
            "id": f"scenario:{scenario.id}:changes",
            "kind": "source-diff",
            "data": {"patch": diff},
        },
        {
            "id": f"scenario:{scenario.id}:effective-config",
            "kind": "effective-config",
            "data": effective,
        },
        {
            "id": f"scenario:{scenario.id}:execution",
            "kind": "execution",
            "data": execution,
        },
        *metric_records,
    ]
    bundle.records = [*supplemental, *bundle.records]
    write_json(output / "metrics.json", metric_reports, redactor)
    if "tool_health" in metric_reports:
        write_json(output / "tool-health.json", health, redactor)
    for metric_name, report in metric_reports.items():
        bundle.records.append(
            {
                "id": f"scenario:{scenario.id}:metric:{metric_name}",
                "kind": "metric",
                "metric": metric_name,
                "data": report,
            }
        )
    write_bundle(output, bundle, redactor)
    projection = build_judge_projection(bundle.records)
    coordinator_snapshot = any(
        session.get("session_id") == coordinator_id for session in bundle.sessions
    )
    if coordinator_id is None or not coordinator_snapshot:
        reason = (
            "no coordinator native session was established"
            if coordinator_id is None
            else "coordinator native snapshot is unavailable for complete review"
        )
        review, coverage = _incomplete_review(output, reason, redactor)
        coverage["judge_max_calls"] = settings.limits.judge_max_calls
        coverage["raw_record_count"] = len(bundle.records)
        coverage["record_count"] = len(projection.records)
        coverage["excluded_by_default"] = projection.excluded
        write_json(output / "judge-coverage.json", coverage, redactor)
    else:
        review, coverage = review_evidence(
            settings=settings,
            scenario=scenario_evidence,
            records=bundle.records,
            evidence_gaps=bundle.gaps,
            blueprints=blueprints,
            plugins=[plugin.manifest() for plugin in plugins],
            acceptance=acceptance,
            tool_health=health,
            workers=workers,
            output=output,
            usage_tracker=scenario_tracker,
        )
    review["unexercised_workers"] = workers["not_exercised"]
    write_json(output / "review.json", review, redactor)
    write_review_markdown(
        output / "review.md",
        review=review,
        acceptance=acceptance,
        tool_health=health,
        workers=workers,
        coverage=coverage,
        redactor=redactor,
    )
    for rec in scenario_tracker.records:
        run_tracker.add(rec)
    usage_summary = scenario_tracker.summary()
    result: _JSON = {
        "id": scenario.id,
        "scenario_source_sha256": scenario.source_sha256,
        "settings": settings_manifest,
        "fixture_sha256": fixture_digest,
        "execution": execution,
        "acceptance": {
            key: value
            for key, value in acceptance.items()
            if key not in {"citation", "stdout", "stderr"}
        },
        "evidence": {
            "status": "complete" if not bundle.gaps else "incomplete",
            "gaps": bundle.gaps,
            "raw_record_count": len(bundle.records),
            "record_count": len(projection.records),
            "page_count": len(
                paginate(projection.records, settings.limits.judge_page_bytes)
            ),
        },
        "tool_health": health["status"],
        "metrics": metric_reports,
        "simulated_user": {
            "calls": simulated_user_calls,
            "max_turns": scenario.simulated_user.max_turns,
            "termination": termination,
        },
        "review": review["review"],
        "cost_usd": usage_summary["cost_usd"],
        "usage": {
            key: value
            for key, value in usage_summary.items()
            if key not in {"cost_usd", "unpriced_calls"}
        },
        "workers": workers,
        "citations": {
            "task": scenario_evidence["citation"],
            "acceptance": acceptance["citation"],
            "changes": f"scenario:{scenario.id}:changes",
            "tool_health": (
                f"scenario:{scenario.id}:metric:tool-health"
                if "tool_health" in metric_reports
                else None
            ),
            "metrics": {
                name: f"scenario:{scenario.id}:metric:{name}" for name in metric_reports
            },
            "findings": sorted(
                {
                    citation
                    for finding in review["findings"]
                    for citation in finding["citations"]
                }
            ),
        },
        "tool_calls": len(health["calls"]),
        "tool_errors": sum(
            call["native_outcome"] == "error" for call in health["calls"]
        ),
        "findings": review["findings"],
        "hook_commands": hook_commands,
        "effective_config_sha256": hashlib.sha256(
            json.dumps(redactor.value(effective), sort_keys=True).encode()
        ).hexdigest(),
        "duration_seconds": time.monotonic() - started,
    }
    if "unpriced_calls" in usage_summary:
        result["unpriced_calls"] = usage_summary["unpriced_calls"]
    write_json(output / "scenario-summary.json", result, redactor)
    return result


def run(
    *,
    binary: Path,
    bridge: Path,
    settings_by_scenario: dict[str, ScenarioSettings],
    output: Path,
    scenario_path: Path,
    scenario_id: str | None,
    blueprints_dir: Path,
    plugins_dir: Path | None,
    build_provenance: _JSON | None = None,
    runtime_isolation: str | None = None,
) -> int:
    """Run selected real-code scenarios sequentially without repeated attempts."""

    isolation_override = validate_runtime_isolation(runtime_isolation)
    if not binary.is_file() or not bridge.is_file():
        raise ValueError("explicit jcode and bridge binaries must exist")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to overwrite evaluation evidence: {output}")
    scenarios = load_scenarios(scenario_path, scenario_id)
    scenario_ids = [scenario.id for scenario in scenarios]
    missing_settings = sorted(set(scenario_ids) - set(settings_by_scenario))
    unknown_settings = sorted(set(settings_by_scenario) - set(scenario_ids))
    if missing_settings or unknown_settings:
        raise ValueError(
            "per-scenario settings must cover exactly the selected scenarios; "
            f"missing: {', '.join(missing_settings) or 'none'}; "
            f"unknown: {', '.join(unknown_settings) or 'none'}"
        )
    unknown_metrics = sorted(
        {name for scenario in scenarios for name in scenario.metrics}
        - set(METRIC_ANALYZERS)
    )
    if unknown_metrics:
        raise ValueError(f"unknown evaluation metrics: {', '.join(unknown_metrics)}")
    isolation_by_scenario = {
        scenario.id: resolve_runtime_isolation(runtime_isolation, scenario.id)
        for scenario in scenarios
    }
    if "blind" in isolation_by_scenario.values():
        # Fail closed before any launch: a missing sandbox or authentication
        # prerequisite aborts the run instead of degrading to host isolation.
        blind_preflight(binary, bridge)
    output.mkdir(parents=True, exist_ok=True)
    redactor = Redactor(
        tuple(entry.settings.api_key for entry in settings_by_scenario.values())
    )
    blueprints, blueprint_issues = snapshot_blueprints(
        blueprints_dir, output / "effective" / "blueprints", redactor
    )
    discovered_workers = {
        str(blueprint["name"])
        for blueprint in blueprints
        if blueprint["name"] != "coordinator"
    }
    for scenario in scenarios:
        scenario_settings = settings_by_scenario[scenario.id].settings
        unknown_worker_models = sorted(
            set(scenario_settings.worker_models) - discovered_workers
        )
        if unknown_worker_models:
            raise ValueError(
                f"scenario {scenario.id} worker_models reference undiscovered "
                "worker blueprint(s): " + ", ".join(unknown_worker_models)
            )
        unknown_worker_efforts = sorted(
            set(scenario_settings.worker_effort) - discovered_workers
        )
        if unknown_worker_efforts:
            raise ValueError(
                f"scenario {scenario.id} worker_effort reference undiscovered "
                "worker blueprint(s): " + ", ".join(unknown_worker_efforts)
            )
    plugins = snapshot_plugins(plugins_dir, output / "effective" / "plugins", redactor)
    manifest: _JSON = {
        "transport": "harness-api-v1",
        "binary": str(binary),
        "binary_sha256": file_sha256(binary),
        "bridge": str(bridge),
        "bridge_sha256": file_sha256(bridge),
        "build": build_provenance or {"status": "externally-selected-binaries"},
        "evaluator": _evaluator_identity(),
        "settings_by_scenario": {
            scenario.id: settings_by_scenario[scenario.id].manifest()
            for scenario in scenarios
        },
        "scenarios": [scenario.manifest() for scenario in scenarios],
        "blueprints": [
            {key: value for key, value in blueprint.items() if key != "content"}
            for blueprint in blueprints
        ],
        "blueprint_issues": blueprint_issues,
        "plugins": [plugin.manifest() for plugin in plugins],
        "runtime_isolation": isolation_override,
        "runtime_isolation_by_scenario": isolation_by_scenario,
        "results": [],
    }
    write_json(output / "manifest.json", manifest, redactor)
    results: list[_JSON] = []
    run_tracker = UsageTracker()
    with tempfile.TemporaryDirectory(prefix="jcode-worker-eval-") as temporary:
        temporary_root = Path(temporary)
        for scenario in scenarios:
            staged_roots: list[Path] = []
            try:
                result = _run_scenario(
                    scenario=scenario,
                    binary=binary,
                    bridge=bridge,
                    settings=settings_by_scenario[scenario.id].settings,
                    settings_manifest=settings_by_scenario[scenario.id].manifest(),
                    output=output / scenario.id,
                    blueprints=blueprints,
                    blueprint_issues=blueprint_issues,
                    blueprint_dir=output / "effective" / "blueprints",
                    plugins=plugins,
                    temporary_root=temporary_root,
                    redactor=redactor,
                    run_tracker=run_tracker,
                    runtime_isolation=isolation_by_scenario[scenario.id],
                    staging_roots=staged_roots,
                )
            finally:
                # Hard-failure guard: remove any public staging root this
                # scenario created but never consumed. Only the temp stage
                # the run itself created is ever deleted.
                for staged_root in staged_roots:
                    shutil.rmtree(staged_root, ignore_errors=True)
            results.append(result)
            manifest["results"].append(
                {
                    "id": scenario.id,
                    "runtime_isolation": isolation_by_scenario[scenario.id],
                    "effective_config_sha256": result["effective_config_sha256"],
                    "hook_commands": result["hook_commands"],
                    "settings": result["settings"],
                }
            )
            run_usage = run_tracker.summary()
            summary_payload: _JSON = {
                "scenarios": results,
                "cost_usd": run_usage["cost_usd"],
                "usage": {
                    key: value
                    for key, value in run_usage.items()
                    if key not in {"cost_usd", "unpriced_calls"}
                },
            }
            if "unpriced_calls" in run_usage:
                summary_payload["unpriced_calls"] = run_usage["unpriced_calls"]
            write_json(output / "manifest.json", manifest, redactor)
            write_json(output / "summary.json", summary_payload, redactor)
            print(
                f"{scenario.id}: execution={result['execution']['status']} "
                f"acceptance={result['acceptance']['status']} "
                f"evidence={result['evidence']['status']} "
                f"tools={result['tool_health']} review={result['review']} "
                f"cost_usd={result['cost_usd']}"
            )
    clean = all(
        result["execution"]["status"] == "completed"
        and result["acceptance"]["status"] == "pass"
        and result["evidence"]["status"] == "complete"
        and all(
            report.get("status") in _METRIC_CLEAN_STATUSES
            for report in result["metrics"].values()
        )
        and result["review"] == "clean"
        for result in results
    )
    return 0 if results and clean else 1
