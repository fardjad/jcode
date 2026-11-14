import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { chmodSync, closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { checkCatalog, checkIsolation, createPatched, dependencyOrder, discoverPatches, listPatches, snapshotPatches } from "../src/patches";
import { normalizedFormatPatch, prepareWorktree } from "../src/worktree";

const roots: string[] = [];
const first = "0001-first.patch";
const second = "0002-second.patch";

function run(root: string, ...args: string[]): string {
  const result = Bun.spawnSync(["git", ...args], { cwd: root, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")}: ${result.stderr.toString()}`);
  return result.stdout.toString().trim();
}

function body(name: string, dependencies = "none"): string {
  return `X-Jcode-Patch-Intent: fixture change
X-Jcode-Patch-Depends-On: ${dependencies}
X-Jcode-Patch-File: ${name}

Patch intent:
change

Why it exists:
test

Upstream integration points:
fixture

Update guidance:
maintain

Validation:
cargo test --lib`;
}

function fixture(): { root: string; base: string } {
  const root = mkdtempSync(join(tmpdir(), "catalog-test-"));
  roots.push(root);
  run(root, "init", "-q", "-b", "personalized");
  run(root, "config", "user.name", "Catalog Test");
  run(root, "config", "user.email", "catalog@example.invalid");
  writeFileSync(join(root, "base.txt"), "base\n");
  writeFileSync(join(root, ".gitignore"), ".patched-jcode/\n");
  run(root, "add", "base.txt", ".gitignore");
  run(root, "commit", "--no-gpg-sign", "-qm", "base");
  const base = run(root, "rev-parse", "HEAD");
  run(root, "update-ref", "refs/jcode/upstream-tags/v1.2.3", base);
  writeFileSync(join(root, "alpha.txt"), "alpha\n");
  run(root, "add", "alpha.txt");
  run(root, "commit", "--no-gpg-sign", "-qm", "first", "-m", body(first));
  const firstText = normalizedFormatPatch(root, "HEAD");
  writeFileSync(join(root, "alpha.txt"), "alpha\nbeta\n");
  run(root, "add", "alpha.txt");
  run(root, "commit", "--no-gpg-sign", "-qm", "second", "-m", body(second, first));
  const secondText = normalizedFormatPatch(root, "HEAD");
  run(root, "reset", "--hard", base);
  writeFileSync(join(root, "VERSION.txt"), "v1.2.3\n");
  mkdirSync(join(root, "patches"));
  writeFileSync(join(root, "patches", first), firstText);
  writeFileSync(join(root, "patches", second), secondText);
  run(root, "add", "patches");
  return { root, base };
}

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("catalog patch operations", () => {
  test("discovers sorted patches and transitive dependency closures", async () => {
    const { root } = fixture();
    expect((await discoverPatches(root)).map(patch => patch.name)).toEqual([first, second]);
    expect((await dependencyOrder(root, `patches/${second}`)).map(patch => patch.name)).toEqual([first, second]);
    expect((await dependencyOrder(root, first)).map(patch => patch.name)).toEqual([first]);
    await expect(dependencyOrder(root, `../${second}`)).rejects.toThrow("under patches");
    const path = join(root, "patches", second);
    writeFileSync(path, readFileSync(path, "utf8").replace(`X-Jcode-Patch-Depends-On: ${first}`, `X-Jcode-Patch-Depends-On: ${second}`));
    await expect(discoverPatches(root)).rejects.toThrow("invalid dependencies");
  });

  test("rejects symlinked patch files and filename header mismatches", async () => {
    const { root } = fixture();
    const path = join(root, "patches", second);
    writeFileSync(path, readFileSync(path, "utf8").replace(`X-Jcode-Patch-File: ${second}`, `X-Jcode-Patch-File: ${first}`));
    await expect(discoverPatches(root)).rejects.toThrow("does not match");
    rmSync(path);
    symlinkSync(join(root, "patches", first), path);
    await expect(discoverPatches(root)).rejects.toThrow("invalid patch file");
  });

  test("validates tracked catalog, actual application, and isolation closures", async () => {
    const { root } = fixture();
    await checkCatalog(root);
    await checkIsolation(root);
    run(root, "reset", "--", `patches/${second}`);
    await expect(checkCatalog(root)).rejects.toThrow("tracked patches do not match");
  });

  test("checks only added hunk lines for whitespace and conflict markers", async () => {
    const { root } = fixture();
    const path = join(root, "patches", first);
    const clean = readFileSync(path, "utf8");
    for (const replacement of ["+alpha ", "+ \talpha", "+<<<<<<< ours"]) {
      writeFileSync(path, clean.replace("+alpha\n", `${replacement}\n`));
      await expect(checkCatalog(root)).rejects.toThrow("whitespace error");
    }
    writeFileSync(path, clean.replace("change\n", "change with prose trailing spaces  \n"));
    await checkCatalog(root);
  });

  test("prepares detached worktrees without discarding dirty or unpersisted work", async () => {
    const { root, base } = fixture();
    await createPatched(root);
    const path = join(root, ".patched-jcode");
    expect(readFileSync(join(path, "alpha.txt"), "utf8")).toBe("alpha\nbeta\n");
    await createPatched(root);
    writeFileSync(join(path, "alpha.txt"), "dirty\n");
    await expect(createPatched(root)).rejects.toThrow("dirty");
    expect(readFileSync(join(path, "alpha.txt"), "utf8")).toBe("dirty\n");
    run(path, "checkout", "--", "alpha.txt");
    writeFileSync(join(path, "mine.txt"), "unsnapshotted\n");
    run(path, "add", "mine.txt");
    run(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "user commit");
    const head = run(path, "rev-parse", "HEAD");
    await expect(createPatched(root)).rejects.toThrow("unpersisted commits");
    expect(run(path, "rev-parse", "HEAD")).toBe(head);
    expect(readFileSync(join(path, "mine.txt"), "utf8")).toBe("unsnapshotted\n");
    await expect(prepareWorktree(root, "root", base, [], root)).rejects.toThrow("catalog checkout");
  });

  test("lists commit-to-patch mappings and stale files", async () => {
    const { root } = fixture();
    await createPatched(root);
    writeFileSync(join(root, "patches", "0003-stale.patch"), "stale\n");
    const output = spyOn(console, "log").mockImplementation(() => {});
    try {
      await listPatches(root);
      expect(output.mock.calls.map(call => String(call[0]))).toEqual([
        expect.stringMatching(new RegExp(`^[0-9a-f]{8} -> ${first.replaceAll(".", "\\.")} \\(first\\)$`)),
        expect.stringMatching(new RegExp(`^[0-9a-f]{8} -> ${second.replaceAll(".", "\\.")} \\(second\\)$`)),
        "0003-stale.patch (stale, no commit)",
      ]);
    } finally {
      output.mockRestore();
    }
  });

  test("reuses owned worktrees but never resets a user-changed HEAD", async () => {
    const { root, base } = fixture();
    const patches = await discoverPatches(root);
    const path = await prepareWorktree(root, "validation", base, patches);
    await prepareWorktree(root, "validation", base, patches.slice(0, 1));
    expect(readFileSync(join(path, "alpha.txt"), "utf8")).toBe("alpha\n");
    writeFileSync(join(path, "extra.txt"), "personal\n");
    run(path, "add", "extra.txt");
    run(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "personal");
    await expect(prepareWorktree(root, "validation", base, patches)).rejects.toThrow("unpersisted commits");
    expect(readFileSync(join(path, "extra.txt"), "utf8")).toBe("personal\n");
  });

  test("reuses a markerless legacy validation worktree only when commits are tool-applied catalog patches", async () => {
    const { root, base } = fixture();
    const patches = await discoverPatches(root);
    const path = await prepareWorktree(root, "validation", base, patches);
    const marker = run(path, "rev-parse", "--git-path", "jcode-catalog-worktree.json");
    const markerFile = marker.startsWith("/") ? marker : join(path, marker);
    unlinkSync(markerFile);
    writeFileSync(join(root, "patches", second), readFileSync(join(root, "patches", second), "utf8").replace("beta\n", "beta-updated\n"));
    await prepareWorktree(root, "validation", base, await discoverPatches(root));
    expect(readFileSync(join(path, "alpha.txt"), "utf8")).toBe("alpha\nbeta-updated\n");
    expect(existsSync(markerFile)).toBe(true);
    unlinkSync(markerFile);
    writeFileSync(join(path, "extra.txt"), "personal\n");
    run(path, "add", "extra.txt");
    run(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "personal");
    await expect(prepareWorktree(root, "validation", base, await discoverPatches(root))).rejects.toThrow("unpersisted commits");
  });

  test("refuses to overwrite an ignored file during patch application", async () => {
    const { root, base } = fixture();
    const path = await prepareWorktree(root, "validation", base, []);
    writeFileSync(join(root, ".git", "info", "exclude"), "alpha.txt\n");
    writeFileSync(join(path, "alpha.txt"), "personal ignored data\n");
    await expect(prepareWorktree(root, "validation", base, await discoverPatches(root))).rejects.toThrow("ignored path");
    expect(readFileSync(join(path, "alpha.txt"), "utf8")).toBe("personal ignored data\n");
  });

  test("refuses an in-progress Git operation even with a clean worktree", async () => {
    const { root, base } = fixture();
    const path = await prepareWorktree(root, "validation", base, []);
    const gitPath = run(path, "rev-parse", "--git-path", "rebase-apply");
    const state = gitPath.startsWith("/") ? gitPath : join(path, gitPath);
    mkdirSync(state);
    await expect(prepareWorktree(root, "validation", base, [])).rejects.toThrow("in-progress Git operation");
    expect(existsSync(state)).toBe(true);
  });

  test("passes held descriptors through Git while applying patches", async () => {
    if (process.platform === "win32") return;
    const { root, base } = fixture();
    const fd = openSync(join(root, "lock"), "w");
    try {
      const hooks = join(root, "hooks");
      mkdirSync(hooks);
      const hook = join(hooks, "pre-applypatch");
      writeFileSync(hook, `#!/bin/sh\ntest -e /dev/fd/${fd}\n`);
      chmodSync(hook, 0o755);
      run(root, "config", "core.hooksPath", hooks);
      await prepareWorktree(root, "locked", base, await discoverPatches(root), undefined, [fd]);
    } finally {
      closeSync(fd);
    }
  });

  test("snapshots validated commits atomically and prunes only with permission", async () => {
    const { root } = fixture();
    await createPatched(root);
    const path = join(root, ".patched-jcode");
    const third = "0003-third.patch";
    writeFileSync(join(path, "third.txt"), "third\n");
    run(path, "add", "third.txt");
    run(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "third", "-m", body(third, second));
    const stale = join(root, "patches", "0004-stale.patch");
    writeFileSync(stale, "stale\n");
    const before = readFileSync(join(root, "patches", first), "utf8");
    await expect(snapshotPatches(root, false)).rejects.toThrow("stale patch files");
    expect(existsSync(join(root, "patches", third))).toBe(false);
    expect(readFileSync(join(root, "patches", first), "utf8")).toBe(before);
    await snapshotPatches(root, true);
    expect(existsSync(stale)).toBe(false);
    expect(readFileSync(join(root, "patches", third), "utf8")).toContain(`X-Jcode-Patch-File: ${third}`);
    expect(readFileSync(join(root, "patches", third), "utf8")).toMatch(/^From 0{40} /);
  });

  test("snapshot rejects duplicate, reordered and forward-dependent names before writes", async () => {
    for (const [name, dependency, reason] of [
      [second, "none", "duplicate"],
      ["0000-earlier.patch", "none", "out of commit order"],
      ["0003-third.patch", "0004-later.patch", "earlier commit"],
    ]) {
      const { root } = fixture();
      await createPatched(root);
      const path = join(root, ".patched-jcode");
      writeFileSync(join(path, "third.txt"), "third\n");
      run(path, "add", "third.txt");
      run(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "third", "-m", body(name!, dependency!));
      const before = readFileSync(join(root, "patches", second), "utf8");
      await expect(snapshotPatches(root, true)).rejects.toThrow(reason!);
      expect(readFileSync(join(root, "patches", second), "utf8")).toBe(before);
    }
  });

  test("invalid later snapshot commit leaves all catalog files untouched", async () => {
    const { root } = fixture();
    await createPatched(root);
    const path = join(root, ".patched-jcode");
    writeFileSync(join(path, "third.txt"), "third\n");
    run(path, "add", "third.txt");
    run(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "missing metadata");
    const before = readFileSync(join(root, "patches", first), "utf8");
    await expect(snapshotPatches(root, true)).rejects.toThrow("commit");
    expect(readFileSync(join(root, "patches", first), "utf8")).toBe(before);
    expect(existsSync(join(root, "patches", "0003-third.patch"))).toBe(false);
  });
});
