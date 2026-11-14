import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { copyFile, mkdir, readFile, rm } from "node:fs/promises";
import { basename, join } from "node:path";
import { inflateRawSync } from "node:zlib";
import { atomicWrite, withFileLock } from "./primitives";

export const NEXTEST_VERSION = "0.9.149";
const RELEASES_URL = "https://get.nexte.st/releases.json";
const USER_AGENT = "jcode-nextest-bootstrap/1.0";

export type Output = (text: string) => void;

/** Execute a command while forwarding output as it arrives. */
export async function runCommand(args: string[], options: { cwd: string; env: Record<string, string>; output?: Output; lockFds?: number[] }): Promise<{ code: number; stdout: string }> {
  const stdio: ["ignore", "pipe", "pipe", ...("ignore" | number)[]] = ["ignore", "pipe", "pipe"];
  if (process.platform !== "win32") for (const fd of options.lockFds ?? []) {
    if (!Number.isSafeInteger(fd) || fd < 3) throw new Error(`invalid lock descriptor: ${fd}`);
    while (stdio.length <= fd) stdio.push("ignore");
    stdio[fd] = fd;
  }
  const child = Bun.spawn(args, { cwd: options.cwd, env: options.env, stdio });
  let stdout = "";
  const pump = async (stream: ReadableStream<Uint8Array>, isStdout: boolean) => {
    const decoder = new TextDecoder();
    for await (const bytes of stream) {
      const text = decoder.decode(bytes, { stream: true });
      if (isStdout) stdout += text;
      (isStdout ? process.stdout : process.stderr).write(text);
      options.output?.(text);
    }
    const tail = decoder.decode();
    if (tail) {
      if (isStdout) stdout += tail;
      (isStdout ? process.stdout : process.stderr).write(tail);
      options.output?.(tail);
    }
  };
  await Promise.all([pump(child.stdout, true), pump(child.stderr, false)]);
  return { code: await child.exited, stdout };
}

export function nextestTarget(platform = process.platform, arch = process.arch): string {
  const machine = arch === "x64" ? "x86_64" : arch === "arm64" ? "aarch64" : arch;
  if (!["x86_64", "aarch64"].includes(machine)) throw new Error(`unsupported cargo-nextest platform: ${platform} ${arch}`);
  if (platform === "darwin") return "universal-apple-darwin";
  if (platform === "win32") return `${machine}-pc-windows-msvc`;
  if (platform === "linux") {
    let libc = "";
    try {
      const ldd = Bun.spawnSync(["ldd", "--version"], { stdout: "pipe", stderr: "pipe" });
      libc = `${ldd.stdout.toString()} ${ldd.stderr.toString()}`.toLowerCase();
    } catch { /* The GNU target is the legacy fallback when ldd is unavailable. */ }
    return `${machine}-unknown-linux-${libc.includes("musl") ? "musl" : "gnu"}`;
  }
  throw new Error(`unsupported cargo-nextest platform: ${platform} ${arch}`);
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}
export function findNextestAsset(document: unknown, target: string): { url: string; sha256: string } {
  const doc = record(document);
  const projects = record(doc?.projects);
  const project = record(projects?.["cargo-nextest"]);
  const ranges = record(project?.ranges);
  const range = record(ranges?.["0.9"]);
  const versions = record(range?.versions);
  const version = record(versions?.[NEXTEST_VERSION]);
  const candidates: unknown = projects ? version?.locations ?? [] : Array.isArray(document) ? document : doc?.releases ?? [];
  if (!Array.isArray(candidates)) throw new Error("invalid nextest release metadata");
  for (const item of candidates) {
    const release = record(item);
    if (!release || (release.version !== undefined && String(release.version) !== NEXTEST_VERSION)) continue;
    const assets = release.assets ?? [release];
    if (!Array.isArray(assets)) continue;
    for (const item of assets) {
      const asset = record(item);
      if (!asset || asset.target !== target) continue;
      const url = asset.url ?? asset.download_url;
      const checksum = asset.sha256 ?? asset.checksum ?? record(asset.checksums)?.sha256;
      if (typeof url === "string" && typeof checksum === "string") {
        const sha256 = checksum.replace(/^sha256:/, "").toLowerCase();
        if (!/^[0-9a-f]{64}$/.test(sha256)) throw new Error("invalid cargo-nextest checksum");
        return { url, sha256 };
      }
    }
  }
  throw new Error(`no cargo-nextest ${NEXTEST_VERSION} asset for ${target}`);
}

