import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { createReadStream } from "node:fs";
import { mkdir, open, readFile, realpath, stat, unlink } from "node:fs/promises";
import { basename, join, resolve } from "node:path";
import { gitExecutable, withIsolatedEnv } from "./isolation";
import { discoverPatches } from "./patches";
import { atomicWrite, withFileLock } from "./primitives";
import { ensurePinnedSdk, resolveBase } from "./sdk";
import { prepareWorktree } from "./worktree";

const targets = [
  { package: "jcode", binary: "jcode" },
  { package: "jcode-harness-api-server", binary: "jcode-harness-api-bridge" },
];
const build = {
  profile: "debug",
  cargo_profile: "dev",
  targets,
  helper: { path: "scripts/cargo_exec.sh", contract_version: 1 },
  cargo_args: ["build", "--profile", "dev", ...targets.flatMap(({ package: pkg, binary }) => ["--package", pkg, "--bin", binary])],
};
type Inputs = {
  schema: number;
  base: string;
  patches: { name: string; sha256: string }[];
  build: typeof build;
  toolchain: { rustc_vv: string; host: string };
};
type Source = { worktree: string; head: string; tree: string };
type Binary = { path: string; sha256: string };
type Stamp = {
  schema: number; input_schema: number; status: string; fingerprint: string;
  base: string; patches: Inputs["patches"]; build: typeof build; toolchain: Inputs["toolchain"];
  created: number; source: Source; binaries: Record<string, Binary>;
  command: string[]; build_log: string; build_status: string;
};
export type WorkerBuildProvenance = Stamp & {
  stamp: string; action: "built" | "reused"; built: boolean; reused: boolean;
  binary_paths: Record<string, string>;
};

function evaluationDir(root: string): string { return join(root, ".tools", "worker-eval"); }
function targetDir(root: string): string { return join(root, ".tools", "cargo-target", "worker-eval"); }
function binaryPaths(root: string): Record<string, string> {
  const suffix = process.platform === "win32" ? ".exe" : "";
  return Object.fromEntries(targets.map(({ binary }) => [binary, join(targetDir(root), "debug", binary + suffix)]));
}

async function hashFile(path: string): Promise<string> {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk as Buffer);
  return hash.digest("hex");
}

