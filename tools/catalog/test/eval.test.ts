import { afterEach, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, watch, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { prepareWorkerEvalBuild, runWorkerEvaluation } from "../src/eval";
import { withFileLock } from "../src/primitives";

const roots: string[] = [];
const originalPath = process.env.PATH;
const primitives = new URL("../src/primitives.ts", import.meta.url).pathname;

function git(root: string, ...args: string[]): string {
  const result = Bun.spawnSync(["git", ...args], { cwd: root, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")}: ${result.stderr.toString()}`);
  return result.stdout.toString();
}
function json(path: string): { status: string } { return JSON.parse(readFileSync(path, "utf8")); }
type Event = { args: string[]; locked: boolean; provenance: { action: string; build_check_seconds: number; binary_paths: Record<string, string> }; pid: number; home: string; jcodeHome: string; provider?: string };
function events(root: string): Event[] {
  const path = join(root, ".tools", "events.jsonl");
  return existsSync(path) ? readFileSync(path, "utf8").trim().split("\n").map(line => JSON.parse(line)) : [];
}

function fixture(): string {
  const root = mkdtempSync(join(tmpdir(), "worker-build-test-"));
  roots.push(root);
  git(root, "init", "-q");
  git(root, "config", "user.name", "Test");
  git(root, "config", "user.email", "test@example.invalid");
  mkdirSync(join(root, "scripts"), { recursive: true });
  mkdirSync(join(root, "sdk", "typescript", "src"), { recursive: true });
  writeFileSync(join(root, "sdk", "typescript", "package.json"), '{"name":"fixture-sdk"}\n');
  writeFileSync(join(root, "sdk", "typescript", "src", "index.ts"), "export {};\n");
  writeFileSync(join(root, "source.txt"), "upstream\n");
  writeFileSync(join(root, "scripts", "cargo_exec.sh"), `#!/bin/bash
set -eu
printf 'build\\n' >> "$JCODE_CATALOG_ROOT/.tools/build-count"
"$TEST_BUN" "$JCODE_CATALOG_ROOT/.tools/check-lock.ts"
if [ -f "$JCODE_CATALOG_ROOT/.tools/hold-build" ]; then exec "$TEST_BUN" "$JCODE_CATALOG_ROOT/.tools/hold.ts"; fi
if [ -f "$JCODE_CATALOG_ROOT/.tools/fail-build" ]; then echo 'fixture build failure'; exit 19; fi
if [ -f "$JCODE_CATALOG_ROOT/.tools/change-patch" ]; then echo 'changed' >> "$JCODE_CATALOG_ROOT/patches/0001-fixture.patch"; fi
if [ -f "$JCODE_CATALOG_ROOT/.tools/change-source" ]; then echo 'dirty' >> source.txt; fi
mkdir -p "$CARGO_TARGET_DIR/debug"
printf 'fixture jcode' > "$CARGO_TARGET_DIR/debug/jcode"
printf 'fixture bridge' > "$CARGO_TARGET_DIR/debug/jcode-harness-api-bridge"
`);
  git(root, "add", ".");
  git(root, "commit", "--no-gpg-sign", "-qm", "upstream");
  const base = git(root, "rev-parse", "HEAD").trim();
  git(root, "update-ref", "refs/jcode/upstream-tags/v1.0.0", base);
  writeFileSync(join(root, "VERSION.txt"), "v1.0.0\n");
  writeFileSync(join(root, "source.txt"), "patched\n");
  git(root, "add", "source.txt");
  git(root, "commit", "--no-gpg-sign", "-qm", `fixture patch

X-Jcode-Patch-Intent: fixture
X-Jcode-Patch-Depends-On: none
X-Jcode-Patch-File: 0001-fixture.patch

Patch intent:
fixture patch

Why it exists:
exercise worker build

Upstream integration points:
source.txt

Update guidance:
retain fixture

Validation:
cargo test fixture`);
  mkdirSync(join(root, "patches"));
  writeFileSync(join(root, "patches", "0001-fixture.patch"), git(root, "format-patch", "--no-signature", "--stdout", "-1"));
  writeFileSync(join(root, "second.txt"), "second patch\n");
  git(root, "add", "second.txt");
  git(root, "commit", "--no-gpg-sign", "-qm", `second patch

X-Jcode-Patch-Intent: second fixture
X-Jcode-Patch-Depends-On: 0001-fixture.patch
X-Jcode-Patch-File: 0002-second.patch

Patch intent:
second fixture

Why it exists:
exercise patch order

Upstream integration points:
second.txt

Update guidance:
retain order

Validation:
cargo test second`);
  writeFileSync(join(root, "patches", "0002-second.patch"), git(root, "format-patch", "--no-signature", "--stdout", "-1"));
  mkdirSync(join(root, ".tools"));
  mkdirSync(join(root, "fake-bin"));
  const rustc = join(root, "fake-bin", "rustc");
  writeFileSync(rustc, "#!/bin/sh\nprintf 'rustc 1.0.0 (fixture)\\nhost: fixture-host\\n'\n");
  chmodSync(rustc, 0o755);
  process.env.PATH = `${join(root, "fake-bin")}:${originalPath ?? ""}`;
  process.env.TEST_BUN = process.execPath;
  writeFileSync(join(root, ".tools", "check-lock.ts"), `import { withFileLock } from ${JSON.stringify(primitives)};
const root = process.env.JCODE_CATALOG_ROOT!;
try { await withFileLock(root + '/.tools/worker-eval/build.lock', async () => {}); process.exit(41); }
catch (error) { if (!String(error).includes('Lock already held')) throw error; }
`);
  writeFileSync(join(root, ".tools", "hold.ts"), `import { createServer } from 'node:net';
import { renameSync, writeFileSync } from 'node:fs';
const root = process.env.JCODE_CATALOG_ROOT!;
createServer().listen(0, '127.0.0.1');
writeFileSync(root + '/.tools/build-ready.tmp', String(process.pid));
renameSync(root + '/.tools/build-ready.tmp', root + '/.tools/build-ready');
await Promise.withResolvers<void>().promise;
`);
  const cli = join(root, "tools", "evaluator", "src", "cli.ts");
  mkdirSync(join(root, "tools", "evaluator", "src"), { recursive: true });
  writeFileSync(cli, `import { appendFileSync, existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { withFileLock } from ${JSON.stringify(primitives)};
const root = process.cwd(), args = process.argv.slice(2), mode = args[0];
let locked = false, provenance = null;
if (mode === 'run') {
  try { await withFileLock(root + '/.tools/worker-eval/build.lock', async () => {}); }
  catch (e) { if (!String(e).includes('Lock already held')) throw e; locked = true; }
  provenance = JSON.parse(readFileSync(args[args.indexOf('--build-provenance') + 1], 'utf8'));
}
appendFileSync(root + '/.tools/events.jsonl', JSON.stringify({args, locked, provenance, pid: process.pid, home: process.env.HOME, jcodeHome: process.env.JCODE_HOME, provider: process.env.JCODE_PROVIDER_TEST}) + '\\n');
if (mode === 'validate-settings' && existsSync(root + '/.tools/fail-preflight')) process.exit(7);
if (mode === 'run' && existsSync(root + '/.tools/fail-evaluation')) process.exit(9);
if (mode === 'run' && existsSync(root + '/.tools/hold-evaluation')) {
  createServer().listen(0, '127.0.0.1');
  writeFileSync(root + '/.tools/evaluation-ready.tmp', String(process.pid));
  renameSync(root + '/.tools/evaluation-ready.tmp', root + '/.tools/evaluation-ready');
  await Promise.withResolvers<void>().promise;
}
`);
  return root;
}

afterEach(() => {
  process.env.PATH = originalPath;
  delete process.env.TEST_BUN;
  delete process.env.JCODE_PROVIDER_TEST;
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

test("builds the complete dedicated patch stack, records exact provenance, and reuses only verified output", async () => {
  const root = fixture();
  mkdirSync(join(root, ".patched-jcode"));
  writeFileSync(join(root, ".patched-jcode", "decoy"), "not input");
  const first = await prepareWorkerEvalBuild(root);
  expect(first.action).toBe("built");
  expect(first.source.worktree).toBe(join(root, ".tools", "worker-eval", "worktree"));
  expect(readFileSync(join(first.source.worktree, "source.txt"), "utf8")).toBe("patched\n");
  expect(readFileSync(join(first.source.worktree, "second.txt"), "utf8")).toBe("second patch\n");
  expect(first.base).toBe(git(root, "rev-parse", "refs/jcode/upstream-tags/v1.0.0").trim());
  expect(first.patches).toEqual(["0001-fixture.patch", "0002-second.patch"].map(name => ({ name, sha256: createHash("sha256").update(readFileSync(join(root, "patches", name))).digest("hex") })));
  expect(first.toolchain).toEqual({ rustc_vv: "rustc 1.0.0 (fixture)\nhost: fixture-host", host: "fixture-host" });
  expect(first.build.cargo_args).toEqual(["build", "--profile", "dev", "--package", "jcode", "--bin", "jcode", "--package", "jcode-harness-api-server", "--bin", "jcode-harness-api-bridge"]);
  expect(first.binary_paths.jcode).toBe(join(root, ".tools", "cargo-target", "worker-eval", "debug", "jcode"));
  expect(first.binaries.jcode.sha256).toBe(createHash("sha256").update("fixture jcode").digest("hex"));
  expect(json(first.build_status).status).toBe("success");
  expect(readFileSync(first.build_log, "utf8")).toContain("worker-eval-exit-status: 0");
  const second = await prepareWorkerEvalBuild(root);
  expect(second.action).toBe("reused");
  expect(second.fingerprint).toBe(first.fingerprint);
  expect(readFileSync(join(root, ".tools", "build-count"), "utf8")).toBe("build\n");
  writeFileSync(first.binary_paths.jcode, "tampered");
  expect((await prepareWorkerEvalBuild(root)).action).toBe("built");
  expect(readFileSync(join(root, ".tools", "build-count"), "utf8")).toBe("build\nbuild\n");
});

test("stale stamp or status triggers rebuilding, and a dirty worktree is preserved", async () => {
  const root = fixture();
  const first = await prepareWorkerEvalBuild(root);
  const stamp: Record<string, unknown> = JSON.parse(readFileSync(first.stamp, "utf8"));
  stamp.fingerprint = "0".repeat(64);
  writeFileSync(first.stamp, JSON.stringify(stamp));
  const second = await prepareWorkerEvalBuild(root);
  expect(second.action).toBe("built");
  const status: Record<string, unknown> = JSON.parse(readFileSync(second.build_status, "utf8"));
  status.status = "failed";
  writeFileSync(second.build_status, JSON.stringify(status));
  const third = await prepareWorkerEvalBuild(root);
  expect(third.action).toBe("built");
  writeFileSync(join(third.source.worktree, "user-edit.txt"), "preserve me");
  await expect(prepareWorkerEvalBuild(root)).rejects.toThrow("dirty");
  expect(readFileSync(join(third.source.worktree, "user-edit.txt"), "utf8")).toBe("preserve me");
});

test("changed patch bytes and toolchain change the fingerprint", async () => {
  const root = fixture();
  const first = await prepareWorkerEvalBuild(root);
  // An added patch email header remains a valid git-am patch while changing its bytes.
  const path = join(root, "patches", "0001-fixture.patch");
  const patch = readFileSync(path, "utf8");
  writeFileSync(path, patch.replace("X-Jcode-Patch-Intent: fixture", "X-Jcode-Patch-Intent: changed fixture"));
  const second = await prepareWorkerEvalBuild(root);
  expect(second.fingerprint).not.toBe(first.fingerprint);
  writeFileSync(join(root, "fake-bin", "rustc"), "#!/bin/sh\nprintf 'rustc 2.0.0\\nhost: another-host\\n'\n");
  const third = await prepareWorkerEvalBuild(root);
  expect(third.fingerprint).not.toBe(second.fingerprint);
});

test("patch addition and removal invalidate the build fingerprint", async () => {
  const root = fixture();
  const first = await prepareWorkerEvalBuild(root);
  const path = join(root, "patches", "0002-second.patch");
  const saved = readFileSync(path);
  rmSync(path);
  const without = await prepareWorkerEvalBuild(root);
  expect(without.fingerprint).not.toBe(first.fingerprint);
  expect(existsSync(join(without.source.worktree, "second.txt"))).toBe(false);
  writeFileSync(path, saved);
  const restored = await prepareWorkerEvalBuild(root);
  expect(restored.fingerprint).toBe(first.fingerprint);
  expect(restored.action).toBe("built");
  expect(existsSync(join(restored.source.worktree, "second.txt"))).toBe(true);
});

test("failed builds retain status and log, never retain a success stamp, and can be retried", async () => {
  const root = fixture();
  writeFileSync(join(root, ".tools", "fail-build"), "");
  await expect(prepareWorkerEvalBuild(root)).rejects.toThrow("exit status 19");
  const builds = join(root, ".tools", "worker-eval", "builds");
  const attempt = join(builds, readdirSync(builds)[0]);
  expect(json(join(attempt, "build-status.json")).status).toBe("failed");
  expect(readFileSync(join(attempt, "build.log"), "utf8")).toContain("fixture build failure");
  expect(existsSync(join(attempt, "success.json"))).toBe(false);
  rmSync(join(root, ".tools", "fail-build"));
  expect((await prepareWorkerEvalBuild(root)).action).toBe("built");
});

test("rejects catalog and source changes during a build", async () => {
  for (const mode of ["change-patch", "change-source"]) {
    const root = fixture();
    writeFileSync(join(root, ".tools", mode), "");
    await expect(prepareWorkerEvalBuild(root)).rejects.toThrow(mode === "change-patch" ? "catalog or toolchain changed" : "source is dirty");
    const attempt = join(root, ".tools", "worker-eval", "builds", readdirSync(join(root, ".tools", "worker-eval", "builds"))[0]);
    expect(json(join(attempt, "build-status.json")).status).toBe("rejected");
    expect(existsSync(join(attempt, "success.json"))).toBe(false);
  }
});

test("preflight failure precedes any native build and passes the selected scenario", async () => {
  const root = fixture();
  writeFileSync(join(root, ".tools", "fail-preflight"), "");
  await expect(runWorkerEvaluation(root, "example-scenario")).rejects.toThrow("preflight failed");
  expect(events(root)).toHaveLength(1);
  expect(events(root)[0].args).toEqual(["validate-settings", "--config", join(root, "customizations", "workers", "evals", "config.toml"), "--scenarios", join(root, "customizations", "workers", "evals", "scenarios"), "--scenario", "example-scenario"]);
  expect(existsSync(join(root, ".tools", "build-count"))).toBe(false);
});

test("evaluation receives provenance and binaries under the build lock in a scrubbed environment", async () => {
  const root = fixture();
  process.env.JCODE_PROVIDER_TEST = "must-not-leak";
  await runWorkerEvaluation(root, "scenario-one");
  const [preflight, run] = events(root);
  expect(preflight.args[0]).toBe("validate-settings");
  expect(run.args[0]).toBe("run");
  expect(run.args.slice(-2)).toEqual(["--scenario", "scenario-one"]);
  expect(run.locked).toBe(true);
  expect(run.provenance.action).toBe("built");
  expect(run.provenance.build_check_seconds).toBeGreaterThanOrEqual(0);
  expect(run.args[run.args.indexOf("--binary") + 1]).toBe(run.provenance.binary_paths.jcode);
  expect(run.args[run.args.indexOf("--bridge") + 1]).toBe(run.provenance.binary_paths["jcode-harness-api-bridge"]);
  expect(run.home).not.toBe(process.env.HOME);
  expect(run.jcodeHome).toBe(join(run.home, ".jcode"));
  expect(run.provider).toBeUndefined();
  await withFileLock(join(root, ".tools", "worker-eval", "build.lock"), async () => {});
});

test("propagates evaluator failures and releases the lock", async () => {
  const root = fixture();
  writeFileSync(join(root, ".tools", "fail-evaluation"), "");
  await expect(runWorkerEvaluation(root)).rejects.toThrow("exit status 9");
  expect(events(root)).toHaveLength(2);
  await withFileLock(join(root, ".tools", "worker-eval", "build.lock"), async () => {});
});

test.skipIf(process.platform === "win32")("running build and evaluation retain their lock after the orchestrator dies", async () => {
  for (const mode of ["build", "evaluation"] as const) {
    const root = fixture();
    writeFileSync(join(root, ".tools", `hold-${mode}`), "");
    const module = new URL("../src/eval.ts", import.meta.url).pathname;
    const expression = mode === "build" ? "prepareWorkerEvalBuild(root)" : "runWorkerEvaluation(root)";
    const script = `import {prepareWorkerEvalBuild, runWorkerEvaluation} from ${JSON.stringify(module)}; const root = ${JSON.stringify(root)}; await ${expression};`;
    const parent = Bun.spawn([process.execPath, "-e", script], { cwd: root, env: { ...process.env }, stdout: "ignore", stderr: "pipe" });
    const stderr = new Response(parent.stderr).text();
    let childPid: number | undefined;
    try {
      const ready = join(root, ".tools", `${mode}-ready`);
      const signal = Promise.withResolvers<void>();
      const watcher = watch(dirname(ready), () => { if (existsSync(ready)) signal.resolve(); });
      try {
        if (!existsSync(ready)) await Promise.race([signal.promise, parent.exited.then(async code => {
          const builds = join(root, ".tools", "worker-eval", "builds");
          const logs = existsSync(builds) ? readdirSync(builds).map(name => {
            const path = join(builds, name, "build.log");
            return existsSync(path) ? readFileSync(path, "utf8") : "";
          }).join("\n") : "";
          throw new Error(`orchestrator exited before ${mode} became ready: ${code}\n${await stderr}\n${logs}`);
        })]);
      } finally { watcher.close(); }
      childPid = Number(readFileSync(ready, "utf8"));
      expect(childPid).toBeGreaterThan(0);
      parent.kill("SIGKILL");
      await parent.exited;
      const lock = join(root, ".tools", "worker-eval", "build.lock");
      await expect(withFileLock(lock, async () => {})).rejects.toThrow("Lock already held");
    } finally {
      if (parent.exitCode === null) parent.kill("SIGKILL");
      await parent.exited;
      if (childPid) {
        try { process.kill(-childPid, "SIGKILL"); } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
        }
        const lock = join(root, ".tools", "worker-eval", "build.lock");
        const script = `import {withFileLock} from ${JSON.stringify(primitives)}; for (;;) { try { await withFileLock(${JSON.stringify(lock)}, async () => {}); break; } catch { const signal = Promise.withResolvers<void>(); setImmediate(signal.resolve); await signal.promise; } }`;
        const waiter = Bun.spawn([process.execPath, "-e", script], { stdout: "ignore", stderr: "ignore" });
        expect(await waiter.exited).toBe(0);
      }
    }
  }
}, 30000);
