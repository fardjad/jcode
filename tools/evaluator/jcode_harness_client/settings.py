"""Owner-only settings for explicitly authorized live evaluations."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# The private shared defaults file merged under every scenario's public
# TOML settings table; shipped template lives next to it as
# ``config.toml.example``.
CONFIG_NAME = "config.toml"
EXAMPLE_SUFFIX = ".example"
KEY_ENV = "JCODE_ACCEPTANCE_API_KEY"
DELEGATION_GUARD_THRESHOLD = 8192
GUARD_BLUEPRINTS_ENV = "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS"
GUARD_THRESHOLD_ENV = "JCODE_DELEGATION_GUARD_THRESHOLD"
GUARD_TAIL_BYTES_ENV = "JCODE_DELEGATION_GUARD_TAIL_BYTES"
PROVIDERS = {
    "openai-compatible",
    "anthropic-compatible",
    "openrouter",
    "open-router",
}
ROLES = ("coordinator", "swarm", "judge", "simulator")
# Plain reasoning-effort levels routed to each provider's real request
# fields. No recursion aliases such as "swarm": a worker without an
# explicit override simply inherits the swarm effort at resolution time.
EFFORTS = ("low", "medium", "high", "xhigh")
_LIMITS = {
    "turn_timeout_seconds",
    "execution_timeout_seconds",
    "swarm_timeout_minutes",
    "acceptance_timeout_seconds",
}
_TABLES = (
    "provider",
    "models",
    "worker_models",
    "limits",
    "effort",
    "worker_effort",
    "delegation_guard",
)

DEFAULT_EXAMPLE = """# Copy using `init-settings`, which creates mode 600.
# Never put real credentials in this example or commit the private copy.
[provider]
type = "openai-compatible"
url = "https://api.example.com/v1"
model = "REPLACE_WITH_YOUR_MODEL"
api_key = "REPLACE_WITH_YOUR_API_KEY"
auth = "bearer"

[models]
coordinator = "REPLACE_WITH_COORDINATOR_MODEL"
swarm = "REPLACE_WITH_SWARM_MODEL"
judge = "REPLACE_WITH_JUDGE_MODEL"
simulator = "REPLACE_WITH_SIMULATOR_MODEL"

[limits]
turn_timeout_seconds = 300
# Optional whole-execution budget in seconds; omit it to stay unbounded.
# execution_timeout_seconds = 1800
swarm_timeout_minutes = 10
acceptance_timeout_seconds = 300

# Optional plain reasoning efforts (low, medium, high, xhigh) routed to each
# provider's native request fields. Omitted values keep provider defaults.
[effort]
coordinator = "REPLACE_WITH_COORDINATOR_EFFORT_OR_OMIT"
swarm = "REPLACE_WITH_SWARM_EFFORT_OR_OMIT"
judge = "REPLACE_WITH_JUDGE_EFFORT_OR_OMIT"
simulator = "REPLACE_WITH_SIMULATOR_EFFORT_OR_OMIT"

# Optional per-worker effort overrides keyed by discovered worker ID.
# A worker without an entry inherits the swarm effort.
[worker_effort]
worker_one = "REPLACE_WITH_WORKER_EFFORT_OR_OMIT"
worker_two = "REPLACE_WITH_WORKER_EFFORT_OR_OMIT"

[delegation_guard]
# Discovered worker IDs the guard nudges toward delegation. An empty list
# disables worker-role guarding while the coordinator nudge stays active.
worker_blueprints = ["worker_one", "worker_two"]
# Oversized tool-result byte threshold that triggers the delegation nudge.
threshold = 8192
# Optional worker output tail byte limit; 0 disables the tail.
# tail_bytes = 1024
"""


@dataclass(frozen=True)
class EvaluationModels:
    coordinator: str
    swarm: str
    judge: str
    simulator: str


@dataclass(frozen=True)
class EvaluationLimits:
    turn_timeout_seconds: int = 300
    execution_timeout_seconds: int | None = None
    swarm_timeout_minutes: int = 10
    acceptance_timeout_seconds: int = 300


@dataclass(frozen=True)
class EvaluationEfforts:
    """Optional plain reasoning efforts per role; None keeps the default."""

    coordinator: str | None = None
    swarm: str | None = None
    judge: str | None = None
    simulator: str | None = None

    def manifest(self) -> dict[str, object]:
        return {
            "coordinator": self.coordinator,
            "swarm": self.swarm,
            "judge": self.judge,
            "simulator": self.simulator,
        }


@dataclass(frozen=True)
class ScenarioTimeouts:
    """Effective budgets in seconds for one scenario's own settings file."""

    turn_timeout_seconds: int
    execution_timeout_seconds: int | None
    turn_timeout_source: str
    execution_timeout_source: str

    def manifest(self) -> dict[str, object]:
        return {
            "turn_timeout_seconds": self.turn_timeout_seconds,
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "sources": {
                "turn_timeout": self.turn_timeout_source,
                "execution_timeout": self.execution_timeout_source,
            },
        }