// Match the sorted JSON representation used by the original fingerprint format.
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(", ")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => `${canonical(key)}: ${canonical(item)}`).join(", ")}}`;
  }
  const encoded = JSON.stringify(value);
  return encoded === undefined ? "undefined" : encoded.replace(/[\u007f-\uffff]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
}
function same(a: unknown, b: unknown): boolean { return canonical(a) === canonical(b); }
async function writeJson(path: string, value: unknown): Promise<void> { await atomicWrite(path, `${JSON.stringify(value, null, 2)}\n`); }

async function capture(command: string[], cwd: string, env: Record<string, string>): Promise<string> {
  const child = Bun.spawn(command, { cwd, env, stdout: "pipe", stderr: "pipe" });
  const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  if (code !== 0) throw new Error(`${command.join(" ")} failed with exit status ${code}: ${(err || out).trim()}`);
  return out.trim();
}
async function git(cwd: string, ...args: string[]): Promise<string> {
  // Respect an already isolated parent; otherwise ignore user git configuration.
  return withIsolatedEnv((env) => capture([gitExecutable(cwd, env), ...args], cwd, env));
}
async function currentInputs(root: string): Promise<Inputs> {
  const patches = (await discoverPatches(root)).slice().sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  const base = resolveBase(root);
  const records = await Promise.all(patches.map(async (patch) => ({ name: basename(patch.path), sha256: await hashFile(patch.path) })));
  const rustc_vv = await withIsolatedEnv((env) => capture(["rustc", "-Vv"], root, env));
  const host = rustc_vv.split(/\r?\n/).find((line) => line.startsWith("host:"))?.slice(5).trim();
  if (!rustc_vv || !host) throw new Error("rustc -Vv did not report a host target");
  return { schema: 1, base, patches: records, build, toolchain: { rustc_vv, host } };
}
async function sourceState(path: string): Promise<Source> {
  const worktree = await realpath(path);
  const status = await git(worktree, "status", "--porcelain");
  if (status) throw new Error(`worker evaluation source is dirty: ${worktree}\n${status}`);
  return { worktree, head: await git(worktree, "rev-parse", "HEAD"), tree: await git(worktree, "rev-parse", "HEAD^{tree}") };
}
function identity(inputs: Inputs, hash: string) {
  return { schema: 1, input_schema: 1, status: "success", fingerprint: hash, base: inputs.base, patches: inputs.patches, build: inputs.build, toolchain: inputs.toolchain };
}
async function validStamp(root: string, hash: string, inputs: Inputs): Promise<Stamp | undefined> {
  const dir = join(evaluationDir(root), "builds", hash);
  try {
    const stamp: Stamp = JSON.parse(await readFile(join(dir, "success.json"), "utf8"));
    for (const [key, value] of Object.entries(identity(inputs, hash))) {
      if (!same((stamp as unknown as Record<string, unknown>)[key], value)) return undefined;
    }
    const worktree = join(evaluationDir(root), "worktree");
    if (stamp.source.worktree !== await realpath(worktree) || !same(stamp.source, await sourceState(worktree))) return undefined;
    const paths = binaryPaths(root);
    if (!same(Object.keys(stamp.binaries).sort(), Object.keys(paths).sort())) return undefined;
    for (const [name, path] of Object.entries(paths)) {
      if (stamp.binaries[name].path !== path || !(await stat(path)).isFile() || stamp.binaries[name].sha256 !== await hashFile(path)) return undefined;
    }
    if (stamp.build_log !== join(dir, "build.log") || !(await stat(stamp.build_log)).isFile() || stamp.build_status !== join(dir, "build-status.json")) return undefined;
    const status = JSON.parse(await readFile(stamp.build_status, "utf8"));
    if (status.fingerprint !== hash || status.status !== "success" || status.exit_code !== 0) return undefined;
    return stamp;
  } catch { return undefined; }
}
function provenance(stamp: Stamp, path: string, action: "built" | "reused"): WorkerBuildProvenance {
  return { ...stamp, stamp: path, action, built: action === "built", reused: action === "reused", binary_paths: Object.fromEntries(Object.entries(stamp.binaries).map(([name, record]) => [name, record.path])) };
}

type Attempt = {
  schema: number; fingerprint: string; status: string; started: number; finished: number | null;
  pid: number | null; runner_pid: number; exit_code: number | null; command: string[];
  cwd: string; log: string; status_path: string; rejection?: string; source?: Source; binaries?: Record<string, Binary>;
};

// Keep the lock until a spawned process and its children have stopped on interruption.
async function runOwned(command: string[], cwd: string, env: Record<string, string>, stdout: "inherit" | number, lockFd?: number, onStart?: (pid: number) => Promise<void>): Promise<{ code: number; interrupted: boolean }> {
  const child = spawn(command[0], command.slice(1), {
    cwd, env, stdio: lockFd === undefined ? ["inherit", stdout, stdout] : ["inherit", stdout, stdout, lockFd], detached: process.platform !== "win32",
  });
  let interrupted = false;
  let escalation: NodeJS.Timeout | undefined;
  const signalChild = (signal: NodeJS.Signals) => {
    if (child.pid) {
      try { process.kill(process.platform === "win32" ? child.pid : -child.pid, signal); } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
      }
    }
  };
  const stop = () => {
    interrupted = true;
    signalChild("SIGTERM");
    escalation ??= setTimeout(() => signalChild("SIGKILL"), 5000);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  const { promise: exited, resolve: resolveExit, reject } = Promise.withResolvers<number>();
  child.once("error", reject);
  child.once("close", (code, signal) => resolveExit(code ?? (signal === "SIGINT" ? 130 : 143)));
  try {
    if (child.pid && onStart) {
      try { await onStart(child.pid); } catch (error) { stop(); await exited.catch(() => undefined); throw error; }
    }
    return { code: await exited, interrupted };
  } finally {
    clearTimeout(escalation);
    process.off("SIGINT", stop);
    process.off("SIGTERM", stop);
  }
}

async function prepareLocked(root: string, lockFd: number): Promise<WorkerBuildProvenance> {
  const inputs = await currentInputs(root);
  const hash = createHash("sha256").update(canonical(inputs)).digest("hex");
  const dir = join(evaluationDir(root), "builds", hash);
  const stampPath = join(dir, "success.json");
  const prior = await validStamp(root, hash, inputs);
  if (prior) {
    if (!same(await currentInputs(root), inputs)) throw new Error("catalog or toolchain changed while validating worker evaluation build");
    return provenance(prior, stampPath, "reused");
  }
  await unlink(stampPath).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; });
  const patches = (await discoverPatches(root)).slice().sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  const source = await prepareWorktree(root, "worker-evaluation", inputs.base, patches, join(evaluationDir(root), "worktree"), [lockFd]);
  const initial = await sourceState(source);
  if (!same(await currentInputs(root), inputs)) throw new Error("catalog or toolchain changed during source materialization");
  await mkdir(targetDir(root), { recursive: true });
  await mkdir(dir, { recursive: true });
  const logPath = join(dir, "build.log");
  const statusPath = join(dir, "build-status.json");
  const command = ["bash", join(source, build.helper.path), ...build.cargo_args];
  const attempt: Attempt = { schema: 1, fingerprint: hash, status: "running", started: Date.now() / 1000, finished: null, pid: null, runner_pid: process.pid, exit_code: null, command, cwd: source, log: logPath, status_path: statusPath };
  await writeJson(statusPath, attempt);
  const log = await open(logPath, "w", 0o600);
  try {
    await log.write(`${JSON.stringify({ fingerprint: hash, command, cwd: source, cargo_target_dir: targetDir(root) })}\n`);
    try {
      const result = await withIsolatedEnv((env) => runOwned(command, source, { ...env, CARGO_TARGET_DIR: targetDir(root), JCODE_CATALOG_ROOT: root }, log.fd, lockFd, async (pid) => { attempt.pid = pid; await writeJson(statusPath, attempt); }));
      attempt.exit_code = result.code;
      attempt.status = result.interrupted ? "interrupted" : result.code === 0 ? "built" : "failed";
    } catch (error) {
      attempt.exit_code = 127;
      attempt.status = "failed";
      await log.write(`build launch failed: ${String(error)}\n`);
    }
    attempt.finished = Date.now() / 1000;
    await log.write(`worker-eval-build-status: ${attempt.status}\nworker-eval-exit-status: ${attempt.exit_code}\n`);
    await log.sync();
  } finally { await log.close(); }
  await writeJson(statusPath, attempt);
  if (attempt.status !== "built") throw new Error(`worker evaluation build ${attempt.status} with exit status ${attempt.exit_code}; log=${logPath}; status=${statusPath}`);
  let final: Source;
  const binaries: Record<string, Binary> = {};
  try {
    if (!same(await currentInputs(root), inputs)) throw new Error("catalog or toolchain changed during worker evaluation build");
    final = await sourceState(source);
    if (!same(final, initial)) throw new Error("worker evaluation source HEAD/tree changed during build");
    for (const [name, path] of Object.entries(binaryPaths(root))) {
      if (!(await stat(path)).isFile()) throw new Error(`worker evaluation binary is not a file: ${path}`);
      binaries[name] = { path, sha256: await hashFile(path) };
    }
  } catch (error) {
    attempt.status = "rejected";
    attempt.rejection = String(error);
    attempt.finished = Date.now() / 1000;
    await writeJson(statusPath, attempt);
    const rejectedLog = await open(logPath, "a");
    try { await rejectedLog.write(`worker-eval-build-rejected: ${attempt.rejection}\n`); await rejectedLog.sync(); } finally { await rejectedLog.close(); }
    throw error;
  }
  attempt.status = "success";
  attempt.source = final;
  attempt.binaries = binaries;
  await writeJson(statusPath, attempt);
  const stamp: Stamp = { ...identity(inputs, hash), created: Date.now() / 1000, source: final, binaries, command, build_log: logPath, build_status: statusPath };
  await writeJson(stampPath, stamp);
  return provenance(stamp, stampPath, "built");
}

/** Build or reuse binaries for the exact pinned base and current patch bytes. */
export async function prepareWorkerEvalBuild(root: string): Promise<WorkerBuildProvenance> {
  root = resolve(root);
  return withFileLock(join(evaluationDir(root), "build.lock"), (fd) => prepareLocked(root, fd));
}

/** Validate settings before building, then hold the build lock through the live run. */
export async function runWorkerEvaluation(root: string, scenario?: string): Promise<void> {
  root = resolve(root);
  const cli = join(root, "tools", "evaluator", "src", "cli.ts");
  const evals = join(root, "customizations", "workers", "evals");
  const config = join(evals, "config.toml");
  const scenarios = join(evals, "scenarios");
  const selection = scenario === undefined ? [] : ["--scenario", scenario];
  await ensurePinnedSdk(root);
  const preflight = [process.execPath, cli, "validate-settings", "--config", config, "--scenarios", scenarios, ...selection];
  const checked = await withIsolatedEnv((env) => runOwned(preflight, root, env, "inherit"));
  if (checked.code !== 0 || checked.interrupted) throw new Error(`settings preflight failed with exit status ${checked.code}; no build was started`);
  const runId = `worker-eval-${new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z")}-${process.pid}-${randomUUID()}`;
  const output = join(evals, "results", runId);
  const provenancePath = join(evaluationDir(root), "provenance", `${runId}.json`);
  await withFileLock(join(evaluationDir(root), "build.lock"), async (fd) => {
    const start = performance.now();
    const result = await prepareLocked(root, fd);
    await writeJson(provenancePath, { ...result, build_check_seconds: (performance.now() - start) / 1000 });
    console.error(`native build ${result.action}: ${result.fingerprint}`);
    const command = [process.execPath, cli, "run", "--binary", result.binary_paths.jcode, "--bridge", result.binary_paths["jcode-harness-api-bridge"], "--config", config, "--scenarios", scenarios, "--blueprints", join(root, "customizations", "workers", "blueprints"), "--plugins", join(root, "customizations", "plugins"), "--build-provenance", provenancePath, "--output", output, ...selection];
    const evaluation = await withIsolatedEnv((env) => runOwned(command, root, env, "inherit", fd));
    if (evaluation.code !== 0 || evaluation.interrupted) throw new Error(`worker evaluation failed with exit status ${evaluation.code}; evidence=${output}`);
  });
  console.error(`evaluation evidence: ${output}`);
}
