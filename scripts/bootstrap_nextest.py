#!/usr/bin/env python3
"""Download and cache the repository's pinned cargo-nextest binary."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.9.143"
RELEASES_URL = "https://get.nexte.st/releases.json"
USER_AGENT = "jcode-nextest-bootstrap/1.0"


def target_name() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if system == "Linux" and machine in {"x86_64", "aarch64"}:
        libc = platform.libc_ver()[0].lower()
        if not libc:
            try:
                result = subprocess.run(["ldd", "--version"], capture_output=True, text=True)
                libc = result.stdout.lower() + result.stderr.lower()
            except OSError:
                libc = ""
        suffix = "musl" if "musl" in libc else "gnu"
        return f"{machine}-unknown-linux-{suffix}"
    if system == "Darwin" and machine in {"x86_64", "aarch64"}:
        return "universal-apple-darwin"
    if system == "Windows" and machine in {"x86_64", "aarch64"}:
        return f"{machine}-pc-windows-msvc"
    raise RuntimeError(f"unsupported cargo-nextest platform: {system} {platform.machine()}")


def releases() -> object:
    request = urllib.request.Request(RELEASES_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def find_asset(document: object, target: str) -> tuple[str, str]:
    if isinstance(document, dict) and "projects" in document:
        project = document["projects"].get("cargo-nextest", {})
        version = project.get("ranges", {}).get("0.9", {}).get("versions", {}).get(VERSION, {})
        candidates = version.get("locations", [])
    else:
        candidates = document if isinstance(document, list) else document.get("releases", [])  # type: ignore[union-attr]
    for release in candidates:
        if not isinstance(release, dict) or ("version" in release and str(release.get("version")) != VERSION):
            continue
        assets = release.get("assets", [release])
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("target") != target:
                continue
            url = asset.get("url") or asset.get("download_url")
            checksums = asset.get("checksums", {})
            digest = asset.get("sha256") or asset.get("checksum") or checksums.get("sha256")
            if isinstance(url, str) and isinstance(digest, str):
                return url, digest.removeprefix("sha256:").lower()
    raise RuntimeError(f"no cargo-nextest {VERSION} asset for {target}")


def safe_extract(data: bytes, destination: Path, executable: str) -> None:
    stream = io.BytesIO(data)
    archive: tarfile.TarFile | zipfile.ZipFile
    if data[:2] == b"PK":
        archive = zipfile.ZipFile(stream)
        members = archive.infolist()
        for member in members:
            name = Path(member.filename)
            if name.name != executable or name.is_absolute() or ".." in name.parts:
                continue
            if member.is_dir() or stat.S_IFMT(member.external_attr >> 16) == stat.S_IFLNK:
                raise RuntimeError("refusing unsafe cargo-nextest archive member")
            destination.write_bytes(archive.read(member))
            return
    else:
        archive = tarfile.open(fileobj=stream, mode="r:*")
        for member in archive.getmembers():
            name = Path(member.name)
            if name.name != executable or name.is_absolute() or ".." in name.parts:
                continue
            if not member.isfile():
                raise RuntimeError("refusing unsafe cargo-nextest archive member")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("cargo-nextest archive member unreadable")
            destination.write_bytes(source.read())
            return
    raise RuntimeError("cargo-nextest executable missing from archive")


def main() -> int:
    try:
        target = target_name()
        executable = "cargo-nextest.exe" if platform.system() == "Windows" else "cargo-nextest"
        root = Path(__file__).resolve().parent.parent
        cached = root / ".tools" / "nextest" / VERSION / target / executable
        if not cached.exists():
            url, expected = find_asset(releases(), target)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise RuntimeError(f"cargo-nextest checksum mismatch: expected {expected}, got {actual}")
            cached.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as temp:
                temporary = Path(temp.name)
            try:
                safe_extract(data, temporary, executable)
                if platform.system() != "Windows":
                    temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                temporary.replace(cached)
            finally:
                temporary.unlink(missing_ok=True)
        version = subprocess.run([str(cached), "--version"], capture_output=True, text=True, check=True).stdout
        if VERSION not in version:
            raise RuntimeError(f"cached cargo-nextest reports unexpected version: {version.strip()}")
        print(cached)
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"bootstrap-nextest: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
