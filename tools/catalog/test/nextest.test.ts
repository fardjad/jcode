import { afterEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ensureNextest, exclusionsPath, extractNextestExecutable, findNextestAsset, loadExclusions, NEXTEST_VERSION, nextestTarget, parseJUnitFailures, runCommand, runNextest, saveExclusions } from "../src/nextest";
import { withFileLock } from "../src/primitives";

const roots: string[] = [];
const temp = () => { const path = mkdtempSync(join(tmpdir(), "nextest-fixture-")); roots.push(path); return path; };
const environment = () => Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined));
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

describe("nextest bootstrap", () => {
  test("selects pinned release metadata and rejects invalid checksums", () => {
    const target = "x86_64-unknown-linux-gnu", checksum = "a".repeat(64);
    const release = { projects: { "cargo-nextest": { ranges: { "0.9": { versions: { [NEXTEST_VERSION]: { locations: [{ target, url: "https://example.invalid/file", checksums: { sha256: checksum } }] } } } } } } };
    expect(findNextestAsset(release, target)).toEqual({ url: "https://example.invalid/file", sha256: checksum });
    expect(() => findNextestAsset([{ target, url: "x", sha256: "invalid" }], target)).toThrow("checksum");
    expect(() => findNextestAsset(release, "missing")).toThrow("no cargo-nextest");
  });

  test("downloads only pinned checksummed archive and reuses validated cache", async () => {
    const root = temp(), target = nextestTarget();
    const executable = process.platform === "win32" ? "cargo-nextest.exe" : "cargo-nextest";
    if (process.platform === "win32") return;
    const archive = await new Bun.Archive({ [`bin/${executable}`]: `#!/bin/sh\necho cargo-nextest ${NEXTEST_VERSION}\n` }, { compress: "gzip" }).bytes();
    const sha256 = createHash("sha256").update(archive).digest("hex");
    const original = globalThis.fetch;
    let requests = 0;
    globalThis.fetch = (async (url: string | URL | Request) => {
      requests++;
      return String(url).endsWith("releases.json")
        ? Response.json([{ target, url: "https://example.invalid/archive", sha256 }])
        : new Response(archive);
    }) as typeof fetch;
    try {
      const binary = await ensureNextest(root, environment());
      expect(readFileSync(binary, "utf8")).toContain(NEXTEST_VERSION);
      expect(await ensureNextest(root, environment())).toBe(binary);
      expect(requests).toBe(2);
      writeFileSync(binary, "#!/bin/sh\necho cargo-nextest 0.0.0\n");
      await expect(ensureNextest(root, environment())).rejects.toThrow("unexpected version");
    } finally { globalThis.fetch = original; }
  });

  test("checksum mismatch never installs a cached binary", async () => {
    const root = temp(), target = nextestTarget(), original = globalThis.fetch;
    globalThis.fetch = (async (url: string | URL | Request) => String(url).endsWith("releases.json")
      ? Response.json([{ target, url: "https://example.invalid/archive", sha256: "0".repeat(64) }])
      : new Response("bad archive")) as typeof fetch;
    try { await expect(ensureNextest(root, environment())).rejects.toThrow("checksum mismatch"); }
    finally { globalThis.fetch = original; }
  });

  test("extracts only safe regular ZIP executable entries", async () => {
    const zip = (name: string, mode = 0o100755) => {
      const filename = Buffer.from(name), content = Buffer.from("binary");
      const local = Buffer.alloc(30); local.writeUInt32LE(0x04034b50); local.writeUInt32LE(content.length, 18); local.writeUInt32LE(content.length, 22); local.writeUInt16LE(filename.length, 26);
      const central = Buffer.alloc(46); central.writeUInt32LE(0x02014b50); central.writeUInt32LE(content.length, 20); central.writeUInt32LE(content.length, 24); central.writeUInt16LE(filename.length, 28); central.writeUInt32LE((mode << 16) >>> 0, 38);
      const offset = local.length + filename.length + content.length, size = central.length + filename.length;
      const end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50); end.writeUInt16LE(1, 8); end.writeUInt16LE(1, 10); end.writeUInt32LE(size, 12); end.writeUInt32LE(offset, 16);
      return Buffer.concat([local, filename, content, central, filename, end]);
    };
    expect(Buffer.from(await extractNextestExecutable(zip("bin/cargo-nextest.exe"), "cargo-nextest.exe")).toString()).toBe("binary");
    await expect(extractNextestExecutable(zip("../cargo-nextest.exe"), "cargo-nextest.exe")).rejects.toThrow("safe executable");
    await expect(extractNextestExecutable(zip("bin/cargo-nextest.exe", 0o120777), "cargo-nextest.exe")).rejects.toThrow("unsafe");
  });
});

