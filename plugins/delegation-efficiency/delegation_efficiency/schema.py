"""SQLite schema and bounded connection lifecycle."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 1
DERIVATION_VERSION = "part3-analysis-2"
STATE_ENV = "DELEGATION_EFFICIENCY_STATE_DIR"
_configured_state_dir: Path | None = None

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');
CREATE TABLE IF NOT EXISTS deletion_barriers (
  session_id TEXT PRIMARY KEY,
  generation INTEGER NOT NULL,
  deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_key TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  envelope TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  semantics_version TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  occurred_at_ms INTEGER NOT NULL,
  ingested_at TEXT NOT NULL,
  privacy_result TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  session_id TEXT NOT NULL,
  source_json TEXT NOT NULL,
  cost_micros INTEGER,
  cost_currency TEXT,
  cost_source TEXT,
  cost_status TEXT,
  UNIQUE(event_id)
);
CREATE TRIGGER IF NOT EXISTS events_source_immutable
  BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'immutable source event'); END;
CREATE INDEX IF NOT EXISTS events_occurred_idx ON events(occurred_at_ms);
CREATE TABLE IF NOT EXISTS guard_observations (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
 tool_invocation_id TEXT, provider_tool_call_id TEXT, guard_event_id TEXT,
 session_id TEXT, tool_name TEXT, guard_outcome TEXT, failure_reason TEXT,
 lifecycle TEXT, threshold_classification TEXT, transformer_classification TEXT,
 original_bytes INTEGER, original_lines INTEGER, original_tokens INTEGER,
 final_visible_bytes INTEGER, final_visible_lines INTEGER, final_visible_tokens INTEGER,
 tokenizer_identity TEXT, tokenizer_status TEXT, evidence_level TEXT,
 occurred_at_ms INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS guard_observations_source_immutable
  BEFORE UPDATE ON guard_observations
  BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TABLE IF NOT EXISTS delegation_observations (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
 delegation_id TEXT, guard_event_id TEXT, tool_invocation_id TEXT,
 parent_session_id TEXT, child_session_id TEXT, session_id TEXT, blueprint_id TEXT,
 spawn_mode TEXT, point_event_kind TEXT, occurred_at_ms INTEGER NOT NULL,
 status TEXT, failure_reason TEXT, outcome TEXT, evidence_level TEXT,
 follow_up_count INTEGER
);
CREATE TRIGGER IF NOT EXISTS delegation_observations_source_immutable
  BEFORE UPDATE ON delegation_observations
  BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TABLE IF NOT EXISTS provider_usage (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
 guard_event_id TEXT, delegation_id TEXT, request_id TEXT, generation_id TEXT,
 attempt INTEGER, process_role TEXT, provider TEXT, route TEXT, model TEXT,
 cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,
 provider_input_tokens INTEGER, provider_output_tokens INTEGER,
 retryable INTEGER, retry_count INTEGER, provider_response_id TEXT, served_model TEXT,
  cost_micros INTEGER, cost_currency TEXT, cost_source TEXT, cost_status TEXT,
  cost_version TEXT, cost_timestamp_ms INTEGER, attribution_status TEXT,
  intermediate INTEGER, final INTEGER,
  evidence_level TEXT
);
CREATE TRIGGER IF NOT EXISTS provider_usage_source_immutable
  BEFORE UPDATE ON provider_usage
  BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TABLE IF NOT EXISTS communication_observations (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
 guard_event_id TEXT, delegation_id TEXT, session_id TEXT, process_role TEXT, direction TEXT,
 communication_kind TEXT, bytes INTEGER, tokens INTEGER, tokenizer_status TEXT,
 occurred_at_ms INTEGER NOT NULL, cost_micros INTEGER, cost_currency TEXT,
 cost_source TEXT, cost_status TEXT
);
CREATE TRIGGER IF NOT EXISTS communication_observations_source_immutable
  BEFORE UPDATE ON communication_observations
  BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TABLE IF NOT EXISTS monthly_aggregates (
  month TEXT NOT NULL, event_kind TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  guard_outcome TEXT NOT NULL, evidence_class TEXT NOT NULL, event_count INTEGER NOT NULL,
 known_cost_micros INTEGER, known_cost_count INTEGER NOT NULL, unknown_cost_count INTEGER NOT NULL,
  PRIMARY KEY(month, event_kind, provider, model, guard_outcome, evidence_class)
);
CREATE TABLE IF NOT EXISTS aggregate_event_keys (
  event_id TEXT PRIMARY KEY,
  rolled_up_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_analysis (
  event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
  derivation_version TEXT NOT NULL,
  month TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  evidence_class TEXT NOT NULL,
  tokenizer_status TEXT,
  eligible_guard INTEGER,
  intercepted INTEGER,
  explicit_delegation_link INTEGER,
  linkage_class TEXT NOT NULL,
  immediate_avoided_bytes INTEGER,
  immediate_avoided_lines INTEGER,
  immediate_avoided_tokens INTEGER,
  immediate_avoided_bytes_evidence TEXT NOT NULL DEFAULT 'unknown',
  immediate_avoided_lines_evidence TEXT NOT NULL DEFAULT 'unknown',
  immediate_avoided_tokens_evidence TEXT NOT NULL DEFAULT 'unknown',
  payload_evidence TEXT NOT NULL,
  threshold_value INTEGER,
  guard_outcome TEXT,
  outcome TEXT,
  latency_ms INTEGER,
  follow_up_count INTEGER,
  communication_bytes INTEGER,
  communication_tokens INTEGER,
  provider TEXT,
  model TEXT,
  process_role TEXT,
  blueprint_name TEXT,
  cost_coverage_class TEXT NOT NULL,
  cost_micros INTEGER,
  provider_input_tokens INTEGER,
  provider_output_tokens INTEGER,
  cache_read_input_tokens INTEGER,
  cache_creation_input_tokens INTEGER,
  retry_count INTEGER
  ,cost_currency TEXT,
  original_bytes INTEGER,
  original_lines INTEGER,
  original_tokens INTEGER,
  final_visible_bytes INTEGER,
  final_visible_lines INTEGER,
  final_visible_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS event_analysis_month_idx ON event_analysis(month);
CREATE INDEX IF NOT EXISTS event_analysis_dimension_idx ON event_analysis(month, provider, model, process_role);
"""


