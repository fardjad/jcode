"""Single live pipeline for real-code coordinator/worker scenario evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import HarnessTimeoutError
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
from .runtime import launch_harness, runtime_environment
from .scenarios import (
    Scenario,
    load_scenarios,
    materialize_scenario,
    snapshot_tree,
    source_diff,
    tree_digest,
    verify_fixture_unchanged,
)
from .settings import KEY_ENV, ProviderSettings, profile_config
from .tool_health import analyze_tool_health
from .user_actor import next_user_reply

_JSON = dict[str, Any]
MetricAnalyzer = Callable[..., tuple[_JSON, list[_JSON]]]
METRIC_ANALYZERS: dict[str, MetricAnalyzer] = {"tool_health": analyze_tool_health}
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
    output: Path,
    blueprints: list[_JSON],
    blueprint_issues: list[str],
    blueprint_dir: Path,
    plugins: list[PluginSnapshot],
    temporary_root: Path,
    redactor: Redactor,
    run_tracker: UsageTracker,
) -> _JSON:
    started = time.monotonic()
    output.mkdir(parents=True)
    scenario_tracker = UsageTracker()
    workspace = temporary_root / "workspaces" / scenario.id
    fixture_digest = tree_digest(scenario.fixture)
    pristine = materialize_scenario(scenario, workspace)
    pristine_workspace = temporary_root / "pristine" / scenario.id
    pristine_copy = materialize_scenario(scenario, pristine_workspace)
    if pristine_copy != pristine:
        raise RuntimeError("pristine fixture copies differ before evaluation")
    instrumentation = temporary_root / "instrumentation" / scenario.id
    trace_root = instrumentation / "hook-traces"
    hook_config, hook_commands = traced_plugin_config(
        plugins, instrumentation / "wrappers", trace_root
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
        )
        + hook_config
        + "\n"
        + profile_config(settings)
    )
    effective = tomllib.loads(config)
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
        "CARGO_TARGET_DIR": str(temporary_root / "cargo-target" / scenario.id),
    }
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
    }
    runtime_holder: dict[str, Any] = {}
    final_retained = False

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
        with launch_harness(
            jcode=binary,
            bridge=bridge,
            config=config,
            profile="worker_acceptance",
            model=model,
            workspace=workspace,
            environment=environment,
            turn_timeout=settings.limits.turn_timeout_seconds,
            shutdown_gaps=retention_gaps,
            created=lambda root: runtime_holder.update(home=root / "jcode", root=root),
            stopped=retain_final,
        ) as runtime:
            runtime_holder.update(home=runtime.home, root=runtime.root)
            try:
                session = runtime.client.create_session(working_dir=workspace)
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
                for turn_index in range(scenario.simulated_user.max_turns):
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

    after_execution = snapshot_tree(workspace)
    acceptance = run_acceptance(
        scenario,
        workspace,
        output,
        temporary_root,
        settings.limits.acceptance_timeout_seconds,
        redactor,
    )
    final = snapshot_tree(workspace)
    acceptance_generated = sorted(
        name
        for name in set(after_execution) | set(final)
        if after_execution.get(name) != final.get(name)
    )
    acceptance["generated_or_modified_files"] = acceptance_generated
    write_json(output / "acceptance.json", acceptance, redactor)
    diff = source_diff(workspace, pristine_workspace, pristine, final)
    write_text(output / "changes.patch", diff, redactor)
    try:
        verify_fixture_unchanged(scenario, fixture_digest)
    except (OSError, ValueError, RuntimeError) as error:
        retention_gaps.append(str(error))

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
    settings: ProviderSettings,
    output: Path,
    scenario_path: Path,
    scenario_id: str | None,
    blueprints_dir: Path,
    plugins_dir: Path | None,
    build_provenance: _JSON | None = None,
) -> int:
    """Run selected real-code scenarios sequentially without repeated attempts."""

    if not binary.is_file() or not bridge.is_file():
        raise ValueError("explicit jcode and bridge binaries must exist")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to overwrite evaluation evidence: {output}")
    scenarios = load_scenarios(scenario_path, scenario_id)
    unknown_metrics = sorted(
        {name for scenario in scenarios for name in scenario.metrics}
        - set(METRIC_ANALYZERS)
    )
    if unknown_metrics:
        raise ValueError(f"unknown evaluation metrics: {', '.join(unknown_metrics)}")
    output.mkdir(parents=True, exist_ok=True)
    redactor = Redactor((settings.api_key,))
    blueprints, blueprint_issues = snapshot_blueprints(
        blueprints_dir, output / "effective" / "blueprints", redactor
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
        "models": {
            role: settings.model_for(role)
            for role in ("coordinator", "swarm", "judge", "simulator")
        },
        "limits": vars(settings.limits),
        "provider": {
            "type": settings.provider,
            "url": settings.url,
            "auth": settings.auth,
            "auth_header": settings.auth_header,
        },
        "scenarios": [scenario.manifest() for scenario in scenarios],
        "blueprints": [
            {key: value for key, value in blueprint.items() if key != "content"}
            for blueprint in blueprints
        ],
        "blueprint_issues": blueprint_issues,
        "plugins": [plugin.manifest() for plugin in plugins],
        "results": [],
    }
    write_json(output / "manifest.json", manifest, redactor)
    results: list[_JSON] = []
    run_tracker = UsageTracker()
    with tempfile.TemporaryDirectory(prefix="jcode-worker-eval-") as temporary:
        temporary_root = Path(temporary)
        for scenario in scenarios:
            result = _run_scenario(
                scenario=scenario,
                binary=binary,
                bridge=bridge,
                settings=settings,
                output=output / scenario.id,
                blueprints=blueprints,
                blueprint_issues=blueprint_issues,
                blueprint_dir=output / "effective" / "blueprints",
                plugins=plugins,
                temporary_root=temporary_root,
                redactor=redactor,
                run_tracker=run_tracker,
            )
            results.append(result)
            manifest["results"].append(
                {
                    "id": scenario.id,
                    "effective_config_sha256": result["effective_config_sha256"],
                    "hook_commands": result["hook_commands"],
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