test("child retains its inherited process lock after the parent closes it", async () => {
  if (process.platform === "win32") return;
  const root = temp(), path = join(root, "lock"), fifo = join(root, "release");
  expect(Bun.spawnSync(["mkfifo", fifo]).exitCode).toBe(0);
  const ready = Promise.withResolvers<void>();
  let child!: Promise<{ code: number; stdout: string }>;
  await withFileLock(path, async fd => {
    child = runCommand(["sh", "-c", 'echo ready; cat "$RELEASE" >/dev/null'], { cwd: root, env: { ...environment(), RELEASE: fifo }, lockFds: [fd], output: () => ready.resolve() });
    await ready.promise;
  });
  try {
    await expect(withFileLock(path, async () => undefined)).rejects.toThrow("Lock already held");
  } finally {
    const writer = Bun.spawn(["sh", "-c", 'echo go > "$RELEASE"'], { env: { ...environment(), RELEASE: fifo }, stdout: "ignore", stderr: "inherit" });
    await writer.exited;
    await child;
  }
  await withFileLock(path, async () => undefined);
});

describe("nextest exclusions", () => {
  test("parses failed cases and rejects malformed, ambiguous and incomplete reports", () => {
    const xml = `<?xml version="1.0"?><testsuites><testsuite failures="2"><testcase name="a::first"><failure>bad</failure></testcase><testcase name="b&amp;c"><error/></testcase><testcase name="ok"/></testsuite></testsuites>`;
    expect(parseJUnitFailures(xml)).toEqual(["a::first", "b&c"]);
    expect(() => parseJUnitFailures('<testsuite failures="1"></testsuite>')).toThrow("no usable");
    expect(() => parseJUnitFailures('<testsuite><testcase name="a"><failure/></testcase><testcase name="a"><error/></testcase></testsuite>')).toThrow("duplicate");
    expect(() => parseJUnitFailures('<testsuite><testcase name="x"><failure/></testsuite>')).toThrow("nesting");
    expect(() => parseJUnitFailures('<testsuite failures="bad"/>')).toThrow("counts");
    expect(parseJUnitFailures('<testsuite><testcase name="module::a"><failure message="x > y"/></testcase></testsuite>')).toEqual(["module::a"]);
  });

  test("stores sorted base-specific names and fails closed on malformed records", async () => {
    const root = temp();
    await saveExclusions(root, "base", ["z", "a", "a"], ["two", "one"]);
    expect((await loadExclusions(root, "base"))?.failed_tests).toEqual(["a", "z"]);
    expect((await loadExclusions(root, "base"))?.failed_validation_tests).toEqual(["one", "two"]);
    expect(await loadExclusions(root, "other")).toBeNull();
    writeFileSync(exclusionsPath(root, "base"), JSON.stringify({ base_commit: "base", nextest_version: NEXTEST_VERSION, failed_tests: ["z", "a"] }));
    await expect(loadExclusions(root, "base")).rejects.toThrow("invalid nextest exclusions");
  });

  test("learning ignores exclusions and rejects a failed run without named failures", async () => {
    if (process.platform === "win32") return;
    const root = temp(), tree = join(root, "tree"), target = join(root, "target"), binary = join(root, ".tools/nextest", NEXTEST_VERSION, nextestTarget(), "cargo-nextest");
    mkdirSync(tree, { recursive: true }); mkdirSync(join(binary, ".."), { recursive: true });
    writeFileSync(binary, `#!/bin/sh\nif [ "$1" = --version ]; then echo 'cargo-nextest ${NEXTEST_VERSION}'; exit 0; fi\nprintf '%s\\n' "$*" > "$CARGO_TARGET_DIR/args"\nmkdir -p "$CARGO_TARGET_DIR/nextest/compat"\nprintf '%s' '<testsuite failures="1"><testcase name="upstream::failure"><failure/></testcase></testsuite>' > "$CARGO_TARGET_DIR/nextest/compat/junit.xml"\nexit 1\n`);
    chmodSync(binary, 0o755); mkdirSync(target, { recursive: true });
    const env = { ...environment(), CARGO_TARGET_DIR: target };
    await saveExclusions(root, "base", ["upstream::failure"], []);
    expect(await runNextest(root, tree, "base", "learn", env)).toEqual(["upstream::failure"]);
    expect(readFileSync(join(target, "args"), "utf8")).not.toContain("filterset");
    await saveExclusions(root, "base", ["upstream::failure", "a,)"], []);
    await expect(runNextest(root, tree, "base", "test", env)).rejects.toThrow("compatibility test failed");
    const filtered = readFileSync(join(target, "args"), "utf8");
    expect(filtered).toContain("not test(=upstream::failure)");
    expect(filtered).toContain("not test(=a\\,\\))");
    await saveExclusions(root, "base", ["upstream::failure"], []);
    writeFileSync(binary, `#!/bin/sh\nif [ "$1" = --version ]; then echo 'cargo-nextest ${NEXTEST_VERSION}'; exit 0; fi\nmkdir -p "$CARGO_TARGET_DIR/nextest/compat"; echo '<testsuite/>' > "$CARGO_TARGET_DIR/nextest/compat/junit.xml"; exit 1\n`);
    await expect(runNextest(root, tree, "base", "learn", env)).rejects.toThrow("no failed tests");
    expect((await loadExclusions(root, "base"))?.failed_tests).toEqual(["upstream::failure"]);
  });
});