def resolve_state_dir(
    cli_state_dir: str | os.PathLike[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve state without requiring setup for ordinary plugin use.

    The optional arguments make precedence and platform branches testable
    without consulting the caller's real home directory.
    """
    environment = os.environ if environ is None else environ
    value = cli_state_dir or environment.get(STATE_ENV)
    if value:
        return Path(value).expanduser()

    if environment.get("JCODE_HOME"):
        return Path(environment["JCODE_HOME"]).expanduser() / "state" / "delegation-efficiency"
    if environment.get("XDG_STATE_HOME"):
        return Path(environment["XDG_STATE_HOME"]).expanduser() / "jcode" / "delegation-efficiency"

    user_home = Path(home).expanduser() if home is not None else Path(environment.get("HOME", Path.home()))
    system = sys.platform if platform is None else platform
    if system.startswith("win"):
        base = Path(environment.get("LOCALAPPDATA", user_home / "AppData" / "Local"))
    elif system == "darwin":
        base = user_home / "Library" / "Application Support"
    else:
        base = user_home / ".local" / "state"
    return base / "jcode" / "delegation-efficiency"


def configure_state_dir(cli_state_dir: str | os.PathLike[str] | None) -> None:
    """Set the process-local CLI choice used by library calls in this command."""
    global _configured_state_dir
    _configured_state_dir = Path(cli_state_dir).expanduser() if cli_state_dir else None


def state_dir(cli_state_dir: str | os.PathLike[str] | None = None) -> Path:
    path = resolve_state_dir(cli_state_dir or _configured_state_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return state_dir() / "delegation-efficiency.sqlite3"


def connect(path: Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(path or db_path(), timeout=2.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 2000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_SQL)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(event_analysis)")}
    for name in (
        "immediate_avoided_bytes_evidence",
        "immediate_avoided_lines_evidence",
        "immediate_avoided_tokens_evidence",
    ):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE event_analysis ADD COLUMN {name} TEXT NOT NULL DEFAULT 'unknown'"
            )
    communication_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(communication_observations)")
    }
    for name, definition in (
        ("process_role", "TEXT"),
        ("cost_micros", "INTEGER"),
        ("cost_currency", "TEXT"),
        ("cost_source", "TEXT"),
        ("cost_status", "TEXT"),
    ):
        if name not in communication_columns:
            try:
                connection.execute(
                    f"ALTER TABLE communication_observations ADD COLUMN {name} {definition}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    provider_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(provider_usage)")
    }
    for name, definition in (("intermediate", "INTEGER"), ("final", "INTEGER")):
        if name not in provider_columns:
            try:
                connection.execute(
                    f"ALTER TABLE provider_usage ADD COLUMN {name} {definition}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    return connection
