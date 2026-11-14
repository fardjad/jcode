import { existsSync, lstatSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, unlinkSync } from "node:fs";
import { basename, join, relative, resolve, sep } from "node:path";
import { parseMetadata, type PatchMetadata } from "./metadata";
import { gitExecutable, shortTempRoot, withIsolatedEnv } from "./isolation";
import { atomicWrite } from "./primitives";
import { resolveBase } from "./sdk";
import { git, normalizedFormatPatch, prepareWorktree } from "./worktree";

export interface Patch { name: string; path: string; metadata: PatchMetadata }
const PATCH_NAME = /^\d{4}-[a-z0-9][a-z0-9-]*\.patch$/;

function patchPaths(root: string): string[] {
  const directory = join(root, "patches");
  if (lstatSync(directory).isSymbolicLink()) throw new Error(`patches directory must not be a symlink: ${directory}`);
  const paths = readdirSync(directory).filter(name => name.endsWith(".patch")).sort().map(name => join(directory, name));
  for (const path of paths) {
    if (!PATCH_NAME.test(basename(path)) || !lstatSync(path).isFile()) throw new Error(`invalid patch file: ${path}`);
  }
  return paths;
}

/** Parse each catalog patch once and enforce lexicographic dependency order. */
export async function discoverPatches(root: string): Promise<Patch[]> {
  root = resolve(root);
  const paths = patchPaths(root);
  if (!paths.length) throw new Error("patch catalog is empty");
  const names = new Set(paths.map(path => basename(path)));
  return paths.map(path => {
    const name = basename(path);
    let metadata: PatchMetadata;
    try { metadata = parseMetadata(readFileSync(path, "utf8")); }
    catch (error) { throw new Error(`${name}: ${error instanceof Error ? error.message : String(error)}`, { cause: error }); }
    if (metadata.filename !== name) throw new Error(`X-Jcode-Patch-File does not match filename: ${name}`);
    if (metadata.dependencies.some(dependency => !names.has(dependency) || dependency >= name)) throw new Error(`invalid dependencies: ${name}`);
    return { name, path, metadata };
  });
}

function closure(catalog: Patch[], name: string): Patch[] {
  const byName = new Map(catalog.map(patch => [patch.name, patch]));
  if (!byName.has(name)) throw new Error(`unknown patch: ${name}`);
  const selected = new Set<string>();
  const visit = (current: string): void => {
    if (selected.has(current)) return;
    const patch = byName.get(current);
    if (!patch) throw new Error(`unknown patch dependency: ${current}`);
    selected.add(current);
    for (const dependency of patch.metadata.dependencies) visit(dependency);
  };
  visit(name);
  return catalog.filter(patch => selected.has(patch.name));
}

export async function dependencyOrder(root: string, name: string): Promise<Patch[]> {
  const leaf = basename(name);
  if (name !== leaf && name !== `patches/${leaf}` && resolve(name) !== resolve(root, "patches", leaf)) throw new Error(`patch must be under patches: ${name}`);
  return closure(await discoverPatches(root), leaf);
}

/** Check only added source payload, not commit prose, removed lines, or binary patches. */
function checkWhitespace(root: string, patch: Patch, text: string): void {
  let target = "<unknown>", inDiff = false, binary = false;
  let hunk: { oldLeft: number; newLeft: number; line: number } | undefined;
  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]!;
    if (hunk && hunk.oldLeft === 0 && hunk.newLeft === 0) hunk = undefined;
    if (!hunk && line === "-- ") break;
    if (line.startsWith("diff --git ")) { inDiff = true; binary = false; target = "<unknown>"; hunk = undefined; continue; }
    if (!inDiff || binary) continue;
    if (hunk) {
      if (line === "\\ No newline at end of file") continue;
      if (line.startsWith("+")) {
        const payload = line.slice(1);
        const issues: string[] = [];
        if (/[ \t]$/.test(payload)) issues.push("trailing whitespace");
        if (/ \t/.test(/^[ \t]*/.exec(payload)![0])) issues.push("space before tab in leading indentation");
        if (/^(?:<{7}|={7}|>{7})(?:\s|$)/.test(payload)) issues.push("conflict marker");
        if (issues.length) throw new Error(`whitespace error in ${relative(root, patch.path)}:${index + 1} (${target}:${hunk.line}): ${issues.join(" and ")}`);
        hunk.line++; hunk.newLeft--; continue;
      }
      if (line.startsWith("-")) { hunk.oldLeft--; continue; }
      if (line.startsWith(" ") || line === "") { hunk.oldLeft--; hunk.newLeft--; hunk.line++; continue; }
      hunk = undefined;
    }
    if (line.startsWith("GIT binary patch") || line.startsWith("Binary files ")) { binary = true; continue; }
    if (line.startsWith("+++ ")) { target = line.slice(4).split("\t")[0]!.replace(/^"|"$/g, "").replace(/^b\//, ""); continue; }
    const match = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line);
    if (match) hunk = { oldLeft: Number(match[2] ?? 1), newLeft: Number(match[4] ?? 1), line: Number(match[3]) };
  }
}

