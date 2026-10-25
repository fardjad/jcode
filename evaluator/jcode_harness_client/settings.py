"""Owner-only settings for explicitly authorized live evaluations."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PRIVATE_NAME = ".eval-settings.toml"
EXAMPLE_NAME = ".eval-settings.example.toml"
KEY_ENV = "JCODE_ACCEPTANCE_API_KEY"
PROVIDERS = {
    "openai-compatible",
    "anthropic-compatible",
    "openrouter",
    "open-router",
}
ROLES = ("coordinator", "swarm", "judge", "simulator")
_LIMITS = {
    "turn_timeout_seconds",
    "swarm_timeout_minutes",
    "acceptance_timeout_seconds",
    "judge_max_calls",
    "judge_page_bytes",
}

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
swarm_timeout_minutes = 10
acceptance_timeout_seconds = 300
# Positive cost guard; exhaustion before complete coverage is incomplete.
judge_max_calls = 32
# Maximum evidence bytes per judge request, not a transcript truncation limit.
judge_page_bytes = 120000
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
    swarm_timeout_minutes: int = 10
    acceptance_timeout_seconds: int = 300
    judge_max_calls: int = 32
    judge_page_bytes: int = 120_000


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

    def model_for(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError("unknown evaluation model role")
        return str(getattr(self.models, role)) if self.models else self.model


def parse_settings(text: str) -> ProviderSettings:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Parser errors can quote secret-bearing source lines.
        raise ValueError(
            "invalid evaluation settings TOML, check syntax without sharing secrets"
        ) from None
    if set(data) - {"provider", "models", "limits"} or not isinstance(
        data.get("provider"), dict
    ):
        raise ValueError(
            "settings require [provider] with optional [models] and [limits]"
        )
    models = data.get("models", {})
    limits = data.get("limits", {})
    if not isinstance(models, dict) or set(models) - set(ROLES):
        raise ValueError("models support coordinator, swarm, judge, and simulator only")
    if not isinstance(limits, dict) or set(limits) - _LIMITS:
        raise ValueError("unsupported evaluation limits field")
    for key, value in limits.items():
        if type(value) is not int or value < 1:
            raise ValueError(f"evaluation {key} must be a positive integer")
    provider = data["provider"]
    if set(provider) - {"type", "url", "model", "api_key", "auth", "auth_header"}:
        raise ValueError("unsupported evaluation settings field")
    for key in ("type", "url", "model", "api_key"):
        if not isinstance(provider.get(key), str) or not provider[key].strip():
            raise ValueError(f"evaluation {key} is required")
        if any(ord(character) < 32 for character in provider[key]):
            raise ValueError(f"control characters forbidden in {key}")
    if provider["type"] not in PROVIDERS:
        raise ValueError(
            "provider type must be openai-compatible, "
            "anthropic-compatible, or openrouter"
        )
    try:
        url = urlsplit(provider["url"])
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
    auth = provider.get("auth", "bearer")
    header = provider.get("auth_header")
    if auth not in {"bearer", "header"}:
        raise ValueError("auth must be bearer or header")
    if auth == "header" and (
        not isinstance(header, str)
        or not header
        or any(not (character.isalnum() or character == "-") for character in header)
    ):
        raise ValueError("header authentication requires a valid auth_header name")
    if auth == "bearer" and header is not None:
        raise ValueError("auth_header is only valid for header authentication")
    if provider["api_key"].strip() == "REPLACE_WITH_YOUR_API_KEY":
        raise ValueError("replace the example API key before making live calls")
    resolved: dict[str, str] = {}
    for role in ROLES:
        value = models.get(role, provider["model"])
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
        provider["type"],
        provider["url"],
        provider["model"],
        provider["api_key"],
        auth,
        header,
        EvaluationModels(**resolved),
        EvaluationLimits(**limits),
    )


def load_settings(path: Path) -> ProviderSettings:
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
    return parse_settings(text)


def initialize(root: Path, example: Path | None = None) -> Path:
    path = root / PRIVATE_NAME
    source = example or (root / EXAMPLE_NAME)
    content = (
        source.read_text(encoding="utf-8") if source.is_file() else DEFAULT_EXAMPLE
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(
            "private settings already exist, refusing to overwrite"
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


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