@dataclass(frozen=True)
class DelegationGuardSettings:
    """Explicit delegation-guard worker selection and threshold."""

    worker_blueprints: tuple[str, ...] = ()
    threshold: int = DELEGATION_GUARD_THRESHOLD
    tail_bytes: int | None = None

    def manifest(self) -> dict[str, object]:
        return {
            "worker_blueprints": list(self.worker_blueprints),
            "threshold": self.threshold,
            "tail_bytes": self.tail_bytes,
        }


@dataclass(frozen=True)
class ProviderSettings:
    provider: str
    url: str
    model: str
    api_key: str = field(repr=False)
    auth: str = "bearer"
    auth_header: str | None = None
    models: EvaluationModels | None = None
    limits: EvaluationLimits = field(default_factory=EvaluationLimits)
    worker_models: dict[str, str] = field(default_factory=dict)
    effort: EvaluationEfforts = field(default_factory=EvaluationEfforts)
    worker_effort: dict[str, str] = field(default_factory=dict)
    delegation_guard: DelegationGuardSettings = field(
        default_factory=DelegationGuardSettings
    )

    def model_for(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError("unknown evaluation model role")
        return str(getattr(self.models, role)) if self.models else self.model

    def model_for_worker(self, worker_id: str) -> str:
        """Resolve a discovered worker override or the legacy shared swarm model."""

        return self.worker_models.get(worker_id, self.model_for("swarm"))

    def effort_for(self, role: str) -> str | None:
        """Optional plain effort for a role; None keeps provider defaults."""

        if role not in ROLES:
            raise ValueError("unknown evaluation effort role")
        if self.effort is None:
            return None
        value = getattr(self.effort, role)
        return value if isinstance(value, str) else None

    def effort_for_worker(self, worker_id: str) -> str | None:
        """Worker override first, then the swarm effort fallback."""

        return self.worker_effort.get(worker_id, self.effort_for("swarm"))

    def timeouts(self) -> ScenarioTimeouts:
        """Budgets from this scenario's own [limits] table."""

        execution = self.limits.execution_timeout_seconds
        return ScenarioTimeouts(
            turn_timeout_seconds=self.limits.turn_timeout_seconds,
            execution_timeout_seconds=execution,
            turn_timeout_source="limits",
            execution_timeout_source="limits" if execution is not None else "unbounded",
        )

    def delegation_guard_environment(self) -> dict[str, str]:
        """Explicit guard env; injected only when the plugin is installed."""

        environment = {
            GUARD_BLUEPRINTS_ENV: ",".join(self.delegation_guard.worker_blueprints),
            GUARD_THRESHOLD_ENV: str(self.delegation_guard.threshold),
        }
        if self.delegation_guard.tail_bytes is not None:
            environment[GUARD_TAIL_BYTES_ENV] = str(self.delegation_guard.tail_bytes)
        return environment

    def sanitized_manifest(self) -> dict[str, object]:
        """Secret-free settings record for per-scenario run manifests."""

        return {
            "provider": {
                "type": self.provider,
                "url": self.url,
                "auth": self.auth,
                "auth_header": self.auth_header,
            },
            "models": {
                role: self.model_for(role)
                for role in ("coordinator", "swarm", "judge", "simulator")
            },
            "worker_models": dict(self.worker_models),
            "limits": vars(self.limits),
            "effort": self.effort.manifest(),
            "worker_effort": dict(self.worker_effort),
            "delegation_guard": self.delegation_guard.manifest(),
        }


@dataclass(frozen=True)
class ScenarioSettings:
    """One scenario's effective settings plus the shared defaults provenance.

    Overrides come from the version-controlled scenario TOML ``settings``
    mapping, whose raw SHA is already tracked by the scenario manifest, so
    no separate per-scenario source hash exists; the shared private
    defaults get their own explicit provenance fields.
    """

    scenario_id: str
    settings: ProviderSettings
    defaults_source: Path | None = None
    defaults_sha256: str | None = None

    def manifest(self) -> dict[str, object]:
        return {
            "defaults": (
                {
                    "source": self.defaults_source.name,
                    "sha256": self.defaults_sha256,
                }
                if self.defaults_source is not None
                else None
            ),
            "effective_sha256": _effective_sha256(self.settings),
            "settings": self.settings.sanitized_manifest(),
        }


def _effective_sha256(settings: ProviderSettings) -> str:
    """Stable digest of the sanitized merged settings, never raw secrets."""

    payload = json.dumps(
        settings.sanitized_manifest(), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_worker_key(worker_id: str) -> None:
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker IDs must be nonempty strings")
    if worker_id == "coordinator":
        raise ValueError("coordinator is reserved and cannot have a worker entry")
    if worker_id != worker_id.strip() or any(
        ord(character) < 32 for character in worker_id
    ):
        raise ValueError(
            "worker IDs must not contain surrounding whitespace or controls"
        )


def _validate_guard_blueprints(blueprints: object, source: str) -> list[str]:
    if not isinstance(blueprints, list):
        raise ValueError(f"{source} delegation_guard worker_blueprints must be a list")
    seen: set[str] = set()
    for blueprint in blueprints:
        _validate_worker_key(blueprint)
        # The guard receives IDs in a comma-separated environment variable.
        if "," in blueprint:
            raise ValueError("delegation_guard worker IDs must not contain commas")
        if blueprint in seen:
            raise ValueError("delegation_guard worker IDs must be unique")
        seen.add(blueprint)
    return blueprints


def parse_settings(text: str) -> ProviderSettings:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Parser errors can quote secret-bearing source lines.
        raise ValueError(
            "invalid evaluation settings TOML, check syntax without sharing secrets"
        ) from None
    return _settings_from_data(data)


def _validate_provider_url(provider_url: str) -> bool:
    try:
        url = urlsplit(provider_url)
        _ = url.port
        safe_transport = url.scheme == "https" or (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        return bool(
            safe_transport
            and url.hostname
            and not (url.username or url.password or url.query or url.fragment)
        )
    except ValueError:
        return False


def _validate_partial(data: dict[str, object], source: str) -> dict[str, object]:
    """Validate every table one defaults or scenario file defines.

    Completeness is not required: a partial file may define any subset of
    the known tables. Unknown keys and type errors are still rejected,
    even when a later merge would mask them with an override.
    """

    if set(data) - set(_TABLES):
        raise ValueError(
            f"{source} supports [provider] with optional [models], "
            "[worker_models], [limits], [effort], [worker_effort], and "
            "[delegation_guard] only"
        )
    provider = data.get("provider")
    if provider is not None:
        if not isinstance(provider, dict) or set(provider) - {
            "type",
            "url",
            "model",
            "api_key",
            "auth",
            "auth_header",
        }:
            raise ValueError(f"{source} [provider] has unsupported fields")
        for key in ("type", "url", "model", "api_key"):
            if key not in provider:
                continue
            value = provider[key]
            if (
                not isinstance(value, str)
                or not value.strip()
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(f"{source} provider {key} must be nonempty text")
        if "type" in provider and provider["type"] not in PROVIDERS:
            raise ValueError(
                f"{source} provider type must be openai-compatible, "
                "anthropic-compatible, or openrouter"
            )
        if "url" in provider and not _validate_provider_url(provider["url"]):
            raise ValueError(
                f"{source} API URL must use HTTPS (or HTTP loopback), "
                "without credentials, query or fragment"
            )
        auth_value = provider.get("auth")
        if auth_value is not None and (
            not isinstance(auth_value, str) or auth_value not in {"bearer", "header"}
        ):
            raise ValueError(f"{source} auth must be bearer or header")
        header_value = provider.get("auth_header")
        if header_value is not None:
            if (
                not isinstance(header_value, str)
                or not header_value
                or any(
                    not (character.isalnum() or character == "-")
                    for character in header_value
                )
            ):
                raise ValueError(
                    f"{source} header authentication requires a valid auth_header name"
                )
            if auth_value is not None and auth_value != "header":
                raise ValueError(
                    f"{source} auth_header is only valid for header authentication"
                )
    models = data.get("models")
    if models is not None:
        if not isinstance(models, dict) or set(models) - set(ROLES):
            raise ValueError(
                f"{source} models support coordinator, swarm, judge, and simulator only"
            )
        for role, value in models.items():
            if (
                not isinstance(value, str)
                or not value.strip()
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(
                    f"{source} evaluation {role} model must be nonempty text"
                )
    worker_models = data.get("worker_models")
    if worker_models is not None:
        if not isinstance(worker_models, dict):
            raise ValueError(
                f"{source} worker_models must map worker IDs to model names"
            )
        for worker_id, value in worker_models.items():
            _validate_worker_key(worker_id)
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(
                    f"{source} worker model for {worker_id!r} must be "
                    "nonempty model text"
                )
    limits = data.get("limits")
    if limits is not None:
        if not isinstance(limits, dict) or set(limits) - _LIMITS:
            raise ValueError(f"{source} unsupported evaluation limits field")
        for key, value in limits.items():
            if key == "execution_timeout_seconds":
                if value is not None and (type(value) is not int or value < 1):
                    raise ValueError(
                        f"{source} execution_timeout_seconds must be a "
                        "positive integer or omitted"
                    )
                continue
            if type(value) is not int or value < 1:
                raise ValueError(
                    f"{source} evaluation {key} must be a positive integer"
                )
    effort = data.get("effort")
    if effort is not None:
        if not isinstance(effort, dict) or set(effort) - set(ROLES):
            raise ValueError(
                f"{source} effort supports coordinator, swarm, judge, and "
                "simulator only"
            )
        for value in effort.values():
            if not isinstance(value, str) or value not in EFFORTS:
                raise ValueError(
                    f"{source} effort values must be plain low, medium, or high strings"
                )
    worker_effort = data.get("worker_effort")
    if worker_effort is not None:
        if not isinstance(worker_effort, dict):
            raise ValueError(
                f"{source} worker_effort must map worker IDs to effort strings"
            )
        for worker_id, value in worker_effort.items():
            _validate_worker_key(worker_id)
            if not isinstance(value, str) or value not in EFFORTS:
                raise ValueError(
                    f"{source} worker effort for {worker_id!r} must be plain "
                    "low, medium, or high"
                )
    guard = data.get("delegation_guard")
    if guard is not None:
        if not isinstance(guard, dict) or set(guard) - {
            "worker_blueprints",
            "threshold",
            "tail_bytes",
        }:
            raise ValueError(
                f"{source} delegation_guard supports worker_blueprints, "
                "threshold, and tail_bytes only"
            )
        if "worker_blueprints" in guard:
            _validate_guard_blueprints(guard["worker_blueprints"], source)
        if "threshold" in guard:
            threshold = guard["threshold"]
            if type(threshold) is not int or threshold < 0:
                raise ValueError(
                    f"{source} delegation_guard threshold must be a nonnegative integer"
                )
        if "tail_bytes" in guard:
            value = guard["tail_bytes"]
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"{source} delegation_guard tail_bytes must be a nonnegative integer"
                )
    return data


# Credential-bearing field names rejected at any nesting in public
# scenario TOML settings; secrets live only in the private config file.
_CREDENTIAL_KEYS = {
    "api_key",
    "apikey",
    "password",
    "passwd",
    "token",
    "secret",
    "secrets",
    "credential",
    "credentials",
    "private_key",
    "client_secret",
}
# Public scenario overrides may tune non-secret provider routing only.
_SCENARIO_PROVIDER_KEYS = {"type", "url", "model", "auth", "auth_header"}


def _reject_credential_fields(value: object, source: str) -> None:
    """Reject credential-named fields at any nesting depth or list item."""

    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.strip().lower() in _CREDENTIAL_KEYS:
                raise ValueError(
                    f"{source} must not contain credential fields such as "
                    f"{key!r}; secrets belong only in the private defaults file"
                )
            _reject_credential_fields(child, source)
    elif isinstance(value, list):
        for child in value:
            _reject_credential_fields(child, source)


def validate_scenario_overrides(data: object, source: str) -> dict[str, object]:
    """Validate a public scenario TOML ``settings`` override table.

    The table may define any subset of the known tables (an empty table
    inherits everything from the shared config), but unknown keys, type
    errors, and credential fields at any nesting are rejected even
    before the merge. Provider overrides may carry non-secret routing
    fields only; ``api_key`` and friends stay in ``config.toml``.
    """

    if not isinstance(data, dict):
        raise ValueError(f"{source} must be a mapping of setting tables")
    _reject_credential_fields(data, source)
    provider = data.get("provider")
    if provider is not None:
        if not isinstance(provider, dict) or set(provider) - _SCENARIO_PROVIDER_KEYS:
            raise ValueError(
                f"{source} [provider] supports non-secret type, url, model, "
                "auth, and auth_header only; credentials live in the private "
                "defaults file"
            )
    return _validate_partial(data, source)


def _parse_source_tables(text: str, source: str) -> dict[str, object]:
    """Parse one defaults or scenario file into partially validated tables."""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Parser errors can quote secret-bearing source lines.
        raise ValueError(
            f"invalid {source} settings TOML, check syntax without sharing secrets"
        ) from None
    return _validate_partial(data, source)


def _merge_tables(
    base: Mapping[str, object] | None,
    override: Mapping[str, object] | None,
) -> dict[str, object]:
    """Merge defaults under overrides, key-wise per known table.

    An override key replaces the defaults key entirely, including explicit
    empty lists; unmentioned keys inherit the defaults value.
    """

    merged: dict[str, object] = {}
    for source in (base, override):
        if not source:
            continue
        for table, value in source.items():
            existing = merged.get(table)
            if isinstance(existing, dict) and isinstance(value, dict):
                combined = dict(existing)
                combined.update(value)
                merged[table] = combined
            else:
                merged[table] = value
    return merged


def _settings_from_data(data: dict[str, object]) -> ProviderSettings:
    if set(data) - {
        "provider",
        "models",
        "worker_models",
        "limits",
        "effort",
        "worker_effort",
        "delegation_guard",
    } or not isinstance(data.get("provider"), dict):
        raise ValueError(
            "settings require [provider] with optional [models], [worker_models], "
            "[limits], [effort], [worker_effort], and [delegation_guard]"
        )
    models = data.get("models", {})
    worker_models = data.get("worker_models", {})
    limits = data.get("limits", {})
    effort = data.get("effort", {})
    worker_effort = data.get("worker_effort", {})
    if not isinstance(models, dict) or set(models) - set(ROLES):
        raise ValueError("models support coordinator, swarm, judge, and simulator only")
    if not isinstance(worker_models, dict):
        raise ValueError("worker_models must map worker IDs to model names")
    for worker_id, value in worker_models.items():
        _validate_worker_key(worker_id)
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or value.startswith("REPLACE_")
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(
                f"worker model for {worker_id!r} must be nonempty model text"
            )
    if not isinstance(limits, dict) or set(limits) - _LIMITS:
        raise ValueError("unsupported evaluation limits field")
    for key, value in limits.items():
        if key == "execution_timeout_seconds":
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(
                    "evaluation execution_timeout_seconds must be a positive "
                    "integer or omitted"
                )
            continue
        if type(value) is not int or value < 1:
            raise ValueError(f"evaluation {key} must be a positive integer")
    if not isinstance(effort, dict) or set(effort) - set(ROLES):
        raise ValueError(
            "effort supports coordinator, swarm, judge, and simulator only"
        )
    for _role, value in effort.items():
        if not isinstance(value, str) or value not in EFFORTS:
            raise ValueError("effort values must be plain low, medium, or high strings")
    if not isinstance(worker_effort, dict):
        raise ValueError("worker_effort must map worker IDs to effort strings")
    for worker_id, value in worker_effort.items():
        _validate_worker_key(worker_id)
        if not isinstance(value, str) or value not in EFFORTS:
            raise ValueError(
                f"worker effort for {worker_id!r} must be plain low, medium, or high"
            )
    guard = data.get("delegation_guard", {})
    if not isinstance(guard, dict) or set(guard) - {
        "worker_blueprints", "threshold", "tail_bytes"
    }:
        raise ValueError(
            "delegation_guard supports worker_blueprints, threshold, and tail_bytes only"
        )
    blueprints = _validate_guard_blueprints(guard.get("worker_blueprints", []), "settings")
    guard_threshold = guard.get("threshold", DELEGATION_GUARD_THRESHOLD)
    if type(guard_threshold) is not int or guard_threshold < 0:
        raise ValueError("delegation_guard threshold must be a nonnegative integer")
    guard_tail_bytes = guard.get("tail_bytes")
    if "tail_bytes" in guard and (
        type(guard_tail_bytes) is not int or guard_tail_bytes < 0
    ):
        raise ValueError("delegation_guard tail_bytes must be a nonnegative integer")
    provider_data = data["provider"]
    if not isinstance(provider_data, dict) or set(provider_data) - {
        "type",
        "url",
        "model",
        "api_key",
        "auth",
        "auth_header",
    }:
        raise ValueError("unsupported evaluation settings field")
    required: dict[str, str] = {}
    for key in ("type", "url", "model", "api_key"):
        value = provider_data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"evaluation {key} is required")
        if any(ord(character) < 32 for character in value):
            raise ValueError(f"control characters forbidden in {key}")
        required[key] = value
    provider_type = required["type"]
    provider_url = required["url"]
    provider_model = required["model"]
    provider_api_key = required["api_key"]
    if provider_type not in PROVIDERS:
        raise ValueError(
            "provider type must be openai-compatible, "
            "anthropic-compatible, or openrouter"
        )
    try:
        url = urlsplit(provider_url)
        _ = url.port
        safe_transport = url.scheme == "https" or (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        valid = bool(
            safe_transport
            and url.hostname
            and not (url.username or url.password or url.query or url.fragment)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "API URL must use HTTPS (or HTTP loopback), "
            "without credentials, query or fragment"
        )
    auth_value = provider_data.get("auth", "bearer")
    if not isinstance(auth_value, str) or auth_value not in {"bearer", "header"}:
        raise ValueError("auth must be bearer or header")
    auth = auth_value
    header: str | None = None
    if auth == "header":
        header_value = provider_data.get("auth_header")
        if (
            not isinstance(header_value, str)
            or not header_value
            or any(
                not (character.isalnum() or character == "-")
                for character in header_value
            )
        ):
            raise ValueError("header authentication requires a valid auth_header name")
        header = header_value
    elif provider_data.get("auth_header") is not None:
        raise ValueError("auth_header is only valid for header authentication")
    if provider_api_key.strip() == "REPLACE_WITH_YOUR_API_KEY":
        raise ValueError("replace the example API key before making live calls")
    resolved: dict[str, str] = {}
    for role in ROLES:
        value = models.get(role, provider_model)
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(
                f"evaluation {role} model must be nonempty text "
                "without control characters"
            )
        if value != value.strip() or value.startswith("REPLACE_"):
            raise ValueError(
                f"set a concrete evaluation {role} model without surrounding whitespace"
            )
        resolved[role] = value
    return ProviderSettings(
        provider_type,
        provider_url,
        provider_model,
        provider_api_key,
        auth,
        header,
        EvaluationModels(**resolved),
        EvaluationLimits(**limits),
        dict(worker_models),
        EvaluationEfforts(**effort),
        dict(worker_effort),
        DelegationGuardSettings(tuple(blueprints), guard_threshold, guard_tail_bytes),
    )


def _read_private_settings(path: Path) -> tuple[str, str]:
    """Securely read one owner-only settings file and return text plus digest."""

    if os.name == "nt":
        raise ValueError(
            "private credential file mode is not supported on Windows "
            "until ACL checks exist"
        )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError(
            f"cannot securely open {path.name}, "
            "create it from the example with mode 600"
        ) from None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError(
                "credential settings must be a regular file owned by you with mode 600"
            )
        if info.st_size > 16384:
            raise ValueError("credential settings exceed the supported size")
        try:
            text = handle.read()
        except UnicodeError:
            raise ValueError("credential settings must be UTF-8 TOML") from None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, digest


def load_settings(path: Path) -> ProviderSettings:
    text, _ = _read_private_settings(path)
    return parse_settings(text)


def _validate_scenario_id(scenario_id: str) -> None:
    if (
        not isinstance(scenario_id, str)
        or not scenario_id.strip()
        or scenario_id != scenario_id.strip()
        or any(ord(character) < 32 for character in scenario_id)
        or scenario_id in {".", ".."}
        or "/" in scenario_id
        or "\\" in scenario_id
        or scenario_id.startswith(".")
    ):
        raise ValueError(
            "scenario settings filenames must be plain scenario IDs "
            "without path separators or traversal"
        )


def load_scenario_settings(
    config_path: Path,
    scenario_ids: list[str],
    discovered_ids: list[str],
    scenario_settings: Mapping[str, Mapping[str, object] | None],
) -> dict[str, ScenarioSettings]:
    """Preflight-load and merge settings for every selected scenario.

    The exact private shared config file (``evals/config.toml``) is
    securely loaded first and merged key-wise under every scenario's
    public TOML ``settings`` table. Only that one exact file is read;
    nothing in its directory is globbed, so no other private file can be
    read by accident. Overrides stay credential-free in the
    version-controlled scenario config, and the fully merged result is
    validated completely. A scenario without a ``settings`` table
    inherits everything from the shared config. A missing config file is
    a clear preflight error even when no scenario overrides exist, with
    no compatibility fallback.

    Every selected scenario is validated before any build or paid call.
    """

    if config_path.is_symlink():
        raise ValueError("settings config must not be a symlink")
    for scenario_id in scenario_ids:
        _validate_scenario_id(scenario_id)
    for scenario_id in discovered_ids:
        _validate_scenario_id(scenario_id)
    selected = set(scenario_ids)
    if len(selected) != len(scenario_ids):
        raise ValueError("scenario IDs must be unique")
    discovered = set(discovered_ids)
    if not selected <= discovered:
        raise ValueError("selected scenario IDs must be discovered scenario IDs")
    unknown_settings = sorted(set(scenario_settings) - selected)
    if unknown_settings:
        raise ValueError(
            "settings overrides given for non-selected scenario(s): "
            + ", ".join(unknown_settings)
        )
    defaults_path = config_path
    if not defaults_path.is_file():
        raise ValueError(
            f"missing required settings config file {config_path.name}; "
            f"create the private shared config from {CONFIG_NAME}"
            f"{EXAMPLE_SUFFIX} (init-config creates it with mode 600); a "
            "scenario without a settings table is not a fallback and there "
            "is no retired single-file fallback"
        )
    defaults_text, defaults_digest = _read_private_settings(defaults_path)
    defaults_data = _parse_source_tables(defaults_text, CONFIG_NAME)
    loaded: dict[str, ScenarioSettings] = {}
    for scenario_id in scenario_ids:
        raw_overrides = scenario_settings.get(scenario_id)
        overrides = (
            validate_scenario_overrides(
                raw_overrides, f"scenario {scenario_id} TOML settings"
            )
            if raw_overrides is not None
            else {}
        )
        merged = _merge_tables(defaults_data, overrides)
        try:
            settings = _settings_from_data(merged)
        except ValueError as error:
            raise ValueError(
                f"scenario {scenario_id} TOML settings merged over "
                f"{CONFIG_NAME} is invalid: {error}"
            ) from None
        loaded[scenario_id] = ScenarioSettings(
            scenario_id=scenario_id,
            settings=settings,
            defaults_source=defaults_path,
            defaults_sha256=defaults_digest,
        )
    return loaded


def initialize(config_path: Path) -> Path:
    """Create the owner-only shared config file, never overwriting.

    The credential-free ``config.toml.example`` shipped next to the target
    is the template. Overrides live in each version-controlled scenario
    config's ``settings`` table, and only the private shared config holds
    credentials. There is no legacy single-file fallback.
    """

    if config_path.is_symlink():
        raise ValueError("settings config path must not be a symlink")
    if not config_path.parent.is_dir():
        raise ValueError("settings config directory must be a real directory")
    defaults_example = config_path.with_name(config_path.name + EXAMPLE_SUFFIX)
    defaults_target = config_path
    if defaults_example.is_symlink() or not defaults_example.is_file():
        raise ValueError(f"missing {defaults_example.name} template")
    if defaults_target.exists():
        raise ValueError(
            f"{defaults_target.name} already exists, refusing to overwrite"
        )
    try:
        fd = os.open(defaults_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(
            f"{defaults_target.name} already exists, refusing to overwrite"
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(defaults_example.read_text(encoding="utf-8"))
    return defaults_target


def profile_config(settings: ProviderSettings) -> str:
    provider_type = (
        "open-router" if settings.provider == "openrouter" else settings.provider
    )
    lines = [
        "[providers.worker_acceptance]",
        "type = " + json.dumps(provider_type),
        "base_url = " + json.dumps(settings.url),
        "default_model = " + json.dumps(settings.model_for("coordinator")),
        "api_key_env = " + json.dumps(KEY_ENV),
        "auth = " + json.dumps(settings.auth),
        "requires_api_key = true",
        "model_catalog = false",
    ]
    if settings.auth_header:
        lines.append("auth_header = " + json.dumps(settings.auth_header))
    return "\n".join(lines) + "\n"