async function temporaryWorktree<T>(root: string, base: string, action: (path: string) => Promise<T>): Promise<T> {
  const temporary = mkdtempSync(join(shortTempRoot(), "jcode-patches-"));
  const path = join(temporary, "worktree");
  let created = false;
  try {
    await prepareWorktree(root, "temporary", base, [], path);
    created = true;
    return await action(path);
  } finally {
    if (created || existsSync(join(path, ".git"))) {
      const removal = Bun.spawnSync([gitExecutable(root), "worktree", "remove", "--force", path], { cwd: root, stdout: "pipe", stderr: "pipe" });
      if (removal.exitCode !== 0) throw new Error(`cannot remove temporary worktree ${path}: ${removal.stderr.toString()}`);
    }
    rmSync(temporary, { recursive: true, force: true });
  }
}

async function apply(path: string, patches: Patch[]): Promise<void> {
  if (!patches.length) return;
  await withIsolatedEnv(env => git(path, ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "am", "--3way", ...patches.map(patch => patch.path)], { env }));
}

export async function checkCatalog(root: string): Promise<void> {
  root = resolve(root);
  if (git(root, ["branch", "--show-current"]) !== "personalized") throw new Error("current branch must be personalized");
  const catalog = await discoverPatches(root);
  const tracked = git(root, ["ls-files", "--", "patches/*"]).split("\n").filter(Boolean).sort();
  const expected = catalog.map(patch => relative(root, patch.path).split(sep).join("/")).sort();
  if (JSON.stringify(tracked) !== JSON.stringify(expected)) throw new Error(`tracked patches do not match catalog; expected ${expected.join(" ")}; tracked ${tracked.join(" ")}`);
  for (const patch of catalog) {
    const text = readFileSync(patch.path, "utf8");
    if (!/\r?\n---\r?\n/.test(text) || !/\r?\n\r?\n/.test(text.split(/\r?\n---\r?\n/, 1)[0]!)) throw new Error(`missing patch body section: ${patch.name}`);
    checkWhitespace(root, patch, text);
  }
  const base = resolveBase(root);
  await temporaryWorktree(root, base, async path => {
    await apply(path, catalog);
    console.log(`patch catalog valid: tree ${git(path, ["rev-parse", "HEAD^{tree}"])}`);
  });
}

