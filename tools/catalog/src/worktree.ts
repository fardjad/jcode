import { existsSync, lstatSync, mkdirSync, readFileSync, realpathSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { gitExecutable, withIsolatedEnv } from "./isolation";
import { parseMetadata } from "./metadata";
import { atomicWrite } from "./primitives";
import type { Patch } from "./patches";

/** Run Git without suppressing diagnostics on failure. */
export function git(cwd: string, args: string[], options: { env?: Record<string, string>; raw?: boolean; lockFds?: number[] } = {}): string {
  const stdio: ["ignore", "pipe", "pipe", ...("ignore" | number)[]] = ["ignore", "pipe", "pipe"];
  for (const fd of options.lockFds ?? []) {
    if (!Number.isSafeInteger(fd) || fd < 3) throw new Error(`invalid lock descriptor: ${fd}`);
    while (stdio.length <= fd) stdio.push("ignore");
    stdio[fd] = fd;
  }
  const result = (() => {
    try {
      return Bun.spawnSync([gitExecutable(cwd, options.env ?? process.env), ...args], { cwd, env: options.env ?? process.env, stdio });
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const code = error instanceof Error && "code" in error && typeof error.code === "string" ? ` (${error.code})` : "";
      throw new Error(`git ${args.join(" ")} failed to start${code}: ${detail}`, { cause: error });
    }
  })();
  if (result.exitCode !== 0) {
    const detail = [
      `exitCode: ${result.exitCode}`,
      ...(result.signalCode != null ? [`signalCode: ${result.signalCode}`] : []),
      ...(result.stdout.length ? [`stdout:\n${result.stdout.toString().trimEnd()}`] : []),
      ...(result.stderr.length ? [`stderr:\n${result.stderr.toString().trimEnd()}`] : []),
    ].join("\n");
    throw new Error(`git ${args.join(" ")} failed\n${detail}`);
  }
  const output = result.stdout.toString();
  return options.raw ? output : output.trim();
}

export function normalizedFormatPatch(worktree: string, commit: string, run: typeof git = git): string {
  return run(worktree, ["-c", "diff.suppressBlankEmpty=true", "format-patch", "--no-signature", "--stdout", "-1", commit], { raw: true })
    .replace(/^From [0-9a-f]{40,64} /, match => `From ${"0".repeat(match.length - 6)} `);
}

function commonDirectory(root: string, run: typeof git): string {
  return realpathSync(resolve(root, run(root, ["rev-parse", "--git-common-dir"])));
}

function markerPath(path: string, run: typeof git): string {
  const value = run(path, ["rev-parse", "--git-path", "jcode-catalog-worktree.json"]);
  return isAbsolute(value) ? value : resolve(path, value);
}

function hasPersistedCommits(root: string, path: string, base: string, run: typeof git): boolean {
  try {
    run(path, ["merge-base", "--is-ancestor", base, "HEAD"]);
    const commits = run(path, ["rev-list", "--reverse", `${base}..HEAD`]).split("\n").filter(Boolean);
    let last = "";
    for (const commit of commits) {
      const message = run(path, ["show", "-s", "--format=%B", commit]);
      const metadata = parseMetadata(message);
      const name = metadata.filename;
      if (!/^\d{4}-[a-z0-9][a-z0-9-]*\.patch$/.test(name) || name <= last) return false;
      const file = join(root, "patches", name);
      if (lstatSync(file).isSymbolicLink() || readFileSync(file, "utf8") !== normalizedFormatPatch(path, commit, run)) return false;
      last = name;
    }
    return true;
  } catch {
    return false;
  }
}

function hasToolAppliedCommits(path: string, base: string, run: typeof git): boolean {
  try {
    run(path, ["merge-base", "--is-ancestor", base, "HEAD"]);
    const commits = run(path, ["rev-list", "--reverse", `${base}..HEAD`]).split("\n").filter(Boolean);
    let last = "";
    for (const commit of commits) {
      const committer = run(path, ["show", "-s", "--format=%cn <%ce>", commit]);
      if (committer !== "jcode validation <jcode-validation@example.invalid>") return false;
      const metadata = parseMetadata(run(path, ["show", "-s", "--format=%B", commit]));
      if (!/^\d{4}-[a-z0-9][a-z0-9-]*\.patch$/.test(metadata.filename) || metadata.filename <= last) return false;
      last = metadata.filename;
    }
    return true;
  } catch {
    return false;
  }
}

function isOwnedHead(path: string, head: string, base: string, run: typeof git): boolean {
  const file = markerPath(path, run);
  if (!existsSync(file)) return hasToolAppliedCommits(path, base, run);
  try {
    const marker: unknown = JSON.parse(readFileSync(file, "utf8"));
    return typeof marker === "object" && marker !== null && "head" in marker && marker.head === head;
  } catch {
    return false;
  }
}

/** Git can overwrite ignored files during checkout; detect all affected paths first. */
function protectUntrackedFiles(path: string, base: string, patches: Patch[], run: typeof git): void {
  const tracked = new Set(run(path, ["ls-files", "-z"], { raw: true }).split("\0").filter(Boolean));
  const targets = new Set(run(path, ["diff", "--name-only", "--no-renames", "-z", "HEAD", base, "--"], { raw: true }).split("\0").filter(Boolean));
  for (const patch of patches) {
    const records = run(path, ["apply", "--numstat", "-z", resolve(patch.path)], { raw: true }).split("\0");
    for (let index = 0; index < records.length && records[index]; index++) {
      const record = records[index]!;
      const separator = record.indexOf("\t", record.indexOf("\t") + 1);
      if (separator < 0) throw new Error(`invalid patch file list: ${patch.name}`);
      const filename = record.slice(separator + 1);
      if (filename) targets.add(filename);
      else {
        const before = records[++index], after = records[++index];
        if (!before || !after) throw new Error(`invalid patch rename: ${patch.name}`);
        targets.add(before); targets.add(after);
      }
    }
  }
  for (const target of targets) {
    const parts = target.split("/");
    if (target.startsWith("/") || parts.some(part => !part || part === "." || part === "..")) throw new Error(`invalid patch path: ${target}`);
    for (let index = 1; index <= parts.length; index++) {
      const name = parts.slice(0, index).join("/");
      let stat;
      try { stat = lstatSync(join(path, name)); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT" || (error as NodeJS.ErrnoException).code === "ENOTDIR") break;
        throw error;
      }
      if (!tracked.has(name) && (index === parts.length || !stat.isDirectory())) {
        throw new Error(`refusing to overwrite untracked or ignored path: ${join(path, name)}`);
      }
    }
  }
}

/** Create or safely reuse a detached registered worktree, then apply its stack. */
export async function prepareWorktree(root: string, name: string, base: string, patches: Patch[], destination?: string, lockFds: number[] = []): Promise<string> {
  const run: typeof git = (cwd, args, options = {}) => git(cwd, args, { ...options, lockFds });
  root = realpathSync(root);
  if (!name || basename(name) !== name || name.includes("..") || name.includes("\\")) throw new Error(`invalid worktree name: ${name}`);
  const path = resolve(destination ?? join(commonDirectory(root, run), "patch-worktrees", `patch-worktree-${name.replace(/\.patch$/, "")}`));
  mkdirSync(dirname(path), { recursive: true });
  if (existsSync(path) || (() => { try { lstatSync(path); return true; } catch { return false; } })()) {
    if (lstatSync(path).isSymbolicLink()) throw new Error(`worktree destination is a symlink: ${path}`);
    const actual = realpathSync(path);
    if (actual === root) throw new Error(`refusing to reuse catalog checkout: ${path}`);
    let top: string, common: string;
    try {
      top = realpathSync(run(path, ["rev-parse", "--show-toplevel"]));
      common = commonDirectory(path, run);
    } catch (error) {
      throw new Error(`existing path is not a valid Git worktree: ${path}`, { cause: error });
    }
    if (top !== actual || common !== commonDirectory(root, run)) throw new Error(`worktree destination is not a registered catalog worktree: ${path}`);
    const registered = run(root, ["worktree", "list", "--porcelain"]).split("\n").filter(line => line.startsWith("worktree ")).map(line => {
      const location = line.slice(9);
      return existsSync(location) ? realpathSync(location) : resolve(location);
    });
    if (!registered.includes(actual)) throw new Error(`path is not a registered Git worktree: ${path}`);
    for (const state of ["rebase-apply", "rebase-merge", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"]) {
      const gitPath = run(path, ["rev-parse", "--git-path", state]);
      if (existsSync(isAbsolute(gitPath) ? gitPath : resolve(path, gitPath))) throw new Error(`existing worktree has an in-progress Git operation: ${path}`);
    }
    if (run(path, ["status", "--porcelain", "--untracked-files=all"])) throw new Error(`existing worktree is dirty: ${path}`);
    const head = run(path, ["rev-parse", "HEAD"]);
    if (run(path, ["branch", "--show-current"])) throw new Error(`existing worktree has a checked-out branch: ${path}`);
    const patched = resolve(root, ".patched-jcode") === actual;
    if (!hasPersistedCommits(root, path, base, run) && (patched || !isOwnedHead(path, head, base, run))) {
      throw new Error(`existing worktree has unpersisted commits: ${path}; snapshot them before resetting`);
    }
    protectUntrackedFiles(path, base, patches, run);
    run(path, ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "checkout", "--detach", base]);
    run(path, ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "reset", "--hard", base]);
  } else {
    run(root, ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "worktree", "add", "--detach", path, base]);
    run(path, ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "reset", "--hard", base]);
  }
  if (patches.length) {
    try {
      await withIsolatedEnv(env => run(path, ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "am", "--3way", ...patches.map(patch => resolve(patch.path))], { env }));
    } catch (error) {
      throw new Error(`git am conflict in ${path}; resolve and run git -C ${path} am --continue, or git -C ${path} am --abort\n${error instanceof Error ? error.message : String(error)}`, { cause: error });
    }
  }
  await atomicWrite(markerPath(path, run), JSON.stringify({ head: run(path, ["rev-parse", "HEAD"]) }) + "\n");
  return path;
}
