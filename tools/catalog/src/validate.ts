import { createHash } from "node:crypto";
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import { basename, join, resolve } from "node:path";
import { withIsolatedEnv } from "./isolation";
import { atomicWrite, withFileLock } from "./primitives";
import { resolveBase } from "./sdk";
import { dependencyOrder, discoverPatches, checkCatalog, type Patch } from "./patches";
import { prepareWorktree } from "./worktree";
import { exclusionsPath, loadExclusions, NEXTEST_VERSION, runCommand, runNextest, saveExclusions, type Output } from "./nextest";
import { isRustTest } from "./metadata";

function shellWords(command: string): string[] {
  const words: string[] = [];
  let word = "", quote = "", started = false;
  for (let i = 0; i < command.length; i++) {
    const char = command[i]!;
    if (!quote && /\s/.test(char)) {
      if (started) { words.push(word); word = ""; started = false; }
    } else if (char === "\\" && quote !== "'") {
      if (++i >= command.length) throw new Error(`invalid command quoting: ${command}`);
      word += command[i]!; started = true;
    } else if ((char === "'" || char === '"') && (!quote || quote === char)) {
      quote = quote ? "" : char; started = true;
    } else { word += char; started = true; }
  }
  if (quote) throw new Error(`invalid command quoting: ${command}`);
  if (started) words.push(word);
  return words;
}

