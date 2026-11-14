import { afterEach, describe, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { appendSkips, executedTests, parseFailedTests, runValidations, shouldSkip, testAll as runAll, validationIdentity } from "../src/validate";
import { NEXTEST_VERSION, nextestTarget, saveExclusions } from "../src/nextest";
import type { Patch } from "../src/patches";

const roots: string[] = [];
const temp = () => { const path = mkdtempSync(join(tmpdir(), "validation-fixture-")); roots.push(path); return path; };
const environment = () => Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined));
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

function fixturePatch(command: string): Patch {
  return { name: "0001-fixture.patch", path: "/unused", metadata: { intent: "test", dependencies: [], filename: "0001-fixture.patch", commands: [command] } };
}

describe("validation accounting", () => {
  test("detects actual executed test totals and failure names", () => {
    expect(executedTests("running 0 tests\ntest result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; 10 filtered out\n")).toBe(false);
    expect(executedTests("\u001b[32mtest result: ok. 1 passed; 0 failed; 0 ignored;\u001b[0m\n")).toBe(true);
    expect(executedTests("test result: FAILED. 0 passed; 1 failed; 0 ignored;\n")).toBe(true);
    expect(parseFailedTests("test z::test ... FAILED\ntest a::test ... FAILED\ntest z::test ... FAILED\n")).toEqual(["a::test", "z::test"]);
  });

  test("preserves broad skips and never masks an explicitly selected exact test", () => {
    const exclusions = ["module::selected", "module::other"];
    expect(appendSkips("cargo test --lib module::selected -- --exact", exclusions)).toBe("cargo test --lib module::selected -- --exact --skip 'module::other'");
    expect(appendSkips("cargo test --lib -- module::selected --exact", exclusions)).toBe("cargo test --lib -- module::selected --exact --skip 'module::other'");
    expect(appendSkips("cargo test --lib", exclusions)).toContain(" -- --skip 'module::selected' --skip 'module::other'");
    expect(appendSkips("cargo test --lib -- --exact --skip module::selected", exclusions)).toContain("--skip 'module::selected'");
    expect(appendSkips("cargo test", ["name'; echo bad; '"])).toContain("'\\''");
  });

  test("runs fake cargo with live output, rejects zero tests and keeps exact exclusions executable", async () => {
    if (process.platform === "win32") return;
    const root = temp(), bin = join(root, "bin"), tree = join(root, "tree"), argumentsFile = join(root, "args");
    mkdirSync(bin); mkdirSync(tree);
    const cargo = join(bin, "cargo");
    writeFileSync(cargo, `#!/bin/sh\nprintf '%s\\n' "$@" > "$ARGUMENTS_FILE"\ncase "$FIXTURE" in\n zero) echo 'test result: ok. 0 passed; 0 failed; 1 ignored;';;\n fail) echo 'test module::bad ... FAILED'; exit 1;;\n *) echo 'test result: ok. 1 passed; 0 failed; 0 ignored;';;\nesac\n`);
    chmodSync(cargo, 0o755);
    const env = { ...environment(), PATH: `${bin}:${process.env.PATH ?? ""}`, ARGUMENTS_FILE: argumentsFile, FIXTURE: "zero" };
    await saveExclusions(root, "base", [], ["module::other", "module::selected"]);
    const patch = fixturePatch("cargo test --lib module::selected -- --exact");
    let streamed = "";
    await expect(runValidations(root, tree, "base", [patch], env, "test", text => { streamed += text; })).rejects.toThrow("no tests executed");
    expect(streamed).toContain("0 passed");
    env.FIXTURE = "pass";
    await runValidations(root, tree, "base", [patch], env, "test");
    const args = readFileSync(argumentsFile, "utf8");
    expect(args).toContain("module::selected\n");
    expect(args).toContain("--skip\nmodule::other\n");
    expect(args).not.toContain("--skip\nmodule::selected\n");
    env.FIXTURE = "fail";
    await expect(runValidations(root, tree, "base", [patch], env, "test")).rejects.toThrow("validation failed in 0001-fixture.patch");
    expect(await runValidations(root, tree, "base", [patch], env, "learn")).toEqual(["module::bad"]);
    expect(readFileSync(argumentsFile, "utf8")).not.toContain("--skip");
  });
});