async function fetchChecked(url: string, timeout: number): Promise<Response> {
  const response = await fetch(url, { headers: { "User-Agent": USER_AGENT }, signal: AbortSignal.timeout(timeout) });
  if (!response.ok) throw new Error(`download failed (${response.status}): ${url}`);
  return response;
}

/** Extract only the executable; never unpack archive paths onto disk. */
export async function extractNextestExecutable(data: Uint8Array, executable: string): Promise<Uint8Array> {
  if (data[0] !== 0x50 || data[1] !== 0x4b) {
    const entries = await new Bun.Archive(data).files();
    const matches = [...entries].filter(([name]) => !name.startsWith("/") && !name.split("/").includes("..") && basename(name) === executable);
    if (matches.length !== 1) throw new Error("cargo-nextest archive must contain exactly one safe executable");
    return new Uint8Array(await matches[0]![1].arrayBuffer());
  }
  const bytes = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (bytes.readUInt32LE(i) === 0x06054b50 && i + 22 + bytes.readUInt16LE(i + 20) === bytes.length) { end = i; break; }
  }
  if (end < 0 || bytes.readUInt16LE(end + 4) || bytes.readUInt16LE(end + 6) || bytes.readUInt16LE(end + 8) !== bytes.readUInt16LE(end + 10)) throw new Error("invalid cargo-nextest ZIP directory");
  const count = bytes.readUInt16LE(end + 10), size = bytes.readUInt32LE(end + 12), offset = bytes.readUInt32LE(end + 16);
  if (offset + size !== end) throw new Error("invalid cargo-nextest ZIP directory");
  let position = offset, result: Uint8Array | undefined;
  for (let i = 0; i < count; i++) {
    if (position + 46 > end || bytes.readUInt32LE(position) !== 0x02014b50) throw new Error("invalid cargo-nextest ZIP entry");
    const flags = bytes.readUInt16LE(position + 8), method = bytes.readUInt16LE(position + 10);
    const compressed = bytes.readUInt32LE(position + 20), uncompressed = bytes.readUInt32LE(position + 24);
    const nameLength = bytes.readUInt16LE(position + 28), extraLength = bytes.readUInt16LE(position + 30), commentLength = bytes.readUInt16LE(position + 32);
    const attributes = bytes.readUInt32LE(position + 38), local = bytes.readUInt32LE(position + 42);
    const next = position + 46 + nameLength + extraLength + commentLength;
    if (next > end) throw new Error("invalid cargo-nextest ZIP entry");
    const name = bytes.subarray(position + 46, position + 46 + nameLength).toString("utf8");
    const parts = name.split(/[\\/]/);
    const safe = !name.startsWith("/") && !name.startsWith("\\") && !/^[A-Za-z]:/.test(name) && !parts.includes("..");
    if (safe && parts.at(-1) === executable) {
      const type = (attributes >>> 16) & 0o170000;
      if (result || (type !== 0 && type !== 0o100000) || (flags & 1) || ![0, 8].includes(method)) throw new Error("unsafe cargo-nextest ZIP executable");
      if (local + 30 > offset || bytes.readUInt32LE(local) !== 0x04034b50 || bytes.readUInt16LE(local + 6) !== flags || bytes.readUInt16LE(local + 8) !== method) throw new Error("invalid cargo-nextest ZIP executable");
      const localNameLength = bytes.readUInt16LE(local + 26), localExtraLength = bytes.readUInt16LE(local + 28);
      const start = local + 30 + localNameLength + localExtraLength;
      if (start + compressed > offset || bytes.subarray(local + 30, local + 30 + localNameLength).toString("utf8") !== name) throw new Error("invalid cargo-nextest ZIP executable");
      const content = bytes.subarray(start, start + compressed);
      result = method === 8 ? inflateRawSync(content, { maxOutputLength: uncompressed }) : content;
      if (result.length !== uncompressed) throw new Error("invalid cargo-nextest ZIP executable size");
    }
    position = next;
  }
  if (position !== end || !result) throw new Error("cargo-nextest archive must contain exactly one safe executable");
  return result;
}