export function parseFailedTests(output: string): string[] {
  return [...new Set([...output.replace(/\x1b\[[0-9;]*m/g, "").matchAll(/^test (\S+) \.\.\. FAILED\s*$/gm)].map(match => match[1]!))].sort();
}

export function executedTests(output: string): boolean {
  return [...output.replace(/\x1b\[[0-9;]*m/g, "").matchAll(/^test result: \S+\. (\d+) passed; (\d+) failed;/gm)]
    .some(match => Number(match[1]) + Number(match[2]) > 0);
}

export function appendSkips(command: string, exclusions: string[]): string {
  const args = shellWords(command);
  const delimiter = args.indexOf("--");
  const selected = new Set<string>();
  if (delimiter !== -1 && args.slice(delimiter + 1).includes("--exact")) {
    const valueOptions = new Set(["-p", "--package", "--exclude", "--manifest-path", "--features", "--target", "--target-dir", "--profile", "-j", "--jobs", "--color", "--config", "--lockfile-path", "--bin", "--example", "--test", "--bench", "--message-format", "--artifact-dir", "--skip", "--test-threads", "--logfile", "--format", "--shuffle-seed"]);
    let skip = false;
    for (const arg of args.slice(2)) {
      if (skip) { skip = false; continue; }
      if (valueOptions.has(arg)) { skip = true; continue; }
      if (!arg.startsWith("-") && arg.includes("::")) selected.add(arg);
    }
  }
  const remaining = exclusions.filter(name => !selected.has(name));
  const quote = (value: string) => `'${value.replace(/'/g, "'\\''")}'`;
  return command + (remaining.length ? `${delimiter < 0 ? " --" : ""}${remaining.map(name => ` --skip ${quote(name)}`).join("")}` : "");
}

function git(worktree: string, args: string[]): string {
  const result = Bun.spawnSync(["git", ...args], { cwd: worktree, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${result.stderr.toString().trim()}`);
  return result.stdout.toString().trim();
}

function assertClean(worktree: string, base?: string): void {
  if (git(worktree, ["status", "--porcelain", "--untracked-files=no"])) throw new Error(`validation requires clean tracked source: ${worktree}`);
  if (base && git(worktree, ["rev-parse", "HEAD"]) !== base) throw new Error(`upstream learning requires clean base ${base}: ${worktree}`);
}

export async function runValidations(root: string, worktree: string, base: string, patches: Patch[], env: Record<string, string>, mode: "test" | "learn", output?: Output, lockFds?: number[]): Promise<string[]> {
  const exclusions = mode === "learn" ? [] : (await loadExclusions(root, base))?.failed_validation_tests ?? [];
  const failures = new Set<string>();
  for (const patch of patches) {
    for (const declared of patch.metadata.commands) {
      const rust = isRustTest(declared);
      if (mode === "learn" && !rust) continue;
      const command = rust && mode === "test" ? appendSkips(declared, exclusions) : declared;
      const heading = `${mode === "learn" ? "learning" : "validating"} ${patch.name}: ${command}\n`;
      process.stdout.write(heading); output?.(heading);
      const result = await runCommand(["bash", "-c", command], { cwd: worktree, env, output, lockFds });
      if (mode === "learn") {
        for (const name of parseFailedTests(result.stdout)) failures.add(name);
      } else {
        if (result.code !== 0) throw new Error(`validation failed in ${patch.name}: ${command}`);
        if (rust && !executedTests(result.stdout)) throw new Error(`validation failed in ${patch.name}: ${command} (no tests executed)`);
      }
    }
  }
  return [...failures].sort();
}

async function perform(root: string, base: string, catalog: Patch[], target: Patch | undefined, compatEach: boolean, output?: Output, runnerFd?: number): Promise<void> {
  const purpose = "patch-validation";
  const cargoTarget = join(root, ".tools", "cargo-target", purpose);
  await withFileLock(join(cargoTarget, "catalog.lock"), fd => withIsolatedEnv(async env => {
    env.CARGO_TARGET_DIR = cargoTarget;
    env.JCODE_CATALOG_ROOT = root;
    const lockFds = runnerFd === undefined ? [fd] : [fd, runnerFd];
    const closure = target ? await dependencyOrder(root, target.name) : catalog;
    const worktree = await prepareWorktree(root, "patch-validation", base, closure, undefined, lockFds);
    assertClean(worktree);
    const checked = await runCommand(["cargo", "check", "--workspace", "--tests"], { cwd: worktree, env, output, lockFds });
    if (checked.code !== 0) throw new Error(`cargo check failed in ${worktree}`);
    await runValidations(root, worktree, base, target ? [target] : catalog, env, "test", output, lockFds);
    if (!target || compatEach) await runNextest(root, worktree, base, "test", env, output, lockFds);
  }));
}

export async function validateStack(root: string): Promise<void> {
  root = resolve(root);
  await checkCatalog(root);
  await perform(root, resolveBase(root), await discoverPatches(root), undefined, true);
}

function selectPatch(root: string, catalog: Patch[], value: string): Patch {
  const name = basename(value);
  if (value !== name && value !== `patches/${name}` && resolve(value) !== resolve(root, "patches", name)) throw new Error(`patch must be under patches: ${value}`);
  const patch = catalog.find(item => item.name === name);
  if (!patch) throw new Error(`unknown patch: ${value}`);
  return patch;
}

export async function testPatch(root: string, patch: string, options: { compatEach?: boolean } = {}): Promise<void> {
  root = resolve(root);
  await checkCatalog(root);
  const catalog = await discoverPatches(root);
  await perform(root, resolveBase(root), catalog, selectPatch(root, catalog, patch), options.compatEach ?? false);
}

function fileHash(path: string): string | null {
  return existsSync(path) ? createHash("sha256").update(readFileSync(path)).digest("hex") : null;
}

export async function validationIdentity(root: string, name: string, base: string): Promise<string> {
  const closure = await dependencyOrder(root, name);
  const rustc = await withIsolatedEnv(async env => {
    const result = Bun.spawnSync(["rustc", "-Vv"], { cwd: root, env, stdout: "pipe", stderr: "pipe" });
    if (result.exitCode !== 0) throw new Error(`rustc -Vv failed: ${result.stderr.toString().trim()}`);
    return result.stdout.toString().trim();
  });
  const identity = { schema: 2, base, patches: closure.map(patch => [patch.name, fileHash(patch.path)]), rustc, exclusions: fileHash(exclusionsPath(root, base)), nextest: NEXTEST_VERSION, config: fileHash(join(root, "nextest.toml")) };
  return createHash("sha256").update(JSON.stringify(identity)).digest("hex");
}

type Result = { status: "pass" | "fail" | "running" | "stale"; fingerprint: string; blob?: string | null; compat_each?: boolean; log?: string; started?: number; finished?: number };
type Results = Record<string, Result>;
async function loadResults(path: string): Promise<Results> {
  if (!existsSync(path)) return {};
  try {
    const data: unknown = JSON.parse(await readFile(path, "utf8"));
    if (!data || typeof data !== "object" || Array.isArray(data) || Object.values(data).some(value => !value || typeof value !== "object" || Array.isArray(value))) throw new Error("expected result records");
    return data as Results;
  } catch (error) { throw new Error(`invalid results ${path}`, { cause: error }); }
}

export function shouldSkip(record: Result | undefined, blob: string | null, fingerprint: string, options: { force?: boolean; compatEach?: boolean }): boolean {
  return !options.force && record?.status === "pass" && record.blob === blob && record.fingerprint === fingerprint && (!options.compatEach || record.compat_each === true);
}

export async function testAll(root: string, options: { only?: string[]; force?: boolean; compatEach?: boolean } = {}): Promise<void> {
  root = resolve(root);
  await withFileLock(join(root, ".tools", "patch-tests", "runner.lock"), async runnerFd => {
    const base = resolveBase(root);
    await checkCatalog(root);
    const catalog = await discoverPatches(root);
    const selected = options.only?.length ? [...new Set(options.only.map(name => selectPatch(root, catalog, name)))].sort((a, b) => a.name.localeCompare(b.name)) : catalog;
    const directory = join(root, ".tools", "patch-tests", base);
    await mkdir(directory, { recursive: true });
    const path = join(directory, "results.json"), results = await loadResults(path);
    const save = () => atomicWrite(path, `${JSON.stringify(results, null, 2)}\n`);
    const fingerprints = new Map(await Promise.all(catalog.map(async patch => [patch.name, await validationIdentity(root, patch.name, base)] as const)));
    const current = async () => {
      if (resolveBase(root) !== base) return false;
      const now = await discoverPatches(root);
      if (now.length !== catalog.length || now.some((patch, i) => patch.name !== catalog[i]?.name)) return false;
      for (const patch of now) if (await validationIdentity(root, patch.name, base) !== fingerprints.get(patch.name)) return false;
      return true;
    };
    if (!options.only?.length) {
      const fingerprint = createHash("sha256").update(JSON.stringify([...fingerprints])).digest("hex");
      if (options.force || results.__full_stack__?.status !== "pass" || results.__full_stack__.fingerprint !== fingerprint) {
        results.__full_stack__ = { status: "fail", fingerprint };
        await save();
        await perform(root, base, catalog, undefined, true, undefined, runnerFd);
        if (!await current()) throw new Error("full-stack inputs changed during validation, stop and rerun");
        results.__full_stack__.status = "pass";
        await save();
      }
    } else console.log("full-stack gate was not run (--only targeted run)");
    let failed = false;
    for (const [index, patch] of selected.entries()) {
      const fingerprint = fingerprints.get(patch.name)!;
      const blob = fileHash(patch.path);
      if (shouldSkip(results[patch.name], blob, fingerprint, options)) continue;
      if (resolveBase(root) !== base || await validationIdentity(root, patch.name, base) !== fingerprint) throw new Error("validation inputs changed before launch, stop and rerun");
      const log = join(directory, `${patch.name.replace(/\.patch$/, "")}.log`);
      writeFileSync(log, "");
      const record: Result = results[patch.name] = { status: "running", fingerprint, blob, compat_each: !!options.compatEach, log, started: Date.now() };
      await save();
      try {
        await perform(root, base, catalog, patch, !!options.compatEach, text => appendFileSync(log, text), runnerFd);
        record.status = "pass";
      } catch (error) {
        record.status = "fail"; failed = true;
        appendFileSync(log, `${String(error)}\n`);
        console.error(`${patch.name}: ${String(error)}`);
      }
      record.finished = Date.now();
      if (resolveBase(root) !== base || await validationIdentity(root, patch.name, base) !== fingerprint) { record.status = "stale"; failed = true; }
      await save();
      console.log(`JCODE_PROGRESS ${JSON.stringify({ current: index + 1, total: selected.length, message: `${patch.name} ${record.status}` })}`);
    }
    if (!await current()) throw new Error("validation inputs changed during run, stop and rerun");
    if (failed) throw new Error("one or more patch validations failed");
  });
}

export async function learnUpstream(root: string): Promise<void> {
  root = resolve(root);
  const base = resolveBase(root);
  const target = join(root, ".tools", "cargo-target", "upstream-learning");
  await withFileLock(join(target, "catalog.lock"), fd => withIsolatedEnv(async env => {
    env.CARGO_TARGET_DIR = target;
    env.JCODE_CATALOG_ROOT = root;
    const worktree = await prepareWorktree(root, "patch-learn", base, [], undefined, [fd]);
    assertClean(worktree, base);
    const nextest = await runNextest(root, worktree, base, "learn", env, undefined, [fd]);
    assertClean(worktree, base);
    const validation = await runValidations(root, worktree, base, await discoverPatches(root), env, "learn", undefined, [fd]);
    assertClean(worktree, base);
    if (resolveBase(root) !== base) throw new Error("upstream pin changed during learning");
    await saveExclusions(root, base, nextest, validation);
  }));
}