describe("closure cache", () => {
  test("requires current pass, target blob, closure fingerprint and requested compatibility", () => {
    const record = { status: "pass" as const, blob: "blob", fingerprint: "closure", compat_each: false };
    expect(shouldSkip(record, "blob", "closure", {})).toBe(true);
    expect(shouldSkip(record, "changed", "closure", {})).toBe(false);
    expect(shouldSkip(record, "blob", "changed", {})).toBe(false);
    expect(shouldSkip(record, "blob", "closure", { force: true })).toBe(false);
    expect(shouldSkip(record, "blob", "closure", { compatEach: true })).toBe(false);
    expect(shouldSkip({ ...record, compat_each: true }, "blob", "closure", { compatEach: true })).toBe(true);
    expect(shouldSkip({ ...record, status: "running" }, "blob", "closure", {})).toBe(false);
  });

  test("dependency, nextest config and exclusions invalidate dependent closure", async () => {
    if (process.platform === "win32") return;
    const root = temp(), patches = join(root, "patches"), bin = join(root, "bin");
    mkdirSync(patches); mkdirSync(bin);
    const original = process.env.PATH;
    const rustc = join(bin, "rustc");
    writeFileSync(rustc, "#!/bin/sh\necho 'rustc fixture 1'\n"); chmodSync(rustc, 0o755);
    process.env.PATH = `${bin}:${original ?? ""}`;
    const patchText = (name: string, dependency: string, detail: string) => `X-Jcode-Patch-Intent: test\nX-Jcode-Patch-Depends-On: ${dependency}\nX-Jcode-Patch-File: ${name}\n\nPatch intent:\n${detail}\nWhy it exists:\nfixture\nUpstream integration points:\nfixture\nUpdate guidance:\nfixture\nValidation:\ncargo test --lib\n---\n`;
    const first = join(patches, "0001-first.patch"), second = join(patches, "0002-second.patch");
    writeFileSync(first, patchText("0001-first.patch", "none", "first"));
    writeFileSync(second, patchText("0002-second.patch", "0001-first.patch", "second"));
    try {
      const initial = await validationIdentity(root, "0002-second.patch", "base");
      writeFileSync(first, patchText("0001-first.patch", "none", "changed"));
      const changed = await validationIdentity(root, "0002-second.patch", "base");
      expect(changed).not.toBe(initial);
      writeFileSync(join(root, "nextest.toml"), "[profile.compat]\nfail-fast = false\n");
      const config = await validationIdentity(root, "0002-second.patch", "base");
      expect(config).not.toBe(changed);
      await saveExclusions(root, "base", ["upstream::broken"], []);
      expect(await validationIdentity(root, "0002-second.patch", "base")).not.toBe(config);
    } finally { if (original === undefined) delete process.env.PATH; else process.env.PATH = original; }
  });

  test("full stack gate and closure passes are reused, with compatibility upgrades rerun", async () => {
    if (process.platform === "win32") return;
    const root = temp(), bin = join(root, "fake-bin"), calls = join(root, "calls"), index = join(root, "fixture-index");
    mkdirSync(bin);
    const env = { ...environment(), GIT_AUTHOR_NAME: "Fixture", GIT_AUTHOR_EMAIL: "fixture@example.invalid", GIT_COMMITTER_NAME: "Fixture", GIT_COMMITTER_EMAIL: "fixture@example.invalid" };
    const git = (args: string[], extra: Record<string, string> = {}) => {
      const result = Bun.spawnSync(["git", ...args], { cwd: root, env: { ...env, ...extra }, stdout: "pipe", stderr: "pipe" });
      if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")}: ${result.stderr.toString()}`);
      return result.stdout.toString();
    };
    git(["init", "-b", "personalized"]);
    writeFileSync(join(root, "fixture.txt"), "old\n");
    writeFileSync(join(root, "VERSION.txt"), "v1.0.0\n");
    git(["add", "fixture.txt", "VERSION.txt"]); git(["commit", "-m", "base"]);
    const base = git(["rev-parse", "HEAD"]).trim();
    git(["update-ref", "refs/jcode/upstream-tags/v1.0.0", base]);
    writeFileSync(join(root, "new-content"), "new\n");
    const blob = git(["hash-object", "-w", "new-content"]).trim();
    const indexEnv = { GIT_INDEX_FILE: index };
    git(["read-tree", "HEAD"], indexEnv);
    git(["update-index", "--add", "--cacheinfo", `100644,${blob},fixture.txt`], indexEnv);
    const tree = git(["write-tree"], indexEnv).trim();
    const message = join(root, "message");
    writeFileSync(message, "fixture patch\n\nX-Jcode-Patch-Intent: fixture\nX-Jcode-Patch-Depends-On: none\nX-Jcode-Patch-File: 0001-fixture.patch\n\nPatch intent:\nfixture\nWhy it exists:\nfixture\nUpstream integration points:\nfixture\nUpdate guidance:\nfixture\nValidation:\ncargo test --lib\n");
    const commit = git(["commit-tree", tree, "-p", base, "-F", message]).trim();
    const patch = git(["-c", "diff.suppressBlankEmpty=true", "format-patch", "--no-signature", "--stdout", "-1", commit]).replace(/^From [0-9a-f]{40} /, `From ${"0".repeat(40)} `);
    mkdirSync(join(root, "patches"));
    writeFileSync(join(root, "patches", "0001-fixture.patch"), patch);
    git(["add", "patches/0001-fixture.patch"]);
    const cargo = join(bin, "cargo"), rustc = join(bin, "rustc");
    writeFileSync(cargo, "#!/bin/sh\necho cargo:$* >> \"$CALLS\"\nif [ \"$FAIL_GATE\" = 1 ]; then exit 1; fi\nif [ \"$1\" = test ]; then echo 'test result: ok. 1 passed; 0 failed; 0 ignored;'; fi\n");
    writeFileSync(rustc, "#!/bin/sh\necho 'rustc fixture'\n");
    chmodSync(cargo, 0o755); chmodSync(rustc, 0o755);
    const binary = join(root, ".tools", "nextest", NEXTEST_VERSION, nextestTarget(), "cargo-nextest");
    mkdirSync(join(binary, ".."), { recursive: true });
    writeFileSync(binary, `#!/bin/sh\nif [ "$1" = --version ]; then echo 'cargo-nextest ${NEXTEST_VERSION}'; else echo nextest >> "$CALLS"; fi\n`);
    chmodSync(binary, 0o755);
    const oldPath = process.env.PATH, oldCalls = process.env.CALLS, oldFail = process.env.FAIL_GATE;
    process.env.PATH = `${bin}:${oldPath ?? ""}`; process.env.CALLS = calls; process.env.FAIL_GATE = "0";
    try {
      await runAll(root, {});
      const first = readFileSync(calls, "utf8").trim().split("\n");
      expect(first).toEqual(["cargo:check --workspace --tests", "cargo:test --lib", "nextest", "cargo:check --workspace --tests", "cargo:test --lib"]);
      await runAll(root, {});
      expect(readFileSync(calls, "utf8").trim().split("\n")).toEqual(first);
      await runAll(root, { compatEach: true });
      expect(readFileSync(calls, "utf8").trim().split("\n").slice(first.length)).toEqual(["cargo:check --workspace --tests", "cargo:test --lib", "nextest"]);
      const results = JSON.parse(readFileSync(join(root, ".tools", "patch-tests", base, "results.json"), "utf8"));
      expect(results.__full_stack__.status).toBe("pass");
      expect(results["0001-fixture.patch"].compat_each).toBe(true);
      process.env.FAIL_GATE = "1";
      await expect(runAll(root, { force: true })).rejects.toThrow("cargo check failed");
      const failed = JSON.parse(readFileSync(join(root, ".tools", "patch-tests", base, "results.json"), "utf8"));
      expect(failed.__full_stack__.status).toBe("fail");
    } finally {
      if (oldPath === undefined) delete process.env.PATH; else process.env.PATH = oldPath;
      if (oldCalls === undefined) delete process.env.CALLS; else process.env.CALLS = oldCalls;
      if (oldFail === undefined) delete process.env.FAIL_GATE; else process.env.FAIL_GATE = oldFail;
    }
  });
});