export async function ensureNextest(root: string, env: Record<string, string>): Promise<string> {
  const target = nextestTarget();
  const executable = process.platform === "win32" ? "cargo-nextest.exe" : "cargo-nextest";
  const cached = join(root, ".tools", "nextest", NEXTEST_VERSION, target, executable);
  return withFileLock(`${cached}.lock`, async () => {
    if (!existsSync(cached)) {
      const asset = findNextestAsset(await (await fetchChecked(RELEASES_URL, 30_000)).json(), target);
      const data = new Uint8Array(await (await fetchChecked(asset.url, 120_000)).arrayBuffer());
      const actual = createHash("sha256").update(data).digest("hex");
      if (actual !== asset.sha256) throw new Error(`cargo-nextest checksum mismatch: expected ${asset.sha256}, got ${actual}`);
      await atomicWrite(cached, await extractNextestExecutable(data, executable), 0o755);
    }
    const result = Bun.spawnSync([cached, "--version"], { cwd: root, env, stdout: "pipe", stderr: "pipe" });
    const reported = result.stdout.toString().trim();
    const words = reported.split(/\s+/);
    if (result.exitCode !== 0 || words[0] !== "cargo-nextest" || words[1] !== NEXTEST_VERSION) {
      throw new Error(`cached cargo-nextest reports unexpected version: ${reported}`);
    }
    return cached;
  });
}

export function exclusionsPath(root: string, base: string): string {
  return join(root, ".tools", "nextest", "exclusions", `${base}.json`);
}

type Exclusions = { base_commit: string; nextest_version: string; nextest_pin: string; failed_tests: string[]; failed_validation_tests?: string[] };
function names(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.some(v => typeof v !== "string" || !v || v.trim() !== v || /[\x00-\x1f]/.test(v)) || JSON.stringify(value) !== JSON.stringify([...new Set(value)].sort())) {
    throw new Error(`${field} must contain sorted unique non-empty test names`);
  }
  return value;
}
export async function loadExclusions(root: string, base: string): Promise<Exclusions | null> {
  const path = exclusionsPath(root, base);
  if (!existsSync(path)) return null;
  try {
    const data = JSON.parse(await readFile(path, "utf8"));
    if (!data || typeof data !== "object") throw new Error("expected object");
    if (data.base_commit !== base || data.nextest_version !== NEXTEST_VERSION) return null;
    names(data.failed_tests, "failed_tests");
    names(data.failed_validation_tests ?? [], "failed_validation_tests");
    return data as Exclusions;
  } catch (error) {
    throw new Error(`invalid nextest exclusions ${path}`, { cause: error });
  }
}
export async function saveExclusions(root: string, base: string, nextest: string[], validation: string[]): Promise<void> {
  const payload: Exclusions = { base_commit: base, nextest_version: NEXTEST_VERSION, nextest_pin: NEXTEST_VERSION, failed_tests: names([...new Set(nextest)].sort(), "failed_tests"), failed_validation_tests: names([...new Set(validation)].sort(), "failed_validation_tests") };
  await atomicWrite(exclusionsPath(root, base), `${JSON.stringify(payload, null, 2)}\n`);
}

