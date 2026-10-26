"""Authoritative interpretation of catalog metadata and validation commands."""
from dataclasses import dataclass
import re
import shlex

SECTIONS = (
    "Patch intent:", "Why it exists:", "Upstream integration points:",
    "Update guidance:", "Validation:",
)
HEADERS = ("Intent", "Kind", "Depends-On", "File")


@dataclass(frozen=True)
class Metadata:
    intent: str
    kind: str
    dependencies: tuple[str, ...]
    filename: str | None
    commands: tuple[str, ...]


def header(text: str, name: str) -> str:
    values = re.findall(rf"^{re.escape(name)}:[ \t]*(.*)$", text, re.MULTILINE)
    if len(values) != 1 or not values[0].strip():
        raise ValueError(f"must have exactly one non-empty {name}: header; found {len(values)}")
    return values[0].strip()


def parse(text: str, *, require_filename: bool = True) -> Metadata:
    """Parse a source message or the message portion of a generated mail patch."""
    text = text.partition("\n---\n")[0]
    values = {key: header(text, f"X-Jcode-Patch-{key}") for key in HEADERS[:3]}
    file_values = re.findall(r"^X-Jcode-Patch-File:[ \t]*(.*)$", text, re.MULTILINE)
    filename = header(text, "X-Jcode-Patch-File") if file_values or require_filename else None
    positions = [text.find(section) for section in SECTIONS]
    for section in SECTIONS:
        if text.count(section) != 1:
            raise ValueError(f"must have exactly one {section}")
    if positions != sorted(positions):
        raise ValueError("body sections out of order")
    for index, position in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(text)
        if not text[position + len(SECTIONS[index]):end].strip():
            raise ValueError(f"empty {SECTIONS[index]}")
    commands = tuple(line for line in text[positions[-1] + len(SECTIONS[-1]):].splitlines() if line.strip())
    if any(line != line.lstrip() or line.startswith(("-", "*", "`")) for line in commands):
        raise ValueError("invalid validation command")
    dependency_text = values["Depends-On"]
    dependencies = () if dependency_text == "none" else tuple(item.strip() for item in dependency_text.split(","))
    if any(not name for name in dependencies) or len(set(dependencies)) != len(dependencies):
        raise ValueError("invalid or duplicate dependency")
    return Metadata(values["Intent"], values["Kind"], dependencies, filename, commands)


def is_rust_test(command: str) -> bool:
    """Recognize both supported Rust launch forms, not incidental string prefixes."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return len(argv) >= 2 and argv[0] in {"cargo", "scripts/cargo_exec.sh"} and argv[1] == "test"
