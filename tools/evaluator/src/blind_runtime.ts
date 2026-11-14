import { constants } from "node:fs";
import { access, chmod, copyFile, lstat, mkdir, mkdtemp, readdir, realpath, rm, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DANGEROUS_FILES, getDangerousDirectories } from "@anthropic-ai/sandbox-runtime/dist/sandbox/sandbox-utils.js";

export interface BlindRuntimeOptions {
  stagingRoot: string;
  binary: string;
  bridge: string;
  /** SDK-owned control home, separate from the sandbox's jcode home. */
  sdkHome: string;
  /** Existing staged policy/plugin files or directories to keep read-only. */
  readonlyPaths?: string[];
  /** Provider API hostnames permitted inside the coordinator sandbox. */
  allowedDomains?: string[];
}

function contains(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel === "" || (!rel.startsWith("../") && rel !== ".." && !isAbsolute(rel));
}

function quote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

async function realDirectory(path: string, role: string): Promise<string> {
  if (!isAbsolute(path) || resolve(path) !== path) throw new Error(`${role} must be an absolute normalized path`);
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink() || await realpath(path) !== path) {
    throw new Error(`${role} must be a real directory without symlink components: ${path}`);
  }
  return path;
}

async function directory(path: string): Promise<void> {
  try {
    await mkdir(path, { mode: 0o700 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
  }
  await realDirectory(path, "sandbox directory");
}

async function absent(path: string): Promise<boolean> {
  try { await lstat(path); return false; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return true;
    throw error;
  }
}

const sandboxNode = Bun.which("node");

function sandboxNodePath(): string {
  if (!sandboxNode) throw new Error("Node.js is required to launch sandbox-runtime");
  return sandboxNode;
}

function sandboxRuntimeCli(): string {
  const modulePath = fileURLToPath(import.meta.resolve("@anthropic-ai/sandbox-runtime"));
  return join(dirname(modulePath), "cli.js");
}

function ripgrepPath(): string {
  const path = Bun.which("rg");
  if (!path) throw new Error("ripgrep (rg) is required by sandbox-runtime for protected-file checks");
  return resolve(path);
}

function settings(
  ripgrep: string,
  allowedDomains: string[],
  allowRead: string[],
  allowWrite: string[],
  denyWrite: string[],
  allowAllUnixSockets = false,
): string {
  return JSON.stringify({
    network: { allowedDomains, deniedDomains: [], strictAllowlist: true, ...(allowAllUnixSockets ? { allowAllUnixSockets: true } : {}) },
    filesystem: {
      denyRead: ["/", "/var", "/tmp", "/sys"],
      allowRead: [...new Set(["/usr", "/etc", "/bin", "/lib", "/lib64", "/sbin", dirname(dirname(sandboxRuntimeCli())), ...allowRead])],
      allowWrite: [...new Set(allowWrite)],
      denyWrite: [...new Set(denyWrite)],
    },
    ripgrep: { command: ripgrep },
  });
}

async function writeSettings(path: string, content: string): Promise<void> {
  await writeFile(path, content, { mode: 0o600, flag: "wx" });
}

async function runSrt(
  settingsPath: string,
  argv: string[],
  cwd: string,
  env: Record<string, string>,
  timeoutMs: number,
): Promise<{ exitCode: number; stdout: string; stderr: string; timedOut: boolean }> {
  const child = Bun.spawn([sandboxNodePath(), sandboxRuntimeCli(), "--settings", settingsPath, "--", ...argv], {
    cwd, env, stdin: "ignore", stdout: "pipe", stderr: "pipe",
  });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; child.kill("SIGKILL"); }, timeoutMs);
  try {
    const [exitCode, stdout, stderr] = await Promise.all([
      child.exited, new Response(child.stdout).text(), new Response(child.stderr).text(),
    ]);
    return { exitCode, stdout, stderr, timedOut };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Stage independent executable copies and provide the SDK a sandboxed launcher.
 * The SDK control home stays outside the daemon home because daemon PIDs belong
 * to the sandbox namespace. Only its API socket directory is exposed.
 */
export async function prepareBlindRuntime(options: BlindRuntimeOptions): Promise<{ binary: string }> {
  const root = await realDirectory(options.stagingRoot, "staging root");
  if (!contains("/var/tmp", root) || root === "/var/tmp") {
    throw new Error("blind staging root must be a private directory under /var/tmp");
  }
  const rootInfo = await lstat(root);
  if (rootInfo.uid !== process.getuid?.() || (rootInfo.mode & 0o077) !== 0) throw new Error("staging root must be owned by the caller and private");
  const readonlyPaths = options.readonlyPaths ?? [];
  for (const path of readonlyPaths) {
    if (!isAbsolute(path) || resolve(path) !== path || path === root || !contains(root, path)) {
      throw new Error(`read-only path must be strictly inside the staging root: ${path}`);
    }
    const info = await lstat(path);
    if ((!info.isFile() && !info.isDirectory()) || info.isSymbolicLink() || await realpath(path) !== path) {
      throw new Error(`read-only path must be a real file or directory without symlinks: ${path}`);
    }
  }
  const sdkHome = await realDirectory(options.sdkHome, "SDK control home");
  if (contains(root, sdkHome) || contains(sdkHome, root)) throw new Error("SDK control home must be separate from the staging tree");
  if ((!contains("/tmp", sdkHome) && !contains("/var/tmp", sdkHome)) || ["/tmp", "/var/tmp"].includes(sdkHome)) {
    throw new Error("SDK control home must be a private directory under /tmp or /var/tmp");
  }
  const sdkInfo = await lstat(sdkHome);
  if (sdkInfo.uid !== process.getuid?.() || (sdkInfo.mode & 0o077) !== 0) throw new Error("SDK control home must be owned by the caller and private");
  if (!await absent(join(sdkHome, "servers.json"))) throw new Error("SDK control home must not contain a server registry");
  const sdkRun = join(sdkHome, "run");
  await directory(sdkRun);
  if ((await readdir(sdkRun)).length) throw new Error("SDK control run directory must be empty");
  if (Buffer.byteLength(join(sdkRun, "jcode-api.sock")) >= 104) throw new Error("SDK API socket path is too long");
  if (process.platform !== "linux") throw new Error("blind runtime requires Linux");

  const bin = join(root, "bin");
  const workspace = join(root, "workspace");
  const jcodeHome = join(root, "jcode-home");
  const runtime = join(root, "runtime");
  const settingsPath = join(runtime, "sandbox-settings.json");
  for (const dir of [bin, workspace, jcodeHome, runtime, join(root, "home"), join(root, "xdg-config"),
    join(root, "xdg-cache"), join(root, "xdg-data"), join(root, "xdg-state"), join(root, "cargo-target"), join(jcodeHome, "run")]) {
    await directory(dir);
  }
  // SRT masks absent protected paths with device files, which break workspace
  // copies. Existing regular placeholders receive the same read-only protection.
  for (const name of DANGEROUS_FILES) {
    try { await writeFile(join(workspace, name), "", { flag: "wx", mode: 0o600 }); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
  }
  for (const name of getDangerousDirectories()) {
    const parts = name.split("/");
    let path = workspace;
    for (let index = 0; index < parts.length; index++) {
      path = join(path, parts[index]!);
      try { await mkdir(path); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        const info = await lstat(path);
        if (info.isSymbolicLink() || (!info.isDirectory() && index < parts.length - 1)) throw new Error(`unsafe protected path: ${path}`);
      }
    }
  }
  if (Buffer.byteLength(join(jcodeHome, "run", "jcode-debug.sock")) >= 104) throw new Error("daemon socket path is too long");

  for (const [source, name] of [[options.binary, "jcode"], [options.bridge, "jcode-harness-api-bridge"]] as const) {
    const info = await lstat(source);
    if (!info.isFile() && !info.isSymbolicLink()) throw new Error(`native executable is not a file: ${source}`);
    await access(source, constants.R_OK);
    const destination = join(bin, name);
    await copyFile(source, destination, constants.COPYFILE_EXCL);
    await chmod(destination, 0o755);
  }

  const lockedPaths = [...readonlyPaths, bin, settingsPath];
  const allowedDomains = [...new Set(options.allowedDomains ?? [])];
  const sandboxSettings = settings(ripgrepPath(), allowedDomains, [root, sdkRun], [root, sdkRun], lockedPaths, true);
  await writeSettings(settingsPath, sandboxSettings);
  for (const path of lockedPaths) {
    const info = await lstat(path);
    if ((!info.isFile() && !info.isDirectory()) || info.isSymbolicLink() || await realpath(path) !== path) {
      throw new Error(`read-only path must be a real file or directory without symlinks: ${path}`);
    }
  }

  const env = {
    PATH: `${bin}:/usr/bin:/bin`, HOME: join(root, "home"),
    JCODE_HOME: jcodeHome, JCODE_RUNTIME_DIR: join(jcodeHome, "run"),
    JCODE_SOCKET: join(jcodeHome, "run", "jcode.sock"), JCODE_API_SOCKET: join(sdkRun, "jcode-api.sock"),
    XDG_CONFIG_HOME: join(root, "xdg-config"), XDG_CACHE_HOME: join(root, "xdg-cache"),
    XDG_DATA_HOME: join(root, "xdg-data"), XDG_STATE_HOME: join(root, "xdg-state"),
    CARGO_TARGET_DIR: join(root, "cargo-target"), TMPDIR: runtime, TMP: runtime, TEMP: runtime,
    LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", JCODE_NO_AUTO_UPDATE: "1",
  };
  const forwarded = ["JCODE_ACCEPTANCE_API_KEY", "JCODE_SWARM_MODEL", "JCODE_SWARM_ENABLED", "JCODE_SWARM_SPAWN_MODE", "JCODE_WAKE_MODE"];
  const optional = forwarded.map(key => `if [ "\${${key}+set}" = set ]; then env_args+=("${key}=\${${key}}"); fi`).join("\n");
  // The isolated network cannot reach host loopback or private networks directly.
  const source = `#!/bin/bash\ncd ${quote(workspace)} || exit 126\nenv_args=()\n${optional}\nexec /usr/bin/env -i ${Object.entries(env).map(([key, value]) => quote(`${key}=${value}`)).join(" ")} "\${env_args[@]}" ${quote(sandboxNodePath())} ${quote(sandboxRuntimeCli())} --settings ${quote(settingsPath)} -- /usr/bin/env -u NO_PROXY -u no_proxy ${quote(join(bin, "jcode"))} "$@"\n`;
  const wrapper = join(bin, "blind-jcode");
  await writeFile(wrapper, source, { mode: 0o755, flag: "wx" });
  await chmod(wrapper, 0o755);

  const probeEnv = { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: join(root, "home"), TMPDIR: runtime, LANG: "C.UTF-8" };
  const probe = await runSrt(settingsPath, ["/usr/bin/true"], workspace, probeEnv, 30_000);
  if (probe.timedOut || probe.exitCode !== 0) {
    throw new Error(`sandbox-runtime cannot establish blind sandbox (exit ${probe.exitCode}${probe.timedOut ? ", timed out" : ""}): ${probe.stderr.trim()}`);
  }
  return { binary: wrapper };
}

export interface BlindCommandOptions {
  /** A disposable workspace; no other part of its parent tree is mounted. */
  workspace: string;
  argv: string[];
  timeoutMs: number;
  /** Copy a read-only snapshot into sandbox-private tmpfs before running. */
  copyWorkspace?: boolean;
}

/** Run untrusted acceptance code without SDK state, credentials or network access. */
export async function runBlindCommand(options: BlindCommandOptions): Promise<{
  exitCode: number;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}> {
  if (!options.argv.length || !options.argv[0]) throw new Error("acceptance command must not be empty");
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0) throw new Error("acceptance timeout must be positive");
  const workspace = await realDirectory(options.workspace, "acceptance workspace");
  if (!contains("/var/tmp", workspace) || workspace === "/var/tmp") {
    throw new Error("acceptance workspace must be a private directory under /var/tmp");
  }
  const settingsRoot = await mkdtemp("/var/tmp/jcode-accept-settings-");
  try {
    const settingsPath = join(settingsRoot, "sandbox-settings.json");
    const launchDir = options.copyWorkspace ? join(settingsRoot, "launch") : workspace;
    if (options.copyWorkspace) await mkdir(launchDir);
    await writeSettings(settingsPath, settings(ripgrepPath(), [], [workspace, launchDir], [launchDir], []));
    const python = Bun.spawnSync(["/usr/bin/python3", "-I", "-c", "import os,sys; print(os.pathsep.join(p for p in sys.path if p.startswith('/usr/')))"], {
      env: { PATH: "/usr/bin:/bin" }, stdout: "pipe", stderr: "pipe",
    });
    const pythonPath = python.exitCode === 0 ? python.stdout.toString().trim() : "";
    if (python.exitCode === 0 && !pythonPath) throw new Error("cannot determine trusted Python standard library paths");
    const env = {
      PATH: "/usr/bin:/bin", HOME: "/tmp", TMPDIR: "/tmp", LANG: "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1", PYTHONSAFEPATH: "1",
      ...(pythonPath ? { PYTHONPATH: `${pythonPath}:${workspace}` } : {}),
    };
    const argv = options.copyWorkspace
      ? ["/bin/sh", "-c", 'work=$(mktemp -d /tmp/jcode-accept-XXXXXXXX) || exit 1; cp -a -- "$1/." "$work/" || exit 1; shift; cd "$work" || exit 1; if [ "${PYTHONPATH+x}" ]; then export PYTHONPATH="${PYTHONPATH%:*}:$work"; fi; exec "$@"', "jcode-accept", workspace, ...options.argv]
      : options.argv;
    return await runSrt(settingsPath, argv, launchDir, env, options.timeoutMs);
  } finally {
    await rm(settingsRoot, { recursive: true, force: true });
  }
}
