"""Consistent secret redaction and owner-only evidence writes."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Redactor:
    secrets: tuple[str, ...] = field(repr=False, default=())

    def text(self, value: str) -> str:
        for secret in self.secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def data(self, value: bytes) -> bytes:
        for secret in self.secrets:
            if secret:
                value = value.replace(secret.encode(), b"[REDACTED]")
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {
                self.text(str(key)): self.value(item) for key, item in value.items()
            }
        return value


def write_text(path: Path, text: str, redactor: Redactor | None = None) -> None:
    _write_bytes(path, (redactor.text(text) if redactor else text).encode())


def write_json(
    path: Path,
    value: Any,
    redactor: Redactor | None = None,
    *,
    indent: int | None = 2,
) -> None:
    sanitized = redactor.value(value) if redactor else value
    text = json.dumps(sanitized, indent=indent, ensure_ascii=False) + "\n"
    _write_bytes(path, text.encode())


def write_bytes(path: Path, value: bytes, redactor: Redactor | None = None) -> None:
    _write_bytes(path, redactor.data(value) if redactor else value)


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
