import { dlopen, FFIType } from "bun:ffi";
import { afterEach, describe, expect, test } from "bun:test";
import { lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";
import { withFileLock } from "../src/primitives";
import { ensurePinnedSdk, pinnedVersion, resolveBase } from "../src/sdk";

const temporaryRoots: string[] = [];
const upstreamUrl = "https://github.com/1jehuang/jcode.git";

function git(root: string, ...args: string[]): string {
  const result = Bun.spawnSync(["git", ...args], { cwd: root, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")}: ${result.stderr.toString()}`);
  return result.stdout.toString().trim();
}

function repository(): string {
  const root = mkdtempSync(join(tmpdir(), "sdk-test-"));
  temporaryRoots.push(root);
  git(root, "init", "-q");
  git(root, "config", "user.name", "SDK Test");
  git(root, "config", "user.email", "sdk-test@example.invalid");
  return root;
}

function pin(root: string, version: string): void {
  writeFileSync(join(root, "VERSION.txt"), `${version}\n`);
}

function release(root: string, version: string, source: string, updateRef = true): string {
  const sdk = join(root, "sdk", "typescript");
  mkdirSync(join(sdk, "src"), { recursive: true });
  writeFileSync(join(sdk, "package.json"), JSON.stringify({ name: "fixture-sdk" }));
  writeFileSync(join(sdk, "src", "index.ts"), source);
  writeFileSync(join(sdk, "src", "client.ts"), `// client for ${source}\n`);
  git(root, "add", "--", "sdk/typescript");
  git(root, "commit", "--no-gpg-sign", "-qm", `SDK ${version} ${source}`);
  const commit = git(root, "rev-parse", "HEAD");
  if (updateRef) git(root, "update-ref", `refs/jcode/upstream-tags/${version}`, commit);
  return commit;
}

function sdkPath(root: string): string {
  return join(root, ".tools", "sdk", "typescript");
}

function marker(root: string): { version: string; commit: string } {
  return JSON.parse(readFileSync(join(sdkPath(root), ".jcode-sdk-version.json"), "utf8"));
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("pinned SDK", () => {
  test("reads only stable release pins", () => {
    const root = repository();
    expect(() => pinnedVersion(root)).toThrow("cannot read catalog version");
    writeFileSync(join(root, "VERSION.txt"), "  v1.2.3\n");
    expect(pinnedVersion(root)).toBe("v1.2.3");
    for (const version of ["", "main", "1.2.3", "v1.2", "v1.2.3-rc.1", "v1.2.3\nextra"]) {
      pin(root, version);
      expect(() => pinnedVersion(root)).toThrow("not a stable release tag");
    }
  });

  test("resolves and refreshes the private canonical ref, including annotated tags", () => {
    const upstream = repository();
    const first = release(upstream, "v1.2.3", "export const value = 1;\n", false);
    git(upstream, "tag", "-a", "v1.2.3", "-m", "fixture release");

    const root = repository();
    pin(root, "v1.2.3");
    git(root, "config", `url.${pathToFileURL(upstream).href}.insteadOf`, upstreamUrl);
    expect(resolveBase(root)).toBe(first);

    const second = release(upstream, "v1.2.3", "export const value = 2;\n", false);
    git(upstream, "tag", "-f", "-a", "v1.2.3", "-m", "updated fixture release");
    expect(resolveBase(root)).toBe(first);
    expect(resolveBase(root, { fetchRelease: true })).toBe(second);
  });

  test("extracts from the pinned Git object, not either worktree, and reuses its cache", async () => {
    const root = repository();
    const source = "export const value = 'committed';\n";
    const commit = release(root, "v1.2.3", source);
    pin(root, "v1.2.3");
    writeFileSync(join(root, "sdk", "typescript", "src", "index.ts"), "dirty worktree\n");
    mkdirSync(join(root, ".patched-jcode", "sdk", "typescript", "src"), { recursive: true });
    writeFileSync(join(root, ".patched-jcode", "sdk", "typescript", "src", "index.ts"), "patched worktree\n");

    const path = await ensurePinnedSdk(relative(process.cwd(), root));
    expect(path).toBe(sdkPath(root));
    expect(readFileSync(join(path, "src", "index.ts"), "utf8")).toBe(source);
    expect(readFileSync(join(path, "src", "client.ts"), "utf8")).toBe(`// client for ${source}\n`);
    expect(marker(root)).toEqual({ version: "v1.2.3", commit });
    const actual = realpathSync(path);
    await ensurePinnedSdk(root);
    expect(realpathSync(path)).toBe(actual);
  });

  test("publishes version and ref changes without deleting old reader snapshots", async () => {
    const root = repository();
    const first = release(root, "v1.0.0", "export const value = 1;\n");
    pin(root, "v1.0.0");
    await ensurePinnedSdk(root);
    const firstTree = realpathSync(sdkPath(root));
    expect(lstatSync(sdkPath(root)).isSymbolicLink()).toBe(true);
    expect(marker(root)).toEqual({ version: "v1.0.0", commit: first });

    const second = release(root, "v2.0.0", "export const value = 2;\n");
    pin(root, "v2.0.0");
    await ensurePinnedSdk(root);
    const secondTree = realpathSync(sdkPath(root));
    expect(secondTree).not.toBe(firstTree);
    expect(marker(root)).toEqual({ version: "v2.0.0", commit: second });
    expect(readFileSync(join(firstTree, "src", "index.ts"), "utf8")).toBe("export const value = 1;\n");
    expect(readFileSync(join(secondTree, "src", "index.ts"), "utf8")).toBe("export const value = 2;\n");

    const moved = release(root, "v2.0.0", "export const value = 3;\n");
    await ensurePinnedSdk(root);
    expect(marker(root)).toEqual({ version: "v2.0.0", commit: moved });
    expect(readFileSync(join(secondTree, "src", "index.ts"), "utf8")).toBe("export const value = 2;\n");
  });

  test("rebuilds for malformed or mismatched markers and an incomplete SDK", async () => {
    const root = repository();
    const commit = release(root, "v1.2.3", "export const value = 1;\n");
    pin(root, "v1.2.3");
    await ensurePinnedSdk(root);
    const markerPath = join(sdkPath(root), ".jcode-sdk-version.json");
    for (const invalid of ["{", "null", "[]", JSON.stringify({ version: "v9.9.9", commit }), JSON.stringify({ version: "v1.2.3", commit: 1 })]) {
      const oldTree = realpathSync(sdkPath(root));
      writeFileSync(markerPath, invalid);
      await ensurePinnedSdk(root);
      expect(realpathSync(sdkPath(root))).not.toBe(oldTree);
      expect(marker(root)).toEqual({ version: "v1.2.3", commit });
    }
    const incomplete = realpathSync(sdkPath(root));
    rmSync(join(sdkPath(root), "src", "index.ts"));
    await ensurePinnedSdk(root);
    expect(realpathSync(sdkPath(root))).not.toBe(incomplete);
    expect(readFileSync(join(sdkPath(root), "src", "index.ts"), "utf8")).toBe("export const value = 1;\n");
  });

  test("does not trust a matching marker in a patched-worktree symlink", async () => {
    const root = repository();
    const commit = release(root, "v1.2.3", "export const value = 'upstream';\n");
    pin(root, "v1.2.3");
    const fake = join(root, ".patched-jcode", "sdk", "typescript");
    mkdirSync(join(fake, "src"), { recursive: true });
    writeFileSync(join(fake, "package.json"), "{}\n");
    writeFileSync(join(fake, "src", "index.ts"), "patched SDK\n");
    writeFileSync(join(fake, ".jcode-sdk-version.json"), JSON.stringify({ version: "v1.2.3", commit }));
    mkdirSync(join(root, ".tools", "sdk"), { recursive: true });
    symlinkSync(fake, sdkPath(root), "dir");

    await ensurePinnedSdk(root);
    expect(realpathSync(sdkPath(root))).not.toBe(realpathSync(fake));
    expect(readFileSync(join(sdkPath(root), "src", "index.ts"), "utf8")).toBe("export const value = 'upstream';\n");
    expect(readFileSync(join(fake, "src", "index.ts"), "utf8")).toBe("patched SDK\n");
  });

  test("keeps the old SDK and removes the stage if the new tree is incomplete", async () => {
    const root = repository();
    release(root, "v1.0.0", "export const value = 1;\n");
    pin(root, "v1.0.0");
    await ensurePinnedSdk(root);
    const oldTree = realpathSync(sdkPath(root));
    const oldMarker = marker(root);
    rmSync(join(root, "sdk", "typescript", "src", "index.ts"));
    git(root, "add", "-A", "--", "sdk/typescript");
    git(root, "commit", "--no-gpg-sign", "-qm", "incomplete SDK");
    git(root, "update-ref", "refs/jcode/upstream-tags/v2.0.0", "HEAD");
    pin(root, "v2.0.0");

    await expect(ensurePinnedSdk(root)).rejects.toThrow("missing package.json or src/index.ts");
    expect(realpathSync(sdkPath(root))).toBe(oldTree);
    expect(marker(root)).toEqual(oldMarker);
    expect(readFileSync(join(sdkPath(root), "src", "index.ts"), "utf8")).toBe("export const value = 1;\n");
    expect(readdirSync(join(root, ".tools", "sdk")).some((name) => name.startsWith(".typescript-build-"))).toBe(false);
  });

  test("retains an existing physical SDK directory when migrating it to a pointer", async () => {
    const root = repository();
    release(root, "v1.0.0", "export const value = 1;\n");
    pin(root, "v1.0.0");
    await ensurePinnedSdk(root);
    const oldTree = realpathSync(sdkPath(root));
    rmSync(sdkPath(root));
    renameSync(oldTree, sdkPath(root));
    release(root, "v2.0.0", "export const value = 2;\n");
    pin(root, "v2.0.0");
    if (process.platform !== "linux" && process.platform !== "darwin") {
      await expect(ensurePinnedSdk(root)).rejects.toThrow("atomic SDK directory exchange is unavailable");
      expect(lstatSync(sdkPath(root)).isDirectory()).toBe(true);
      expect(readFileSync(join(sdkPath(root), "src", "index.ts"), "utf8")).toBe("export const value = 1;\n");
      return;
    }
    await ensurePinnedSdk(root);

    expect(lstatSync(sdkPath(root)).isSymbolicLink()).toBe(true);
    const backup = readdirSync(join(root, ".tools", "sdk")).find((name) => name.startsWith(".typescript-previous-"));
    expect(backup).toBeDefined();
    expect(readFileSync(join(root, ".tools", "sdk", backup!, "src", "index.ts"), "utf8")).toBe("export const value = 1;\n");
    expect(readFileSync(join(sdkPath(root), "src", "index.ts"), "utf8")).toBe("export const value = 2;\n");
  });

  test.skipIf(process.platform !== "linux")("exchanges an existing SDK directory in one atomic operation", async () => {
    const root = repository();
    release(root, "v1.0.0", "export const value = 1;\n");
    pin(root, "v1.0.0");
    await ensurePinnedSdk(root);
    const oldTree = realpathSync(sdkPath(root));
    rmSync(sdkPath(root));
    renameSync(oldTree, sdkPath(root));
    release(root, "v2.0.0", "export const value = 2;\n");
    pin(root, "v2.0.0");

    const libc = dlopen("libc.so.6", {
      inotify_init1: { args: [FFIType.i32], returns: FFIType.i32 },
      inotify_add_watch: { args: [FFIType.i32, FFIType.ptr, FFIType.u32], returns: FFIType.i32 },
      read: { args: [FFIType.i32, FFIType.ptr, FFIType.u64], returns: FFIType.i64 },
      close: { args: [FFIType.i32], returns: FFIType.i32 },
    });
    // Linux inotify exposes both paired moves of RENAME_EXCHANGE. A separate
    // move-away followed by an ordinary rename lacks the second move-from.
    const fd = libc.symbols.inotify_init1(0x800 | 0x80000); // NONBLOCK | CLOEXEC
    try {
      expect(fd).toBeGreaterThanOrEqual(0);
      const cache = join(root, ".tools", "sdk");
      expect(libc.symbols.inotify_add_watch(fd, Buffer.from(`${cache}\0`), 0x40 | 0x80)).toBeGreaterThanOrEqual(0);
      await ensurePinnedSdk(root);

      const buffer = Buffer.alloc(8192);
      const length = Number(libc.symbols.read(fd, buffer, buffer.length));
      expect(length).toBeGreaterThan(0);
      const events: { mask: number; cookie: number; name: string }[] = [];
      for (let offset = 0; offset + 16 <= length;) {
        const mask = buffer.readUInt32LE(offset + 4);
        const cookie = buffer.readUInt32LE(offset + 8);
        const nameLength = buffer.readUInt32LE(offset + 12);
        const nameBytes = buffer.subarray(offset + 16, offset + 16 + nameLength);
        const nul = nameBytes.indexOf(0);
        events.push({ mask, cookie, name: nameBytes.subarray(0, nul < 0 ? nameBytes.length : nul).toString() });
        offset += 16 + nameLength;
      }
      const fromSdk = events.find((event) => event.name === "typescript" && (event.mask & 0x40) !== 0);
      expect(fromSdk).toBeDefined();
      const toBackup = events.find((event) => event.cookie === fromSdk!.cookie && (event.mask & 0x80) !== 0);
      expect(toBackup?.name.startsWith(".typescript-previous-")).toBe(true);
      const fromBackup = events.find((event) => event.name === toBackup!.name && (event.mask & 0x40) !== 0);
      expect(fromBackup).toBeDefined();
      expect(events.some((event) => event.name === "typescript" && event.cookie === fromBackup!.cookie && (event.mask & 0x80) !== 0)).toBe(true);
      expect(readFileSync(join(cache, toBackup!.name, "src", "index.ts"), "utf8")).toBe("export const value = 1;\n");
      expect(readFileSync(join(sdkPath(root), "src", "index.ts"), "utf8")).toBe("export const value = 2;\n");
    } finally {
      if (fd >= 0) libc.symbols.close(fd);
      libc.close();
    }
  });

  test("fails safely when another materializer owns the lock", async () => {
    const root = repository();
    release(root, "v1.0.0", "export const value = 1;\n");
    pin(root, "v1.0.0");
    await ensurePinnedSdk(root);
    const oldTree = realpathSync(sdkPath(root));
    release(root, "v2.0.0", "export const value = 2;\n");
    pin(root, "v2.0.0");

    const ready = Promise.withResolvers<void>();
    const gate = Promise.withResolvers<void>();
    const held = withFileLock(join(root, ".tools", "sdk", ".typescript.lock"), async () => {
      ready.resolve();
      await gate.promise;
    });
    await ready.promise;
    try {
      await expect(ensurePinnedSdk(root)).rejects.toThrow();
      expect(realpathSync(sdkPath(root))).toBe(oldTree);
    } finally {
      gate.resolve();
      await held;
    }
    await ensurePinnedSdk(root);
    expect(marker(root).version).toBe("v2.0.0");
  });

  test("does not swap while a separate process holds the SDK lock", async () => {
    const root = repository();
    release(root, "v1.0.0", "export const value = 1;\n");
    pin(root, "v1.0.0");
    await ensurePinnedSdk(root);
    const oldTree = realpathSync(sdkPath(root));
    release(root, "v2.0.0", "export const value = 2;\n");
    pin(root, "v2.0.0");

    const primitive = join(import.meta.dir, "../src/primitives.ts");
    const lock = join(root, ".tools", "sdk", ".typescript.lock");
    const script = `
      import { withFileLock } from ${JSON.stringify(primitive)};
      await withFileLock(${JSON.stringify(lock)}, async () => {
        const gate = Promise.withResolvers();
        process.stdin.once("end", gate.resolve);
        process.stdin.resume();
        process.stdout.write("locked\\n");
        await gate.promise;
      });
    `;
    const child = Bun.spawn([process.execPath, "-e", script], {
      cwd: root, stdin: "pipe", stdout: "pipe", stderr: "pipe",
    });
    try {
      const message = await child.stdout.getReader().read();
      if (!message.value) throw new Error(`lock holder failed: ${await new Response(child.stderr).text()}`);
      expect(new TextDecoder().decode(message.value)).toContain("locked");
      await expect(ensurePinnedSdk(root)).rejects.toThrow();
      expect(realpathSync(sdkPath(root))).toBe(oldTree);
    } finally {
      child.stdin.end();
      await child.exited;
    }
    expect(child.exitCode).toBe(0);
    await ensurePinnedSdk(root);
    expect(marker(root).version).toBe("v2.0.0");
  });
});
