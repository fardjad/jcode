"""Bubblewrap blind-runtime command construction for evaluator harness runs.

This module builds, but never launches, the sandbox command prefix used to
run evaluator workloads with a blind runtime: the sandbox sees a minimal
read-only system, a fresh PID namespace, and only explicitly staged public
paths. Launching a real provider session is out of scope; the broker owns
process lifecycle and parent-PID authentication.

Exact mount and environment interface (for the next broker integration):

* Root: ``bwrap --tmpfs /``. The sandbox root is empty; no host directory,
  and never the host root, is bound across. ``/home``, ``/run``, ``/root``,
  and every other private host path are absent rather than mounted.
* System, read-only: ``--ro-bind /usr /usr`` and ``--ro-bind /etc /etc``
  (TLS certificates and ``/etc/resolv.conf`` DNS stay usable). ``/bin``,
  ``/lib``, ``/lib64``, and ``/sbin`` are bound read-only when they are real
  directories and recreated with ``--symlink`` when the host uses merged
  ``/usr`` layout.
* Fresh namespaces: ``--unshare-pid`` with ``--proc /proc`` (host processes
  and their environments are invisible), ``--dev /dev`` for a new device
  tree, and ``--tmpfs /tmp``. Host networking is preserved: no network
  namespace is created.
* Public staging: the caller stages native binaries (the jcode binary and
  bridge), workspace, config, and runtime state under one public host root
  such as ``/var/tmp/<run>/``. The ``bin``, ``workspace``, ``config``, and
  ``runtime`` subdirectories are the only read-write binds and are mounted
  at their identical absolute host paths, with ``--dir`` entries for their
  parent directories. The sandbox HOME is ``<root>/runtime/home``.
* DNS: ``/etc/resolv.conf`` on the host is frequently a symlink into the
  hidden ``/run`` tree (systemd-resolved, NetworkManager). When it is a
  symlink to a ``/run/`` target, the planner snapshots its content into
  ``<root>/config/resolv.conf`` and the builder mounts that snapshot
  read-only at the symlink's target path inside the otherwise absent
  ``/run`` tree. Only this synthetic file appears under ``/run``; user
  sockets and the rest of the host ``/run`` stay hidden.
* Optional read-only caches: explicit non-private toolchain cache paths are
  exposed read-only at their identical absolute paths.
* Environment: rebuilt from scratch. PATH is only ``<root>/bin:/usr/bin:/bin``
  plus the staged binaries; evaluator virtualenvs, source trees, and the
  inherited PATH never leak. DBus, XDG runtime, SSH, GPG, jcode, and
  toolchain-home variables are scrubbed. ``TERM`` and ``LC_*``/``LANG`` pass
  through. Callers may add explicit extra variables unless scrubbed.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BWRAP_EXECUTABLE",
    "HIDDEN_ROOT_PREFIXES",
    "MINIMUM_PYTHON",
    "PASSTHROUGH_ENV_KEYS",
    "PUBLIC_DIR_ENV_KEYS",
    "SCRUBBED_ENV_KEYS",
    "STAGING_SUBDIRS",
    "SYSTEM_RO_DIRS",
    "USRMERGE_SYMLINK_NAMES",
    "BlindCommand",
    "StagingPlan",
    "build_blind_argv",
    "build_blind_command",
    "build_blind_env",
    "find_child_pid",
    "find_group_member",
    "is_public_path",
    "plan_staging",
    "public_staging_base",
    "public_dirs_env",
    "stage_files",
    "system_python",
]

BWRAP_EXECUTABLE = "bwrap"

# Minimal read-only system mounts. /etc deliberately stays read-only visible
# so TLS trust roots and DNS resolution keep working; it contains normal
# system configuration, not the invoking user's private configuration.
SYSTEM_RO_DIRS = ("/usr", "/etc")

# Top-level directories that are bound read-only when real, or recreated as
# symlinks on merged-/usr hosts where they point into /usr.
USRMERGE_SYMLINK_NAMES = ("bin", "lib", "lib64", "sbin")

# Host paths that must never be staged, bound, or exposed inside the sandbox.
HIDDEN_ROOT_PREFIXES = ("/home", "/root", "/run", "/proc", "/sys", "/dev", "/tmp")

# System trees that a read-write staging root must not sit inside or on top
# of. /tmp is rejected because the sandbox overlays it with tmpfs.
_SYSTEM_ROOT_PREFIXES = ("/usr", "/etc", "/bin", "/lib", "/lib64", "/sbin", "/boot")
_SYSTEM_ROOT_EXACT = ("/", "/var", "/usr", "/etc", "/opt", "/srv", "/mnt", "/media")

# The only read-write binds inside the sandbox, created under the public
# staging root and mounted at their identical absolute paths.
STAGING_SUBDIRS = ("bin", "workspace", "config", "runtime")

# Environment variables that must never cross the sandbox boundary: user
# session sockets and runtimes, agent sockets, and toolchain or jcode homes
# that could reveal the invoking user's private setup. Explicit toolchain
# caches are exposed through read-only mounts, not inherited variables.
SCRUBBED_ENV_KEYS = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "DBUS_SYSTEM_BUS_ADDRESS",
        "DBUS_STARTER_ADDRESS",
        "DBUS_STARTER_BUS_TYPE",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GPG_TTY",
        "GNUPGHOME",
        "GPG_AGENT_INFO",
        "JCODE_HOME",
        "JCODE_CONFIG",
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PATH",
        "WORKON_HOME",
        "PYTHONPATH",
        "PYTHONHOME",
        "CARGO_HOME",
        "RUSTUP_HOME",
    }
)

# Harmless presentation variables copied from the invoking environment when
# present. Everything else is dropped.
PASSTHROUGH_ENV_KEYS = ("TERM", "LANG", "LC_ALL")

# The only directory-pointing variables that may cross the boundary, and
# only through the validated ``public_dirs`` channel of
# :func:`build_blind_env`: every value must resolve inside the public staging
# root. They stay scrubbed for the free-form ``extra_env`` channel, so a
# caller can never smuggle a private host path or a global custom value in
# through an unvalidated variable.
PUBLIC_DIR_ENV_KEYS = (
    "HOME",
    "TMPDIR",
    "JCODE_HOME",
    "JCODE_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)

# The plain system Python exposed inside the sandbox. Tooling that needs a
# different interpreter must stage it into the public bin directory.
MINIMUM_PYTHON = (3, 14)

_PYTHON_NAME_PATTERN = re.compile(r"^python3\.(\d+)$")


@dataclass(frozen=True)
class StagingPlan:
    """Public host paths staged for one blind sandbox run.

    ``root`` is a public (non-private) host directory such as
    ``/var/tmp/<run>/``. ``dirs`` are the four read-write mounts, exposed
    inside the sandbox at their identical absolute paths. ``files`` are the
    native binaries (typically the caller's jcode binary and bridge) that
    :func:`stage_files` copies into ``root/bin``. ``home`` is the sandbox
    HOME directory under the read-write runtime mount.
    """

    root: Path
    dirs: tuple[Path, ...]
    files: tuple[tuple[Path, Path], ...]
    resolv_snapshot: Path | None = None
    resolv_mount_path: Path | None = None

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def home(self) -> Path:
        return self.runtime / "home"


@dataclass(frozen=True)
class BlindCommand:
    """A ready-to-launch sandbox command prefix plus its public environment.

    The caller appends its own command and passes ``env`` verbatim to the
    subprocess: ``[*command.argv, *inner_argv]`` with ``env=command.env``.
    This module never launches the sandbox or any provider.
    """

    argv: tuple[str, ...]
    env: dict[str, str]
    staging: StagingPlan
    python: str

    def command(self, inner_argv: Sequence[str]) -> list[str]:
        """Return the full argv: bwrap prefix plus the inner command."""
        return [*self.argv, *inner_argv]


def is_public_path(path: Path | str) -> bool:
    """True when ``path`` may be staged or mounted into a blind sandbox.

    Private user trees (/home, /root, /run), kernel and device trees, and
    paths shadowed by the sandbox tmpfs (/tmp) are rejected, as are the
    read-only system trees and their top-level system containers.
    """
    real = Path(os.path.realpath(str(path)))
    text = str(real)
    if not real.is_absolute():
        return False
    for prefix in (*HIDDEN_ROOT_PREFIXES, *_SYSTEM_ROOT_PREFIXES):
        if text == prefix or text.startswith(f"{prefix}/"):
            return False
    if text in _SYSTEM_ROOT_EXACT:
        return False
    return True


def _require_public(path: Path | str, role: str) -> Path:
    candidate = Path(str(path))
    if not candidate.is_absolute():
        raise ValueError(f"{role} must be an absolute path: {candidate}")
    if not is_public_path(candidate):
        raise ValueError(f"{role} is not a public sandbox path: {candidate}")
    return Path(os.path.realpath(str(candidate)))


def public_staging_base() -> Path:
    """Return a writable public directory suitable for a staging root.

    Standard public temporary locations are preferred. When none is both
    writable and public the caller must fail closed rather than fall back to
    a private path.
    """
    candidates = [Path("/var/tmp"), Path(tempfile.gettempdir())]
    for candidate in candidates:
        try:
            if (
                candidate.is_dir()
                and os.access(candidate, os.W_OK)
                and is_public_path(candidate)
            ):
                return candidate
        except OSError:
            continue
    raise RuntimeError("no writable public staging base is available")


def public_dirs_env(
    staging: StagingPlan,
    dirs: Mapping[str, str],
) -> dict[str, str]:
    """Validate and return directory variables that must live in the staging root.

    Only :data:`PUBLIC_DIR_ENV_KEYS` names are accepted, and every value must
    resolve (after symlink resolution) inside ``staging.root``. Anything else
    raises: there is no global custom-environment channel for these keys.
    """
    root = Path(os.path.realpath(str(staging.root)))
    validated: dict[str, str] = {}
    for key, value in dirs.items():
        if key not in PUBLIC_DIR_ENV_KEYS:
            raise ValueError(f"not a public sandbox dir variable: {key}")
        if not isinstance(value, str) or not value:
            raise ValueError(f"public sandbox dir variable is empty: {key}")
        resolved = Path(os.path.realpath(value))
        if resolved != root and not str(resolved).startswith(f"{root}/"):
            raise ValueError(
                f"{key} must point inside the public staging root: {value}"
            )
        validated[key] = value
    return validated


def _proc_cmdline(pid: int) -> tuple[str, ...] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return tuple(
        part.decode("utf-8", "surrogateescape") for part in raw.split(b"\x00") if part
    )


def _proc_ppid(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            fields = line.split()
            if len(fields) < 2:
                return None
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def find_child_pid(
    parent_pid: int,
    command: Sequence[str],
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> int:
    """Return the host PID of ``parent_pid``'s child running exactly ``command``.

    Host-side only. Processes inside a bwrap PID namespace keep their host
    PIDs and parent links in the host ``/proc`` tree, so a trusted native
    child can be located by walking known children and matching the exact
    launch argv. No arbitrary peer is ever accepted: a child that does not
    match the exact command is invisible to this lookup. Raises ``TimeoutError``
    when no such child appears within ``timeout`` seconds (fail closed).
    """
    expected = tuple(str(part) for part in command)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            entries = os.listdir("/proc")
        except OSError:
            entries = []
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if _proc_ppid(pid) != parent_pid:
                continue
            if _proc_cmdline(pid) == expected:
                return pid
        time.sleep(poll_interval)
    raise TimeoutError(
        f"no child of {parent_pid} with the expected command appeared in time"
    )


def find_group_member(
    group_pid: int,
    command: Sequence[str],
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> int:
    """Return the host PID of a process group member running exactly ``command``.

    bwrap forks an intermediate process, so the sandbox payload is a
    grandchild of the launched bwrap PID; the whole sandbox nevertheless
    stays in bwrap's own process group (``start_new_session``). Discovery is
    therefore scoped to that group and to the exact launch argv: no other
    host process can match, and no arbitrary peer is accepted. Raises
    ``TimeoutError`` when no such member appears in time (fail closed).
    """
    expected = tuple(str(part) for part in command)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            entries = os.listdir("/proc")
        except OSError:
            entries = []
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                if os.getpgid(pid) != group_pid:
                    continue
            except OSError:
                continue
            if _proc_cmdline(pid) == expected:
                return pid
        time.sleep(poll_interval)
    raise TimeoutError(
        f"no member of process group {group_pid} with the expected command"
        " appeared in time"
    )


def plan_staging(
    root: Path | str,
    *,
    jcode_binary: Path | str | None = None,
    bridge: Path | str | None = None,
) -> StagingPlan:
    """Create and describe the public staging tree under ``root``.

    Creates ``bin``, ``workspace``, ``config``, ``runtime``, and
    ``runtime/home``. Native binaries given as ``jcode_binary`` and
    ``bridge`` are recorded for :func:`stage_files` to copy into ``bin`` as
    ``jcode`` and ``bridge``; nothing is copied here.
    """
    public_root = _require_public(root, "staging root")
    files: list[tuple[Path, Path]] = []
    if jcode_binary is not None:
        source = Path(str(jcode_binary))
        if not source.is_file():
            raise FileNotFoundError(f"jcode binary not found: {source}")
        files.append((source, public_root / "bin" / "jcode"))
    if bridge is not None:
        source = Path(str(bridge))
        if not source.is_file():
            raise FileNotFoundError(f"bridge not found: {source}")
        files.append((source, public_root / "bin" / "bridge"))
    for name in STAGING_SUBDIRS:
        (public_root / name).mkdir(parents=True, exist_ok=True)
    (public_root / "runtime" / "home").mkdir(parents=True, exist_ok=True)
    resolv_snapshot, resolv_mount_path = _snapshot_resolv_conf(public_root)
    dirs = tuple(public_root / name for name in STAGING_SUBDIRS)
    return StagingPlan(
        root=public_root,
        dirs=dirs,
        files=tuple(files),
        resolv_snapshot=resolv_snapshot,
        resolv_mount_path=resolv_mount_path,
    )


def _snapshot_resolv_conf(
    public_root: Path,
) -> tuple[Path | None, Path | None]:
    """Snapshot a symlinked host resolv.conf into the public config tree.

    Hosts using systemd-resolved or NetworkManager point
    ``/etc/resolv.conf`` at a file under the hidden ``/run`` tree. The
    symlink crosses into the sandbox (``/etc`` is read-only visible), but its
    target does not, which would break DNS. Reading the system resolver
    configuration is not a private-path probe: the content is copied into
    the public staging tree and mounted back at the symlink target later.
    """
    host_resolv = Path("/etc/resolv.conf")
    if not host_resolv.is_symlink():
        return None, None
    target = Path(os.path.realpath(str(host_resolv)))
    if not str(target).startswith("/run/"):
        return None, None
    try:
        content = host_resolv.read_text()
    except OSError:
        return None, None
    snapshot = public_root / "config" / "resolv.conf"
    snapshot.write_text(content)
    return snapshot, target


def stage_files(plan: StagingPlan) -> tuple[Path, ...]:
    """Copy the planned native binaries into the public ``bin`` directory.

    Staged binaries are made executable. The copies live only in the public
    staging tree; the sources are left untouched.
    """
    staged: list[Path] = []
    for source, destination in plan.files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(0o755)
        staged.append(destination)
    return tuple(staged)


def system_python(minimum: tuple[int, int] = MINIMUM_PYTHON) -> str:
    """Return a plain system Python interpreter usable inside the sandbox.

    Only public system locations are considered. The highest
    ``/usr/bin/python3.N`` at or above ``minimum`` wins; plain
    ``/usr/bin/python3`` is accepted when no numbered interpreter exists.
    No private or virtualenv interpreter is ever returned.
    """
    usr_bin = Path("/usr/bin")
    best: tuple[int, int] | None = None
    if usr_bin.is_dir():
        for entry in usr_bin.iterdir():
            match = _PYTHON_NAME_PATTERN.match(entry.name)
            if match is None:
                continue
            version = (3, int(match.group(1)))
            if version < minimum:
                continue
            if best is None or version > best:
                best = version
        if best is not None:
            return str(usr_bin / f"python3.{best[1]}")
        fallback = usr_bin / "python3"
        if fallback.exists():
            return str(fallback)
    raise RuntimeError(f"no system Python >= {minimum[0]}.{minimum[1]} under /usr/bin")


def _bind_pairs(argv: Sequence[str], flag: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(argv):
        if item == flag and index + 2 < len(argv):
            pairs.append((argv[index + 1], argv[index + 2]))
    return pairs


def _ancestor_dirs(destinations: Sequence[Path]) -> list[Path]:
    """Ordered ``--dir`` entries for parents of the bind destinations."""
    covered = {str(path) for path in destinations}
    dirs: dict[str, Path] = {}
    for destination in destinations:
        for parent in destination.parents:
            text = str(parent)
            if text == "/" or text in covered or text in dirs:
                continue
            dirs[text] = parent
    return [dirs[text] for text in sorted(dirs, key=lambda t: t.count("/"))]


def build_blind_argv(
    staging: StagingPlan,
    *,
    ro_cache_paths: Sequence[Path | str] = (),
    chdir: bool = True,
    bwrap_executable: str = BWRAP_EXECUTABLE,
) -> tuple[str, ...]:
    """Return the bwrap argv prefix implementing the blind mount interface.

    The prefix ends with the bwrap options; the caller appends the inner
    command. Read-write mounts are limited to the staged public ``bin``,
    ``workspace``, ``config``, and ``runtime`` directories at their
    identical absolute paths. The host root, /home, /run, and every other
    private host tree are never mounted. Host networking stays available.
    """
    root = _require_public(staging.root, "staging root")
    argv: list[str] = [bwrap_executable, "--tmpfs", "/"]
    for system_dir in SYSTEM_RO_DIRS:
        if not Path(system_dir).is_dir():
            raise FileNotFoundError(f"required system directory missing: {system_dir}")
        argv += ["--ro-bind", system_dir, system_dir]
    for name in USRMERGE_SYMLINK_NAMES:
        path = Path("/") / name
        if path.is_symlink():
            argv += ["--symlink", os.readlink(path), f"/{name}"]
        elif path.is_dir():
            argv += ["--ro-bind", str(path), str(path)]
    argv += [
        "--unshare-pid",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--die-with-parent",
    ]
    caches = [
        _require_public(cache, "read-only cache path") for cache in ro_cache_paths
    ]
    for cache in caches:
        if not cache.is_dir():
            raise FileNotFoundError(f"read-only cache path missing: {cache}")
    mounts = [*staging.dirs, *caches]
    for directory in _ancestor_dirs(mounts):
        argv += ["--dir", str(directory)]
    for directory in staging.dirs:
        if not directory.is_dir():
            raise FileNotFoundError(f"staged public directory missing: {directory}")
        argv += ["--bind", str(directory), str(directory)]
    for cache in caches:
        argv += ["--ro-bind", str(cache), str(cache)]
    # DNS snapshot: mounted at the symlink target so /etc/resolv.conf
    # resolves inside without exposing any real content under /run.
    if (
        staging.resolv_snapshot is not None
        and staging.resolv_mount_path is not None
        and staging.resolv_snapshot.is_file()
    ):
        for directory in _ancestor_dirs([staging.resolv_mount_path]):
            argv += ["--dir", str(directory)]
        argv += [
            "--ro-bind",
            str(staging.resolv_snapshot),
            str(staging.resolv_mount_path),
        ]
    if chdir:
        argv += ["--chdir", str(root / "workspace")]
    return tuple(argv)


def build_blind_env(
    staging: StagingPlan,
    *,
    host_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    public_dirs: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the scrubbed environment for processes inside the sandbox.

    The environment is rebuilt from scratch: PATH contains only the staged
    public ``bin`` directory and public system paths, HOME points into the
    read-write runtime mount, and TMPDIR is the sandbox tmpfs. Inherited
    user sockets, runtimes, agent variables, and toolchain or jcode homes
    never cross the boundary. ``extra_env`` entries are applied last and
    may not reintroduce scrubbed variables. ``public_dirs`` is the only
    authoritative channel for directory variables such as ``HOME`` and
    ``JCODE_HOME``: each entry must be a :data:`PUBLIC_DIR_ENV_KEYS` name
    whose value resolves inside the public staging root (see
    :func:`public_dirs_env`), not a global custom environment value.
    """
    env: dict[str, str] = {
        "PATH": ":".join([str(staging.bin), "/usr/bin", "/bin"]),
        "HOME": str(staging.home),
        "TMPDIR": "/tmp",
    }
    if host_env is not None:
        for key in PASSTHROUGH_ENV_KEYS:
            value = host_env.get(key)
            if value:
                env[key] = value
    if extra_env is not None:
        for key, value in extra_env.items():
            if key in SCRUBBED_ENV_KEYS:
                raise ValueError(f"refusing to pass scrubbed variable: {key}")
            env[key] = value
    if public_dirs is not None:
        for key, value in public_dirs_env(staging, public_dirs).items():
            env[key] = value
    return env


def build_blind_command(
    staging: StagingPlan,
    *,
    ro_cache_paths: Sequence[Path | str] = (),
    host_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    public_dirs: Mapping[str, str] | None = None,
    chdir: bool = True,
    bwrap_executable: str = BWRAP_EXECUTABLE,
) -> BlindCommand:
    """Return the complete blind sandbox launch description.

    The result carries the bwrap argv prefix, the scrubbed environment, the
    public staging plan, and the plain system Python available inside. It
    never launches anything; the broker owns process lifecycle.
    """
    argv = build_blind_argv(
        staging,
        ro_cache_paths=ro_cache_paths,
        chdir=chdir,
        bwrap_executable=bwrap_executable,
    )
    env = build_blind_env(
        staging,
        host_env=host_env,
        extra_env=extra_env,
        public_dirs=public_dirs,
    )
    return BlindCommand(
        argv=argv,
        env=env,
        staging=staging,
        python=system_python(),
    )