export async function checkIsolation(root: string): Promise<void> {
  root = resolve(root);
  const catalog = await discoverPatches(root);
  const base = resolveBase(root);
  await temporaryWorktree(root, base, async path => {
    const failures: string[] = [];
    for (const patch of catalog) {
      git(path, ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "reset", "--hard", base]);
      try { await apply(path, closure(catalog, patch.name)); console.log(`ok ${patch.name}`); }
      catch (error) {
        Bun.spawnSync([gitExecutable(path), "am", "--abort"], { cwd: path, stdout: "ignore", stderr: "ignore" });
        failures.push(`${patch.name}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }
    if (failures.length) throw new Error(`patch isolation failures:\n${failures.join("\n")}`);
  });
}

export async function createPatched(root: string): Promise<void> {
  const catalog = await discoverPatches(root);
  await prepareWorktree(root, "patched-jcode", resolveBase(root), catalog, join(root, ".patched-jcode"));
}

function commits(root: string): { path: string; hashes: string[] } {
  const path = join(root, ".patched-jcode");
  if (!existsSync(join(path, ".git"))) throw new Error(`patched worktree not found at ${path}`);
  if (lstatSync(path).isSymbolicLink()) throw new Error(`patched worktree is a symlink: ${path}`);
  const actual = realpathSync(path);
  if (realpathSync(git(path, ["rev-parse", "--show-toplevel"])) !== actual ||
      realpathSync(resolve(path, git(path, ["rev-parse", "--git-common-dir"]))) !== realpathSync(resolve(root, git(root, ["rev-parse", "--git-common-dir"])))) {
    throw new Error(`patched worktree belongs to a different repository: ${path}`);
  }
  const registered = git(root, ["worktree", "list", "--porcelain"]).split("\n").filter(line => line.startsWith("worktree ")).map(line => {
    const location = line.slice(9);
    return existsSync(location) ? realpathSync(location) : resolve(location);
  });
  if (!registered.includes(actual)) throw new Error(`patched worktree is not registered: ${path}`);
  const base = resolveBase(root);
  git(path, ["merge-base", "--is-ancestor", base, "HEAD"]);
  const hashes = git(path, ["rev-list", "--reverse", `${base}..HEAD`]).split("\n").filter(Boolean);
  if (!hashes.length) throw new Error("no commits above pinned release in patched worktree");
  return { path, hashes };
}

export async function snapshotPatches(root: string, prune: boolean): Promise<void> {
  root = resolve(root);
  const { path, hashes } = commits(root);
  const directory = join(root, "patches");
  const existing = new Map(patchPaths(root).map(path => [basename(path), readFileSync(path, "utf8")]));
  const seen = new Set<string>();
  const writes: { name: string; text: string }[] = [];
  let previous = "";
  for (const hash of hashes) {
    const message = git(path, ["show", "-s", "--format=%B", hash]);
    let metadata: PatchMetadata;
    try { metadata = parseMetadata(message); }
    catch (error) { throw new Error(`commit ${hash.slice(0, 8)}: ${error instanceof Error ? error.message : String(error)}`, { cause: error }); }
    const { filename: name, dependencies } = metadata;
    if (!PATCH_NAME.test(name)) throw new Error(`invalid X-Jcode-Patch-File filename: ${name}`);
    if (seen.has(name)) throw new Error(`duplicate X-Jcode-Patch-File name: ${name}`);
    if (previous && name <= previous) throw new Error(`patch filenames out of commit order: ${previous}, ${name}`);
    for (const dependency of dependencies) if (!seen.has(dependency)) throw new Error(`dependency ${dependency} must name an earlier commit`);
    const text = normalizedFormatPatch(path, hash);
    if (!text.startsWith("From ")) throw new Error(`cannot format commit ${hash}`);
    writes.push({ name, text });
    seen.add(name); previous = name;
  }
  const stale = [...existing.keys()].filter(name => !seen.has(name)).sort();
  if (stale.length && !prune) throw new Error(`stale patch files: ${stale.join(" ")}; rerun with --prune to delete`);
  for (const { name, text } of writes) {
    const old = existing.get(name);
    if (old !== text) await atomicWrite(join(directory, name), text, 0o644);
    console.log(`${old === undefined ? "new" : old === text ? "unchanged" : "regenerated"}: ${name}`);
  }
  for (const name of stale) { unlinkSync(join(directory, name)); console.log(`removed: ${name}`); }
}

export async function listPatches(root: string): Promise<void> {
  const { path, hashes } = commits(root);
  const disk = new Set(patchPaths(resolve(root)).map(path => basename(path)));
  for (const hash of hashes) {
    const message = git(path, ["show", "-s", "--format=%B", hash]);
    const matches = [...message.matchAll(/^X-Jcode-Patch-File:[ \t]*(.*)$/gm)];
    const name = matches.length === 1 && matches[0]![1]!.trim() ? matches[0]![1]!.trim() : "(missing X-Jcode-Patch-File)";
    disk.delete(name);
    console.log(`${hash.slice(0, 8)} -> ${name} (${message.split("\n")[0]})`);
  }
  for (const name of [...disk].sort()) console.log(`${name} (stale, no commit)`);
}