function decodeXml(value: string): string {
  if (/<|&(?!#(?:x[\da-f]+|\d+);|(?:amp|lt|gt|quot|apos);)/i.test(value)) throw new Error("invalid JUnit attribute");
  return value.replace(/&([^;]+);/g, (_, entity: string) => {
    const builtin: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
    if (entity in builtin) return builtin[entity]!;
    const numeric = /^#x([0-9a-f]+)$/i.exec(entity) ?? /^#([0-9]+)$/.exec(entity);
    if (numeric) {
      const cp = parseInt(numeric[1]!, entity[1]?.toLowerCase() === "x" ? 16 : 10);
      if (cp > 0 && cp <= 0x10ffff && !(cp >= 0xd800 && cp <= 0xdfff)) return String.fromCodePoint(cp);
    }
    throw new Error(`invalid JUnit entity: &${entity};`);
  });
}

/** Read JUnit failures without trusting malformed XML or ambiguous test names. */
export function parseJUnitFailures(xml: string): string[] {
  const tags = /<!--[\s\S]*?-->|<\?[\s\S]*?\?>|<!\[CDATA\[[\s\S]*?\]\]>|<(?:[^>"']|"[^"]*"|'[^']*')*>/g;
  const stack: { tag: string; name?: string; failed?: boolean }[] = [];
  const found = new Set<string>();
  let position = 0, roots = 0, hasFailureCount = false;
  for (const match of xml.matchAll(tags)) {
    const text = xml.slice(position, match.index);
    if (text.includes("<") || (!stack.length && text.trim()) || /&(?!#(?:x[\da-f]+|\d+);|(?:amp|lt|gt|quot|apos);)/i.test(text)) throw new Error("invalid JUnit XML text");
    position = match.index! + match[0].length;
    const token = match[0];
    if (token.startsWith("<!--") || token.startsWith("<?") || token.startsWith("<![CDATA[")) continue;
    if (token.startsWith("<!")) throw new Error("unsupported JUnit XML declaration");
    if (token.startsWith("</")) {
      const close = /^<\/\s*([\w:.-]+)\s*>$/.exec(token);
      const entry = stack.pop();
      if (!close || !entry || entry.tag !== close[1]) throw new Error("invalid JUnit XML nesting");
      if (entry.tag.split(":").pop() === "testcase" && entry.failed) {
        const name = entry.name ?? "";
        if (!name || name.trim() !== name || /[\x00-\x1f]/.test(name)) throw new Error("JUnit failure has invalid test name");
        if (found.has(name)) throw new Error(`JUnit failure has ambiguous duplicate test name: ${name}`);
        found.add(name);
      }
      continue;
    }
    const start = /^<([\w:.-]+)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>$/.exec(token);
    if (!start) throw new Error("invalid JUnit XML tag");
    const attrs: Record<string, string> = {};
    for (const attr of start[2]!.matchAll(/([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) {
      if (attr[1]! in attrs) throw new Error("duplicate JUnit XML attribute");
      attrs[attr[1]!] = decodeXml(attr[2] ?? attr[3] ?? "");
    }
    const local = start[1]!.split(":").pop();
    if (!stack.length) {
      roots++;
      if (local !== "testsuite" && local !== "testsuites") throw new Error("invalid JUnit root");
    }
    if (local === "testsuite" || local === "testsuites") {
      for (const key of ["failures", "errors"]) if (attrs[key] !== undefined) {
        if (!/^\d+$/.test(attrs[key]!)) throw new Error("JUnit has invalid failure counts");
        if (Number(attrs[key]) > 0) hasFailureCount = true;
      }
    }
    if ((local === "failure" || local === "error") && stack.at(-1)?.tag.split(":").pop() === "testcase") stack.at(-1)!.failed = true;
    const entry = { tag: start[1]!, name: attrs.name, failed: false };
    if (!start[3]) stack.push(entry);
  }
  if (stack.length || roots !== 1 || xml.slice(position).trim()) throw new Error("invalid JUnit XML document");
  if (!found.size && hasFailureCount) throw new Error("JUnit reports failures but no usable failed test names");
  return [...found].sort();
}

export async function runNextest(root: string, worktree: string, base: string, mode: "test" | "learn", env: Record<string, string>, output?: Output, lockFds?: number[]): Promise<string[]> {
  const binary = await ensureNextest(root, env);
  const exclusions = mode === "learn" ? [] : (await loadExclusions(root, base))?.failed_tests ?? [];
  const args = [binary, "nextest", "run", "--manifest-path", join(worktree, "Cargo.toml"), "--config-file", join(root, "nextest.toml"), "--profile", "compat", "--lib", "--bin", "jcode"];
  if (exclusions.length) args.push("--filterset", exclusions.map(name => `not test(=${name.replace(/[\\/),]/g, "\\$&")})`).join(" and "));
  const report = join(worktree, "target", "nextest", "compat", "junit.xml");
  const targetReport = join(env.CARGO_TARGET_DIR!, "nextest", "compat", "junit.xml");
  await rm(report, { force: true });
  if (targetReport !== report) await rm(targetReport, { force: true });
  const result = await runCommand(args, { cwd: worktree, env: { ...env, JCODE_DEV_FEATURE_PROFILE: "minimal" }, output, lockFds });
  if (targetReport !== report && existsSync(targetReport)) {
    await mkdir(join(worktree, "target", "nextest", "compat"), { recursive: true });
    await copyFile(targetReport, report);
  }
  if (mode === "test") {
    if (result.code !== 0) throw new Error(`compatibility test failed in ${worktree}; JUnit report: ${report}`);
    return [];
  }
  let learned: string[];
  try { learned = parseJUnitFailures(await readFile(report, "utf8")); }
  catch (error) { throw new Error(`cannot read JUnit report ${report}`, { cause: error }); }
  if (result.code !== 0 && learned.length === 0) throw new Error("nextest failed but JUnit contains no failed tests");
  return learned;
}
