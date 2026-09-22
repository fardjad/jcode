#!/usr/bin/env python3
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, sys, tempfile, threading, unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from delegation_efficiency.aggregate import report, rollup
from delegation_efficiency.analysis import analysis_report, counterfactual_net_savings, export_analysis, threshold_analysis
from delegation_efficiency.contract import EnvelopeError, compatibility, validate_event
from delegation_efficiency.ingest import ingest
from delegation_efficiency.privacy import purge_aggregates, purge_detail, purge_session, retain, status
from delegation_efficiency.schema import connect, configure_state_dir, resolve_state_dir
from report import visual_report


def event(kind="tool_result", event_id="event-1", **extra):
    value = {
        "envelope": "jcode.delegation-efficiency.v1", "schema_version": "1.0",
        "semantics_version": "part1-runtime-1", "event": kind, "event_id": event_id,
        "occurred_at_unix_ms": 1700000000000, "session_id": "session-1",
        "tool_invocation_id": "tool-1", "tool_name": "read", "evidence_level": "measured",
        "tokenizer_identity": "chars_div_4", "tokenizer_status": "approximate",
        "evidence": "measured", "attribution_status": "not_applicable", "tool_latency_ms": 3,
    }
    value.update(extra)
    return value

class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["DELEGATION_EFFICIENCY_STATE_DIR"] = self.temp.name

    def tearDown(self):
        configure_state_dir(None)
        os.environ.pop("DELEGATION_EFFICIENCY_STATE_DIR", None)
        self.temp.cleanup()

    def test_state_resolution_precedence_and_disposable_default_branches(self):
        cli = pathlib.Path(self.temp.name) / "cli"
        env_state = pathlib.Path(self.temp.name) / "env-state"
        jcode_home = pathlib.Path(self.temp.name) / "jcode-home"
        xdg_state = pathlib.Path(self.temp.name) / "xdg-state"
        env = {
            "DELEGATION_EFFICIENCY_STATE_DIR": str(env_state),
            "JCODE_HOME": str(jcode_home),
            "XDG_STATE_HOME": str(xdg_state),
            "HOME": str(pathlib.Path(self.temp.name) / "home"),
        }
        self.assertEqual(resolve_state_dir(cli, environ=env), cli)
        self.assertEqual(resolve_state_dir(environ=env), env_state)
        env.pop("DELEGATION_EFFICIENCY_STATE_DIR")
        self.assertEqual(resolve_state_dir(environ=env), jcode_home / "state" / "delegation-efficiency")
        env.pop("JCODE_HOME")
        self.assertEqual(resolve_state_dir(environ=env), xdg_state / "jcode" / "delegation-efficiency")
        env.pop("XDG_STATE_HOME")
        self.assertEqual(resolve_state_dir(environ=env, platform="linux"), pathlib.Path(env["HOME"]) / ".local" / "state" / "jcode" / "delegation-efficiency")
        self.assertEqual(resolve_state_dir(environ=env, home=env["HOME"], platform="darwin"), pathlib.Path(env["HOME"]) / "Library" / "Application Support" / "jcode" / "delegation-efficiency")
        env["LOCALAPPDATA"] = str(pathlib.Path(self.temp.name) / "local-app-data")
        self.assertEqual(resolve_state_dir(environ=env, home=env["HOME"], platform="win32"), pathlib.Path(env["LOCALAPPDATA"]) / "jcode" / "delegation-efficiency")

    def test_contract_golden_null_heavy_and_adversarial_values(self):
        self.assertEqual(compatibility(event()), "current")
        self.assertEqual(compatibility({**event(), "schema_version": "2.0"}), "unsupported_schema")
        self.assertEqual(compatibility({**event(), "event": "future"}), "unknown_event_kind")
        self.assertEqual(compatibility({**event(), "x_future_scalar": "rejected"}), "unknown_field")
        for bad in (
            {**event(), "x_payload": {"prompt": "secret"}},
            {**event(), "x_future_scalar": "must not persist"},
            {**event(), "prompt": "secret"},
            {**event(), "session_id": "/Users/private"},
            {**event(), "event_id": {"nested": "bad"}},
            {**event(), "tool_latency_ms": -1},
            {**event(), "unknown": "value"},
        ):
            with self.assertRaises(EnvelopeError): validate_event(bad)
        null_heavy = event("provider_usage", "usage-1", provider="generic", model="model", attribution_status="unknown_linkage", request_id=None, generation_id=None, attempt=None, cost_micros=None)
        self.assertIsNone(validate_event(null_heavy)["cost_micros"])
        with self.assertRaises(EnvelopeError): validate_event({**event(), "deletion_generation": "4"})
        with self.assertRaises(EnvelopeError): validate_event({**event(), "deletion_generation": -1})

    def test_allowlisted_string_values_are_structural_or_closed(self):
        accepted = event(tool_name="read_file", provider="openrouter", route="eu/standard", model="anthropic/claude-3.5-sonnet", served_model="claude-3.5-sonnet", blueprint_name="coordinator", spawn_mode="headless", outcome="success", failure_reason="timeout", lifecycle="completed", status="completed", evidence_level="provider_reported", tokenizer_identity="provider-reported", tokenizer_status="provider_reported", evidence="provider_reported", attribution_status="unknown_linkage")
        self.assertEqual(validate_event(accepted)["model"], "anthropic/claude-3.5-sonnet")
        for field, bad in {
            "provider": "/Users/private", "model": "a" * 32, "route": "../../secret",
            "served_model": "sha256:" + "a" * 64, "failure_reason": "prompt:secret",
            "status": "result text", "tokenizer_identity": "metadata-secret", "tool_name": "prompt",
        }.items():
            with self.assertRaises(EnvelopeError, msg=field):
                validate_event({**event(), field: bad})

    def test_rejects_before_database_write_and_record_is_fail_open(self):
        with self.assertRaises(EnvelopeError): ingest({**event(), "content": "secret"})
        self.assertEqual(status()["events"], 0)
        proc = subprocess.run([sys.executable, str(ROOT / "record.py")], input=b"{not json", env=os.environ, capture_output=True, check=True)
        self.assertEqual(json.loads(proc.stdout), {"status": "dropped"})
        self.assertNotIn(b"json", proc.stderr.lower())

    def test_record_uses_hook_payload_and_falls_back_to_stdin(self):
        payload = json.dumps(event(event_id="hook-payload-1"))
        env = os.environ.copy()
        env["JCODE_HOOK_PAYLOAD"] = payload
        hook_proc = subprocess.run(
            [sys.executable, str(ROOT / "record.py")],
            input=b"{not json",
            env=env,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(hook_proc.stdout), {"status": "inserted"})

        env.pop("JCODE_HOOK_PAYLOAD")
        stdin_proc = subprocess.run(
            [sys.executable, str(ROOT / "record.py")],
            input=json.dumps(event(event_id="stdin-payload-1")).encode(),
            env=env,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(stdin_proc.stdout), {"status": "inserted"})
        self.assertEqual(status()["events"], 2)

    def test_idempotent_source_and_normalized_rows_preserve_cost_provenance(self):
        provider = event("provider_usage", "usage-1", provider="openrouter", model="m", request_id="request-1", generation_id="generation-1", attempt=0, cost_micros=17, cost_source="provider_response", cost_status="provider_reported", cost_currency=None, provider_input_tokens=10)
        self.assertEqual(ingest(provider), "inserted")
        self.assertEqual(ingest(provider), "duplicate")
        db = connect()
        self.assertEqual(db.execute("select count(*) from events").fetchone()[0], 1)
        row = db.execute("select cost_micros,cost_source,source_json from events").fetchone()
        self.assertEqual((row[0], row[1]), (17, "provider_response"))
        self.assertIn('"cost_micros":17', row[2])
        self.assertEqual(db.execute("select count(*) from provider_usage").fetchone()[0], 1)
        db.close()

    def test_separate_observation_tables_and_no_heuristic_join(self):
        ingest(event("tool_result", "guard-1", guard_event_id="guard-1", original_tokens=100, final_visible_tokens=10))
        ingest(event("delegation_spawn", "delegation-1", delegation_id="delegation-1", child_session_id="child-1", guard_event_id=None, evidence_level="estimated_or_null"))
        ingest(event("provider_usage", "usage-2", provider="p", model="m", attribution_status="unknown_linkage"))
        db = connect()
        self.assertEqual(db.execute("select count(*) from guard_observations").fetchone()[0], 1)
        self.assertEqual(db.execute("select count(*) from delegation_observations").fetchone()[0], 1)
        self.assertIsNone(db.execute("select guard_event_id from delegation_observations").fetchone()[0])
        self.assertEqual(db.execute("select count(*) from provider_usage where attribution_status='unknown_linkage'").fetchone()[0], 1)
        db.close()

    def test_typed_communication_observation_is_normalized_without_content(self):
        communication = event(
            "communication_observation", "communication-1", direction="outbound",
            communication_kind="summary", bytes=128, tokens=32,
            delegation_id="delegation-1", guard_event_id=None,
        )
        self.assertEqual(ingest(communication), "inserted")
        db = connect()
        row = db.execute("SELECT direction,communication_kind,bytes,tokens,tokenizer_status FROM communication_observations").fetchone()
        self.assertEqual(tuple(row), ("outbound", "summary", 128, 32, "approximate"))
        source = db.execute("SELECT source_json FROM events").fetchone()[0]
        self.assertNotIn("prompt", source)
        self.assertNotIn("raw", source)
        self.assertEqual(db.execute("SELECT count(*) FROM communication_observations").fetchone()[0], 1)
        db.close()
        for field, value in (("direction", "worker text"), ("communication_kind", "prompt"), ("bytes", "128"), ("tokens", -1)):
            with self.assertRaises(EnvelopeError):
                validate_event({**communication, field: value})

    def test_clean_standalone_install_contains_package_and_executable_scripts(self):
        installed = pathlib.Path(self.temp.name) / "plugin"
        installed.mkdir()
        for name in ("pyproject.toml", "uv.lock", "record.py", "report.py", "retention.py"):
            shutil.copy2(ROOT / name, installed / name)
        shutil.copytree(ROOT / "delegation_efficiency", installed / "delegation_efficiency")
        env = os.environ.copy()
        env["DELEGATION_EFFICIENCY_STATE_DIR"] = str(pathlib.Path(self.temp.name) / "state")
        env.pop("PYTHONPATH", None)
        payload = json.dumps(event("communication_observation", "installed-communication", direction="inbound", communication_kind="follow_up", bytes=16, tokens=4)).encode()
        record = subprocess.run([str(installed / "record.py")], input=payload.decode(), cwd=self.temp.name, env=env, capture_output=True, check=True, text=True)
        self.assertEqual(json.loads(record.stdout), {"status": "inserted"})
        report_proc = subprocess.run([str(installed / "report.py"), "--rollup"], cwd=self.temp.name, env=env, capture_output=True, check=True, text=True)
        self.assertEqual(json.loads(report_proc.stdout)["scope"], "aggregate")
        self.assertTrue((installed / "delegation_efficiency" / "ingest.py").exists())

    def test_monthly_aggregate_has_no_joinable_identifiers_and_unknown_costs(self):
        ingest(event("tool_result", "aggregate-1", cost_micros=10))
        ingest(event("tool_result", "aggregate-2", cost_micros=None))
        self.assertEqual(rollup(), 1)
        result = report()
        serialized = json.dumps(result)
        self.assertNotIn("session-1", serialized)
        self.assertNotIn("aggregate-1", serialized)
        self.assertEqual(result["monthly"][0]["known_cost_count"], 1)
        self.assertEqual(result["monthly"][0]["unknown_cost_count"], 1)
        self.assertEqual(result["costs"]["label"], "provider_reported_or_unknown")

    def test_visual_report_is_self_contained_and_privacy_safe(self):
        ingest(event("tool_result", "visual-event-1", session_id="visual-session-1", cost_micros=10))
        ingest(event("provider_usage", "visual-usage-1", provider="provider-a", model="model-a", attribution_status="unknown_linkage"))
        self.assertEqual(rollup(), 2)
        rendered = visual_report(report())
        self.assertIn("<!doctype html>", rendered.lower())
        self.assertIn("<style>", rendered)
        self.assertIn("color-scheme:light", rendered)
        self.assertNotIn("color-scheme:light dark", rendered)
        self.assertIn("Delegation cost comparison", rendered)
        self.assertIn("No cost comparison is available yet.", rendered)
        self.assertNotIn("Tool output without delegation", rendered)
        self.assertNotIn("visual-event-1", rendered)
        self.assertNotIn("visual-session-1", rendered)
        self.assertNotIn("provider-a", rendered)
        self.assertNotIn("model-a", rendered)
        self.assertNotIn("sha256", rendered.lower())
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("http://", rendered.lower())
        self.assertNotIn("https://", rendered.lower())

    def test_visual_report_shows_only_the_guard_cost_comparison(self):
        rendered = visual_report({
            "monthly": [{"month": "2026-01", "event_kind": "tool_result", "event_count": 4,
                         "known_cost_count": 0, "unknown_cost_count": 0}],
            "analysis": {
                "counterfactual_net_savings": {
                    "evidence": "estimated_counterfactual",
                    "exclusions": {},
                    "summary": {"comparison": {
                        "complete_delegations": 1,
                        "estimated_without_guard_tokens": 100,
                        "guard_notice_tokens": 5,
                        "delegation_request_tokens": 10,
                        "worker_result_tokens": 35,
                        "delegation_overhead_tokens": 15,
                        "estimated_context_savings_tokens": 85,
                    }},
                },
                "rates": {"interception": {"rate": 0.75}},
                "impact": {
                    "eligible_outputs": 4,
                    "intercepted_eligible_outputs": 3,
                    "avoided": {
                        "bytes": {"total": 240, "available": 3, "opportunity": 3, "coverage": 1.0, "evidence": "measured"},
                        "lines": {"total": 12, "available": 3, "opportunity": 3, "coverage": 1.0, "evidence": "measured"},
                        "tokens": {"total": 60, "available": 3, "opportunity": 3, "coverage": 1.0, "evidence": "estimated"},
                    },
                },
            },
        })
        self.assertIn("Tool output without delegation", rendered)
        self.assertIn("100 tokens", rendered)
        self.assertIn("Delegation overhead", rendered)
        self.assertIn("Estimated context savings", rendered)
        self.assertIn("Guard notice: 5 tokens", rendered)
        self.assertIn("Delegation request: 10 tokens", rendered)
        self.assertIn("Worker result: 35 tokens, task-equivalent and excluded from savings", rendered)
        self.assertNotIn("What the guard saved", rendered)
        self.assertNotIn("interception opportunity", rendered)
        self.assertNotIn("Bytes avoided", rendered)
        for internal in ("part 3", "derivation version", "blueprint", "prompt", "plan", "specialist"):
            self.assertNotIn(internal, rendered.lower())

    def test_visual_report_does_not_render_missing_costs_as_zero(self):
        rendered = visual_report({"analysis": {"counterfactual_net_savings": {
            "summary": {"comparison": {}},
            "exclusions": {"missing_or_unintercepted_guard": 41},
        }}})
        self.assertIn("No cost comparison is available yet.", rendered)
        self.assertIn("Missing data is unavailable, not zero.", rendered)
        self.assertNotIn("Tool output without delegation", rendered)
        self.assertNotIn("0 tokens", rendered)
        self.assertNotIn("missing_or_unintercepted_guard", rendered)

    def test_visual_report_evidence_bars_use_monthly_event_counts_not_rows(self):
        rendered = visual_report({
            "monthly": [
                {"month": "2026-01", "event_kind": "large", "event_count": 100, "evidence_class": "measured"},
                {"month": "2026-01", "event_kind": "small", "event_count": 1, "evidence_class": "estimated"},
            ],
        })

    def test_visual_cli_defaults_to_state_output_and_supports_explicit_output_or_stdout(self):
        ingest(event("delegation_spawn", "visual-delegation-1", delegation_id="visual-delegation-id", child_session_id="visual-child-id"))
        rollup()
        output = pathlib.Path(self.temp.name) / "nested" / "report.html"
        env = os.environ.copy()
        output.parent.mkdir()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "report.py"), "--visual", "--output", str(output)],
            env=env, capture_output=True, check=True, text=True,
        )
        rendered = output.read_text(encoding="utf-8")
        self.assertEqual(json.loads(proc.stdout), {"format": "html", "path": str(output), "status": "written"})
        self.assertIn("Delegation cost comparison", rendered)
        self.assertNotIn("visual-delegation-id", rendered)
        self.assertNotIn("visual-child-id", rendered)

        cli_state = pathlib.Path(self.temp.name) / "cli-state"
        cli_env = os.environ.copy()
        cli_env.pop("DELEGATION_EFFICIENCY_STATE_DIR", None)
        cli_proc = subprocess.run(
            [sys.executable, str(ROOT / "report.py"), "--visual", "--state-dir", str(cli_state)],
            env=cli_env, capture_output=True, check=True, text=True,
        )
        self.assertEqual(json.loads(cli_proc.stdout)["path"], str(cli_state / "aggregate-report.html"))
        self.assertTrue((cli_state / "aggregate-report.html").is_file())

        default_output = pathlib.Path(self.temp.name) / "delegation-efficiency-state" / "aggregate-report.html"
        default_env = os.environ.copy()
        default_env.pop("DELEGATION_EFFICIENCY_STATE_DIR")
        default_env.pop("JCODE_HOME", None)
        default_env["XDG_STATE_HOME"] = str(pathlib.Path(self.temp.name) / "xdg-state")
        default_proc = subprocess.run(
            [sys.executable, str(ROOT / "report.py"), "--visual"],
            env=default_env, capture_output=True, check=True, text=True,
        )
        expected_default = pathlib.Path(default_env["XDG_STATE_HOME"]) / "jcode" / "delegation-efficiency" / "aggregate-report.html"
        self.assertEqual(json.loads(default_proc.stdout)["path"], str(expected_default))
        self.assertTrue(expected_default.is_file())

        stdout_proc = subprocess.run(
            [sys.executable, str(ROOT / "report.py"), "--visual", "--stdout"],
            env=env, capture_output=True, check=True, text=True,
        )
        self.assertIn("<!doctype html>", stdout_proc.stdout.lower())
        self.assertNotIn("visual-delegation-id", stdout_proc.stdout)

    def test_repeated_rollup_upserts_nullable_dimensions_without_doubling(self):
        ingest(event("tool_result", "repeat-1", cost_micros=10))
        ingest(event("tool_result", "repeat-2", cost_micros=None))
        self.assertEqual(rollup(), 1)
        self.assertEqual(rollup(), 1)
        db = connect()
        row = db.execute("SELECT event_count,known_cost_micros,known_cost_count,unknown_cost_count FROM monthly_aggregates").fetchone()
        self.assertEqual(tuple(row), (2, 10, 1, 1))
        db.close()

    def test_retention_monthly_detail_expiry_and_restart(self):
        old = event(event_id="old-1", occurred_at_unix_ms=1)
        ingest(old)
        self.assertEqual(retain(days=90)["deleted_events"], 1)
        self.assertEqual(status()["events"], 0)
        db = connect(); self.assertGreaterEqual(db.execute("select count(*) from monthly_aggregates").fetchone()[0], 1); db.close()

    def test_retention_then_rerollup_preserves_historic_aggregate(self):
        ingest(event(event_id="retention-old-1", occurred_at_unix_ms=1, cost_micros=10))
        first = retain(days=90)
        self.assertEqual(first["deleted_events"], 1)
        self.assertEqual(rollup(), 1)
        row = connect().execute("select event_count,known_cost_micros from monthly_aggregates").fetchone()
        self.assertEqual(tuple(row), (1, 10))

    def test_purge_and_late_ingestion_have_transactional_generation_barrier(self):
        ingest(event(event_id="race-existing", session_id="race"))
        start = threading.Barrier(2)
        results = []
        def purge():
            start.wait(); results.append(purge_session("race", 7))
        def late():
            start.wait(); results.append(ingest(event(event_id="race-late", session_id="race")))
        threads = [threading.Thread(target=purge), threading.Thread(target=late)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertTrue("rejected_deleted_generation" in results or any(isinstance(result, dict) for result in results))
        self.assertEqual(status()["events"], 0)
        db = connect()
        self.assertEqual(db.execute("select generation from deletion_barriers where session_id='race'").fetchone()[0], 7)
        db.close()

    def test_failed_purge_rolls_back_barrier_and_deletion(self):
        ingest(event(event_id="rollback-1", session_id="rollback"))
        db = connect()
        db.execute("CREATE TRIGGER fail_purge BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        db.close()
        with self.assertRaisesRegex(Exception, "injected failure"):
            purge_session("rollback", 9)
        db = connect()
        db.execute("DROP TRIGGER fail_purge")
        self.assertEqual(db.execute("select count(*) from deletion_barriers where session_id='rollback'").fetchone()[0], 0)
        self.assertEqual(db.execute("select count(*) from events where session_id='rollback'").fetchone()[0], 1)
        db.close()
        self.assertEqual(purge_session("rollback", 9)["deleted_events"], 1)
        self.assertEqual(ingest(event(event_id="rollback-late", session_id="rollback")), "rejected_deleted_generation")
        self.assertEqual(status()["events"], 0)

    def test_restart_preserves_wal_journal_state_and_barrier(self):
        ingest(event(event_id="restart-1", session_id="restart"))
        purge_session("restart", 3)
        db = connect(); db.execute("PRAGMA wal_checkpoint(FULL)"); db.close()
        self.assertEqual(ingest(event(event_id="restart-late", session_id="restart")), "rejected_deleted_generation")
        self.assertEqual(status()["events"], 0)
        self.assertEqual(status()["wal"], "enabled")
        self.assertTrue((pathlib.Path(self.temp.name) / "delegation-efficiency.sqlite3").exists())

    def test_deletion_barrier_rejects_late_event_and_purges_children(self):
        ingest(event(event_id="delete-1", session_id="session-delete"))
        ingest(event("provider_usage", "delete-2", session_id="session-delete", provider="p", model="m", attribution_status="unknown_linkage"))
        self.assertEqual(purge_session("session-delete", 4)["deleted_events"], 2)
        late = event(event_id="late-1", session_id="session-delete")
        self.assertEqual(ingest(late), "rejected_deleted_generation")
        db = connect(); self.assertEqual(db.execute("select count(*) from events").fetchone()[0], 0); self.assertEqual(db.execute("select count(*) from provider_usage").fetchone()[0], 0); db.close()

    def test_deletion_generation_allows_only_newer_typed_events(self):
        ingest(event(event_id="delete-generation-1", session_id="session-generation"))
        self.assertEqual(purge_session("session-generation", 4)["deleted_events"], 1)
        self.assertEqual(ingest(event(event_id="late-generation-1", session_id="session-generation", deletion_generation=4)), "rejected_deleted_generation")
        self.assertEqual(ingest(event(event_id="new-generation-1", session_id="session-generation", deletion_generation=5)), "inserted")

    def test_purge_rejects_malformed_generation_without_barrier(self):
        with self.assertRaises(ValueError): purge_session("session-generation", True)
        with self.assertRaises(ValueError): purge_session("session-generation", -1)
        db = connect(); self.assertEqual(db.execute("select count(*) from deletion_barriers").fetchone()[0], 0); db.close()

    def test_repeated_purge_is_safe_and_aggregate_purge_is_explicit(self):
        ingest(event())
        rollup()
        self.assertGreaterEqual(purge_detail()["deleted_events"], 1)
        self.assertEqual(purge_detail()["deleted_events"], 0)
        self.assertGreaterEqual(purge_aggregates()["deleted_aggregates"], 1)
        self.assertEqual(purge_aggregates()["deleted_aggregates"], 0)

    def test_concurrent_short_lived_writers_and_restart(self):
        errors = []
        def write(i):
            try: ingest(event(event_id=f"concurrent-{i}"))
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        db = connect(); self.assertEqual(db.execute("select count(*) from events").fetchone()[0], 20); db.close()
        self.assertEqual(ingest(event(event_id="after-restart")), "inserted")

    def test_executable_report_and_retention_commands(self):
        ingest(event())
        env = os.environ.copy()
        report_proc = subprocess.run([sys.executable, str(ROOT / "report.py")], env=env, capture_output=True, check=True, text=True)
        self.assertEqual(json.loads(report_proc.stdout)["scope"], "aggregate")
        retention_proc = subprocess.run([sys.executable, str(ROOT / "retention.py"), "status"], env=env, capture_output=True, check=True, text=True)
        self.assertEqual(json.loads(retention_proc.stdout)["events"], 1)

    def test_part3_derived_analysis_preserves_source_and_labels_estimates(self):
        guard = event(
            "tool_result", "part3-guard", guard_event_id="part3-guard",
            guard_outcome="intercepted", original_bytes=100, original_lines=10,
            original_tokens=25, final_visible_bytes=40, final_visible_lines=4,
            final_visible_tokens=10, denominator_name="threshold_bytes",
            denominator_value=80, outcome="success", tool_latency_ms=12,
        )
        delegation = event(
            "delegation_spawn", "part3-delegation", delegation_id="part3-delegation",
            guard_event_id="part3-guard", child_session_id="part3-child",
            blueprint_name="specialist", status="completed", outcome="success",
        )
        usage = event(
            "provider_usage", "part3-usage", provider="openrouter", model="model-a",
            guard_event_id="part3-guard",
            request_id="request-1", generation_id="generation-1", attempt=0,
            cost_micros=17, cost_source="provider_response",
            cost_status="provider_reported", attribution_status="eligible",
            provider_input_tokens=30, provider_output_tokens=100,
        )
        for value in (guard, delegation, usage):
            self.assertEqual(ingest(value), "inserted")
        db = connect()
        source_before = db.execute("SELECT source_json,cost_micros FROM events WHERE event_id='part3-usage'").fetchone()
        derived = db.execute("SELECT derivation_version,immediate_avoided_tokens,payload_evidence,linkage_class FROM event_analysis WHERE event_id='part3-guard'").fetchone()
        self.assertEqual(tuple(derived), ("part3-analysis-2", 15, "estimated", "guard_observation"))
        usage_linkage = db.execute("SELECT linkage_class FROM event_analysis WHERE event_id='part3-usage'").fetchone()
        self.assertEqual(usage_linkage[0], "explicit_identities")
        labels = db.execute("SELECT immediate_avoided_bytes_evidence,immediate_avoided_lines_evidence,immediate_avoided_tokens_evidence FROM event_analysis WHERE event_id='part3-guard'").fetchone()
        self.assertEqual(tuple(labels), ("measured", "measured", "estimated"))
        db.close()
        result = analysis_report()
        self.assertEqual(result["derivation_version"], "part3-analysis-2")
        self.assertEqual(result["rates"]["interception"]["numerator"], 1)
        self.assertEqual(result["rates"]["interception"]["denominator"], 1)
        self.assertEqual(result["rates"]["delegation"]["numerator"], 1)
        self.assertEqual(result["costs"]["known_events"], 1)
        self.assertFalse(result["costs"]["billing_truth"])
        export = pathlib.Path(self.temp.name) / "analysis.csv"
        exported = export_analysis(export)
        content = export.read_text(encoding="utf-8")
        self.assertEqual(exported["rows"], 3)
        for identifier in ("part3-guard", "part3-delegation", "part3-child", "request-1", "generation-1"):
            self.assertNotIn(identifier, content)
        self.assertIn("immediate_avoided_tokens", content)
        self.assertEqual(threshold_analysis([50, 100])["thresholds"][0]["eligible"], 1)
        db = connect()
        source_after = db.execute("SELECT source_json,cost_micros FROM events WHERE event_id='part3-usage'").fetchone()
        self.assertEqual(tuple(source_before), tuple(source_after))
        db.close()

    def test_part3_provider_attribution_requires_explicit_target_and_all_identities(self):
        usage = event("provider_usage", "part3-unlinked-usage", provider="p", model="m",
                       request_id="request", generation_id="generation", attempt=0,
                       attribution_status="eligible")
        self.assertEqual(ingest(usage), "inserted")
        row = connect().execute("SELECT linkage_class FROM event_analysis WHERE event_id='part3-unlinked-usage'").fetchone()
        self.assertEqual(row[0], "unknown_linkage")

    def test_part3_source_events_are_immutable(self):
        ingest(event(event_id="immutable-source"))
        ingest(event("provider_usage", "immutable-provider", provider="p", model="m", attribution_status="unknown_linkage"))
        db = connect()
        with self.assertRaisesRegex(Exception, "immutable source event"):
            db.execute("UPDATE events SET cost_micros=99 WHERE event_id='immutable-source'")
        with self.assertRaisesRegex(Exception, "immutable source observation"):
            db.execute("UPDATE provider_usage SET provider='other' WHERE event_id='immutable-provider'")
        db.close()

    def test_part3_unknown_linkage_and_unknown_cost_are_not_joined_or_estimated(self):
        ingest(event("provider_usage", "part3-unknown", provider="provider-a", model="model-a", attribution_status="unknown_linkage", cost_micros=None))
        result = analysis_report()
        self.assertEqual(result["rates"]["unknown_linkage_count"], 1)
        self.assertEqual(result["costs"]["unknown_events"], 1)
        self.assertEqual(result["costs"]["coverage"], 0.0)
        self.assertEqual(result["payload"]["avoided_tokens"]["count"], 0)

    def test_part3_report_commands_export_analysis_and_threshold_without_ids(self):
        ingest(event("tool_result", "part3-cli", guard_outcome="intercepted", original_bytes=100, final_visible_bytes=20))
        env = os.environ.copy()
        export = pathlib.Path(self.temp.name) / "cli-analysis.csv"
        analysis_proc = subprocess.run([sys.executable, str(ROOT / "report.py"), "--analysis"], env=env, capture_output=True, check=True, text=True)
        self.assertEqual(json.loads(analysis_proc.stdout)["scope"], "analysis")
        export_proc = subprocess.run([sys.executable, str(ROOT / "report.py"), "--export", str(export)], env=env, capture_output=True, check=True, text=True)
        self.assertEqual(json.loads(export_proc.stdout)["scope"], "content_free_analysis")
        threshold_proc = subprocess.run([sys.executable, str(ROOT / "report.py"), "--threshold", "50", "--threshold", "150"], env=env, capture_output=True, check=True, text=True)
        threshold = json.loads(threshold_proc.stdout)
        self.assertFalse(threshold["thresholds"][0]["automatic_tuning"])
        self.assertNotIn("part3-cli", export.read_text(encoding="utf-8"))

    def test_measured_guard_event_reaches_aggregate_and_visual_report(self):
        ingest(event(
            "tool_result", "e2e-guard", guard_event_id="e2e-guard",
            guard_outcome="intercepted", original_bytes=430, original_lines=30,
            original_tokens=108, final_visible_bytes=5, final_visible_lines=1,
            final_visible_tokens=2, transform_outcome="applied",
            transformer_classification="applied", transformer_count=1,
        ))
        self.assertEqual(rollup(), 1)
        analysis = analysis_report()
        self.assertEqual(analysis["impact"]["intercepted_eligible_outputs"], 1)
        self.assertEqual(analysis["impact"]["avoided"]["bytes"]["total"], 425)
        self.assertEqual(analysis["impact"]["avoided"]["lines"]["total"], 29)
        rendered = visual_report(report() | {"analysis": analysis})

    def test_counterfactual_net_savings_fully_linked_complete_case(self):
        ingest(event(
            "tool_result", "net-guard", guard_event_id="net-guard",
            guard_outcome="intercepted", original_tokens=100,
            final_visible_tokens=5, original_bytes=400, final_visible_bytes=20,
        ))
        ingest(event(
            "delegation_spawn", "net-delegation", delegation_id="net-delegation",
            guard_event_id="net-guard", child_session_id="net-child",
        ))
        common = dict(
            delegation_id="net-delegation", guard_event_id="net-guard",
            tokenizer_status="provider_reported", tokenizer_identity="provider-reported",
            cost_currency="USD", cost_source="provider_response",
            cost_status="provider_reported",
        )
        ingest(event("communication_observation", "net-notice", direction="inbound",
                     communication_kind="clarification", bytes=20, tokens=5,
                     process_role="coordinator", cost_micros=2, **common))
        ingest(event("communication_observation", "net-spawn", direction="outbound",
                    communication_kind="spawn", bytes=40, tokens=10,
                     process_role="coordinator", cost_micros=3, **common))
        ingest(event("communication_observation", "net-return-1", direction="inbound",
                     communication_kind="summary", bytes=60, tokens=20,
                     process_role="worker", cost_micros=4, **common))
        ingest(event("communication_observation", "net-return-2", direction="inbound",
                     communication_kind="context_read", bytes=30, tokens=15,
                     process_role="worker", cost_micros=5, **common))
        ingest(event("provider_usage", "net-coordinator-usage", provider="p", model="m",
                     process_role="coordinator", guard_event_id="net-guard",
                     delegation_id="net-delegation", request_id="net-request",
                     generation_id="net-generation", attempt=0, cost_micros=100,
                     cost_currency="USD", cost_source="provider_response",
                     cost_status="provider_reported", attribution_status="eligible",
                     provider_output_tokens=7))
        result = counterfactual_net_savings()
        row = result["rows"][0]
        self.assertEqual(row["baseline"]["tokens"], 100)
        self.assertEqual(row["actual"]["tokens"], 50)
        self.assertEqual(row["actual"]["delegation_overhead_tokens"], 15)
        self.assertEqual(row["net"]["tokens"], 85)
        self.assertEqual(
            row["actual"]["components"],
            {
                "guard_notice_tokens": 5,
                "delegation_request_tokens": 10,
                "worker_result_tokens": 35,
            },
        )
        self.assertEqual(
            result["summary"]["comparison"],
            {
                "complete_delegations": 1,
                "estimated_without_guard_tokens": 100,
                "guard_notice_tokens": 5,
                "delegation_request_tokens": 10,
                "worker_result_tokens": 35,
                "delegation_overhead_tokens": 15,
                "estimated_context_savings_tokens": 85,
            },
        )
        self.assertEqual(row["baseline"]["token_basis"], "estimated_coordinator_input_full_original_tool_output")
        self.assertEqual(row["baseline"]["cost_status"], "unavailable_no_authoritative_input_price_or_rate")
        self.assertIsNone(row["net"]["cost_micros"])
        self.assertEqual(row["net"]["cost_status"], "unavailable_no_authoritative_input_price_or_rate")

    def test_counterfactual_does_not_equate_guarded_output_with_provider_output_tokens(self):
        ingest(event("tool_result", "different-token-guard", guard_event_id="different-token-guard",
                     guard_outcome="intercepted", original_tokens=100))
        ingest(event("delegation_spawn", "different-token-delegation",
                     delegation_id="different-token-delegation",
                     guard_event_id="different-token-guard", child_session_id="different-token-child"))
        common = dict(delegation_id="different-token-delegation", guard_event_id="different-token-guard",
                      tokenizer_status="provider_reported", tokenizer_identity="provider-reported")
        for event_id, direction, kind, role, tokens in (
            ("different-token-notice", "inbound", "clarification", "coordinator", 5),
            ("different-token-spawn", "outbound", "summary", "coordinator", 10),
            ("different-token-return", "inbound", "summary", "worker", 20),
        ):
            ingest(event("communication_observation", event_id, direction=direction,
                         communication_kind=kind, process_role=role, bytes=tokens * 4,
                         tokens=tokens, **common))
        ingest(event("provider_usage", "different-token-usage", provider="p", model="m",
                     process_role="coordinator", guard_event_id="different-token-guard",
                     delegation_id="different-token-delegation", request_id="different-token-request",
                     generation_id="different-token-generation", attempt=0,
                     provider_input_tokens=1000, provider_output_tokens=7,
                     cost_micros=100, cost_currency="USD", cost_source="provider_response",
                     cost_status="provider_reported", attribution_status="eligible"))
        row = counterfactual_net_savings()["rows"][0]
        self.assertEqual(row["baseline"]["tokens"], 100)
        self.assertEqual(row["actual"]["tokens"], 35)
        self.assertEqual(row["net"]["tokens"], 85)
        self.assertEqual(row["net"]["token_evidence"], "estimated_coordinator_input_counterfactual")
        self.assertNotEqual(row["baseline"]["tokens"], 7)
        self.assertIsNone(row["net"]["cost_micros"])
        self.assertEqual(row["baseline"]["cost_status"], "unavailable_no_authoritative_input_price_or_rate")
        self.assertEqual(row["net"]["cost_status"], "unavailable_no_authoritative_input_price_or_rate")

    def test_counterfactual_excludes_unlinked_and_missing_cost_monetary_cases(self):
        ingest(event("tool_result", "exclude-guard", guard_event_id="exclude-guard",
                     guard_outcome="intercepted", original_tokens=20))
        ingest(event("delegation_spawn", "exclude-unlinked", delegation_id="exclude-unlinked",
                     guard_event_id=None, child_session_id="exclude-child"))
        ingest(event("delegation_spawn", "exclude-missing", delegation_id="exclude-missing",
                     guard_event_id="exclude-guard", child_session_id="exclude-child-2"))
        ingest(event("communication_observation", "exclude-return", direction="inbound",
                     communication_kind="summary", bytes=4, tokens=2,
                     delegation_id="exclude-missing", guard_event_id="exclude-guard",
                     process_role="worker"))
        result = counterfactual_net_savings()
        self.assertEqual(result["summary"]["linked_intercepted_delegations"], 1)
        self.assertEqual(result["summary"]["monetary_rows"], 0)
        self.assertIsNone(result["rows"][0]["net"]["cost_micros"])
        self.assertEqual(result["rows"][0]["net"]["cost_status"], "unavailable_no_authoritative_input_price_or_rate")

    def test_counterfactual_guard_only_has_no_delegation_population(self):
        ingest(event("tool_result", "guard-only", guard_event_id="guard-only",
                     guard_outcome="intercepted", original_tokens=20,
                     final_visible_tokens=2))
        result = counterfactual_net_savings()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["summary"]["linked_intercepted_delegations"], 0)

    def test_counterfactual_accumulates_multiple_worker_returns_and_reads(self):
        ingest(event("tool_result", "multi-guard", guard_event_id="multi-guard",
                     guard_outcome="intercepted", original_tokens=100))
        ingest(event("delegation_spawn", "multi-delegation", delegation_id="multi-delegation",
                     guard_event_id="multi-guard", child_session_id="multi-child"))
        for event_id, kind, tokens in (
            ("multi-notice", "clarification", 5),
            ("multi-spawn", "summary", 10),
            ("multi-return-1", "summary", 20),
            ("multi-return-2", "context_read", 30),
            ("multi-return-3", "follow_up", 15),
        ):
            ingest(event("communication_observation", event_id,
                         direction="outbound" if event_id == "multi-spawn" else "inbound",
                         communication_kind=kind, bytes=tokens * 4, tokens=tokens,
                         delegation_id="multi-delegation", guard_event_id="multi-guard",
                         process_role="worker" if "return" in event_id else "coordinator"))
        result = counterfactual_net_savings()
        row = result["rows"][0]
        self.assertEqual(row["component_counts"]["worker_returns_or_reads"], 3)
        self.assertEqual(row["actual"]["tokens"], 80)
        self.assertEqual(row["net"]["tokens"], 85)

    def test_counterfactual_excludes_unrelated_same_delegation_communications(self):
        ingest(event("tool_result", "link-guard", guard_event_id="link-guard",
                     guard_outcome="intercepted", original_tokens=20))
        ingest(event("delegation_spawn", "link-delegation", delegation_id="link-delegation",
                     guard_event_id="link-guard", child_session_id="link-child"))
        common = dict(delegation_id="link-delegation", tokenizer_status="provider_reported",
                      tokenizer_identity="provider-reported", bytes=4, tokens=2)
        ingest(event("communication_observation", "link-notice", direction="inbound",
                     communication_kind="clarification", process_role="coordinator",
                     guard_event_id="link-guard", **common))
        ingest(event("communication_observation", "link-spawn", direction="outbound",
                     communication_kind="summary", process_role="coordinator",
                     guard_event_id="link-guard", **common))
        ingest(event("communication_observation", "link-return", direction="inbound",
                     communication_kind="summary", process_role="worker",
                     guard_event_id="link-guard", **common))
        ingest(event("communication_observation", "link-unrelated", direction="inbound",
                     communication_kind="summary", process_role="worker", tokens=90,
                     bytes=180, guard_event_id="other-guard", **{k: v for k, v in common.items()
                     if k not in {"bytes", "tokens"}}))
        row = counterfactual_net_savings()["rows"][0]
        self.assertEqual(row["component_counts"], {"guard_notice": 1, "spawn": 1,
                                                    "worker_returns_or_reads": 1})
        self.assertEqual(row["actual"]["tokens"], 6)

    def test_counterfactual_requires_role_correct_directions_for_each_component(self):
        ingest(event("tool_result", "role-guard", guard_event_id="role-guard",
                     guard_outcome="intercepted", original_tokens=20))
        ingest(event("delegation_spawn", "role-delegation", delegation_id="role-delegation",
                     guard_event_id="role-guard", child_session_id="role-child"))
        common = dict(delegation_id="role-delegation", guard_event_id="role-guard",
                      tokenizer_status="provider_reported", tokenizer_identity="provider-reported",
                      bytes=4, tokens=1)
        ingest(event("communication_observation", "role-notice", direction="inbound",
                     communication_kind="clarification", process_role="coordinator", **common))
        ingest(event("communication_observation", "role-spawn", direction="outbound",
                     communication_kind="summary", process_role="coordinator", **common))
        ingest(event("communication_observation", "role-return", direction="inbound",
                     communication_kind="summary", process_role="worker", **common))
        bad_common = {**common, "tokens": 100, "bytes": 400}
        ingest(event("communication_observation", "role-bad-notice", direction="outbound",
                     communication_kind="clarification", process_role="coordinator",
                     **bad_common))
        ingest(event("communication_observation", "role-bad-spawn", direction="inbound",
                     communication_kind="summary", process_role="coordinator",
                     **bad_common))
        ingest(event("communication_observation", "role-bad-return", direction="outbound",
                     communication_kind="summary", process_role="worker",
                     **bad_common))
        row = counterfactual_net_savings()["rows"][0]
        self.assertEqual(row["component_counts"], {"guard_notice": 1, "spawn": 1,
                                                    "worker_returns_or_reads": 1})
        self.assertEqual(row["actual"]["tokens"], 3)

    def test_counterfactual_allows_zero_components_when_evidence_is_complete(self):
        ingest(event("tool_result", "zero-guard", guard_event_id="zero-guard",
                     guard_outcome="intercepted", original_tokens=0))
        ingest(event("delegation_spawn", "zero-delegation", delegation_id="zero-delegation",
                     guard_event_id="zero-guard", child_session_id="zero-child"))
        common = dict(delegation_id="zero-delegation", guard_event_id="zero-guard",
                      tokenizer_status="provider_reported", tokenizer_identity="provider-reported",
                      bytes=0, tokens=0, cost_micros=0, cost_currency="USD",
                      cost_source="provider_response", cost_status="provider_reported")
        ingest(event("communication_observation", "zero-notice", direction="inbound",
                     communication_kind="clarification", process_role="coordinator", **common))
        ingest(event("communication_observation", "zero-spawn", direction="outbound",
                     communication_kind="summary", process_role="coordinator", **common))
        ingest(event("communication_observation", "zero-return", direction="inbound",
                     communication_kind="summary", process_role="worker", **common))
        ingest(event("provider_usage", "zero-baseline", provider="p", model="m",
                     process_role="coordinator", guard_event_id="zero-guard",
                     delegation_id="zero-delegation", request_id="zero-request",
                     generation_id="zero-generation", attempt=0, provider_output_tokens=0,
                     cost_micros=0, cost_currency="USD", cost_source="provider_response",
                     cost_status="provider_reported", attribution_status="eligible", final=True))
        row = counterfactual_net_savings()["rows"][0]
        self.assertEqual(row["actual"]["tokens"], 0)
        self.assertEqual(row["net"]["tokens"], 0)
        self.assertIsNone(row["net"]["cost_micros"])
        self.assertEqual(row["net"]["cost_status"], "unavailable_no_authoritative_input_price_or_rate")

    def test_counterfactual_excludes_duplicate_and_retry_baseline_attempts(self):
        ingest(event("tool_result", "duplicate-guard", guard_event_id="duplicate-guard",
                     guard_outcome="intercepted", original_tokens=40))
        ingest(event("delegation_spawn", "duplicate-delegation", delegation_id="duplicate-delegation",
                     guard_event_id="duplicate-guard", child_session_id="duplicate-child"))
        common = dict(delegation_id="duplicate-delegation", guard_event_id="duplicate-guard",
                      tokenizer_status="provider_reported", tokenizer_identity="provider-reported",
                      bytes=4, tokens=1, cost_micros=1, cost_currency="USD",
                      cost_source="provider_response", cost_status="provider_reported")
        for event_id, attempt, retry_count, cost in (
            ("duplicate-baseline-a", 0, 0, 100),
            ("duplicate-baseline-b", 0, 0, 200),
            ("duplicate-baseline-retry", 1, 1, 300),
        ):
            ingest(event("provider_usage", event_id, provider="p", model="m",
                         process_role="coordinator", request_id="duplicate-request",
                         generation_id="duplicate-generation", attempt=attempt,
                         retry_count=retry_count, provider_output_tokens=40,
                         cost_micros=cost, cost_currency="USD",
                     cost_source="provider_response", cost_status="provider_reported",
                         attribution_status="eligible", final=True, **{k: v for k, v in common.items()
                         if k not in {"bytes", "tokens", "cost_micros", "cost_currency",
                                      "cost_source", "cost_status"}}))
        for event_id, direction, kind, role in (
            ("duplicate-notice", "inbound", "clarification", "coordinator"),
            ("duplicate-spawn", "outbound", "summary", "coordinator"),
            ("duplicate-return", "inbound", "summary", "worker"),
        ):
            ingest(event("communication_observation", event_id, direction=direction,
                         communication_kind=kind, process_role=role, **common))
        row = counterfactual_net_savings()["rows"][0]
        self.assertIsNone(row["baseline"]["cost_micros"])
        self.assertEqual(row["baseline"]["cost_status"],
                         "unavailable_no_authoritative_input_price_or_rate")
        self.assertIsNone(row["net"]["cost_micros"])

    def test_applied_transformer_reduction_overrides_below_threshold_impact(self):
        ingest(event(
            "tool_result", "contradictory-transformer", guard_event_id="contradictory-transformer",
            guard_outcome="below_threshold", original_bytes=430, original_lines=30,
            original_tokens=108, final_visible_bytes=5, final_visible_lines=1,
            final_visible_tokens=2, transform_outcome="applied",
            transformer_classification="applied", transformer_count=1,
        ))
        analysis = analysis_report()
        self.assertEqual(analysis["impact"]["intercepted_eligible_outputs"], 1)
        self.assertEqual(analysis["impact"]["avoided"]["bytes"]["total"], 425)
        self.assertEqual(analysis["impact"]["avoided"]["lines"]["total"], 29)
        self.assertEqual(analysis["impact"]["avoided"]["tokens"]["total"], 106)
        rendered = visual_report(report() | {"analysis": analysis})

        ingest(event(
            "tool_result", "non-applied-below-threshold", guard_event_id="non-applied-below-threshold",
            guard_outcome="below_threshold", original_bytes=430, final_visible_bytes=5,
            transformer_classification="not_applied",
        ))
        unchanged = analysis_report()
        self.assertEqual(unchanged["impact"]["intercepted_eligible_outputs"], 1)

if __name__ == "__main__": unittest.main()
