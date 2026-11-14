import { dlopen, FFIType, read as ffiRead } from "bun:ffi";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, realpathSync, renameSync, rmSync, symlinkSync } from "node:fs";
import { basename, isAbsolute, join, relative, resolve, sep } from "node:path";
import { randomUUID } from "node:crypto";
import { atomicWrite, withFileLock } from "./primitives";

const UPSTREAM_URL = "https://github.com/1jehuang/jcode.git";
const STABLE_RELEASE = /^v[0-9]+\.[0-9]+\.[0-9]+$/;
const COMMIT = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const MARKER = ".jcode-sdk-version.json";

/** Read the catalog's canonical stable release pin. */
export function pinnedVersion(root: string): string {
  const path = join(root, "VERSION.txt");
  let version: string;
  try {
    version = readFileSync(path, "utf8").trim();
  } catch (error) {
    throw new Error(`cannot read catalog version: ${path}`, { cause: error });
  }
  if (!STABLE_RELEASE.test(version)) {
    throw new Error(`not a stable release tag: ${version}`);
  }
  return version;
}

function run(root: string, command: string, args: string[]): string {
  const result = Bun.spawnSync([command, ...args], {
    cwd: root,
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  if (result.exitCode !== 0) {
    const detail = result.stderr.toString().trim();
    throw new Error(`${command} ${args.join(" ")} failed${detail ? `: ${detail}` : ""}`);
  }
  return result.stdout.toString().trim();
}

function resolveRelease(root: string, version: string, fetchRelease: boolean): string {
  const ref = `refs/jcode/upstream-tags/${version}`;
  const check = Bun.spawnSync(["git", "show-ref", "--verify", "--quiet", ref], {
    cwd: root,
    stdin: "ignore",
    stdout: "ignore",
    stderr: "pipe",
  });
  if (check.exitCode !== 0 && check.exitCode !== 1) {
    throw new Error(`git show-ref failed: ${check.stderr.toString().trim()}`);
  }
  if (fetchRelease || check.exitCode === 1) {
    run(root, "git", ["fetch", "--no-tags", UPSTREAM_URL, `+refs/tags/${version}:${ref}`]);
  }
  const commit = run(root, "git", ["rev-parse", "--verify", `${ref}^{commit}`]);
  if (!COMMIT.test(commit)) throw new Error(`invalid commit for ${ref}: ${commit}`);
  return commit;
}

/** Resolve the pin via the private upstream ref, fetching the canonical tag if absent. */
export function resolveBase(root: string, options: { fetchRelease?: boolean } = {}): string {
  return resolveRelease(root, pinnedVersion(root), options.fetchRelease ?? false);
}

function isFile(path: string): boolean {
  try {
    return lstatSync(path).isFile();
  } catch {
    return false;
  }
}

function hasPinnedSdk(directory: string, cache: string, version: string, commit: string): boolean {
  try {
    // Never treat an external symlink (in particular one into .patched-jcode) as the cache.
    if (lstatSync(directory).isSymbolicLink()) {
      const actual = realpathSync(directory);
      const within = relative(realpathSync(cache), actual);
      if (!within || within === ".." || within.startsWith(`..${sep}`) || isAbsolute(within)) return false;
    }
    if (!lstatSync(join(directory, "src")).isDirectory() || !isFile(join(directory, MARKER)) || !isFile(join(directory, "package.json")) || !isFile(join(directory, "src", "index.ts"))) return false;
    const marker: unknown = JSON.parse(readFileSync(join(directory, MARKER), "utf8"));
    return typeof marker === "object" && marker !== null &&
      "version" in marker && marker.version === version &&
      "commit" in marker && marker.commit === commit;
  } catch {
    return false;
  }
}
// The extracted SDK lives outside tools/, so ordinary package resolution
// otherwise cannot reach the workspace's Ajv dependency.
function ensureSdkDependencies(root: string, directory: string): void {
  const link = join(directory, "node_modules");
  const target = join(root, "tools", "node_modules");
  try {
    if (!lstatSync(link).isSymbolicLink() || resolve(directory, readlinkSync(link)) !== target) {
      throw new Error(`unexpected SDK dependency path: ${link}`);
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    symlinkSync(target, link, "dir");
  }
}


type Exchange = {
  swap: (from: Uint8Array, to: Uint8Array) => number;
  errno: () => number | null;
};

// Keep the native library loaded for the lifetime of its function pointers.
let nativeExchange: Exchange | undefined;

function exchangeSdkPaths(from: string, to: string): void {
  if (!nativeExchange) {
    if (process.platform !== "linux" && process.platform !== "darwin") {
      throw new Error(`atomic SDK directory exchange is unavailable on ${process.platform}; existing SDK preserved`);
    }
    try {
      if (process.platform === "linux") {
        const libc = dlopen("libc.so.6", {
          renameat2: { args: [FFIType.i32, FFIType.ptr, FFIType.i32, FFIType.ptr, FFIType.u32], returns: FFIType.i32 },
          __errno_location: { args: [], returns: FFIType.ptr },
        });
        nativeExchange = {
          // AT_FDCWD = -100, RENAME_EXCHANGE = 2.
          swap: (a, b) => libc.symbols.renameat2(-100, a, -100, b, 2),
          errno: () => {
            const address = libc.symbols.__errno_location();
            return address === null ? null : ffiRead.i32(address);
          },
        };
      } else {
        const libc = dlopen("/usr/lib/libSystem.B.dylib", {
          renamex_np: { args: [FFIType.ptr, FFIType.ptr, FFIType.u32], returns: FFIType.i32 },
          __error: { args: [], returns: FFIType.ptr },
        });
        nativeExchange = {
          // RENAME_SWAP = 2.
          swap: (a, b) => libc.symbols.renamex_np(a, b, 2),
          errno: () => {
            const address = libc.symbols.__error();
            return address === null ? null : ffiRead.i32(address);
          },
        };
      }
    } catch (error) {
      throw new Error(`atomic SDK directory exchange is unavailable on ${process.platform}; existing SDK preserved`, { cause: error });
    }
  }
  if (nativeExchange.swap(Buffer.from(`${from}\0`), Buffer.from(`${to}\0`)) !== 0) {
    throw new Error(`atomic SDK directory exchange failed (errno ${nativeExchange.errno()}); existing SDK preserved`);
  }
}

/**
 * Materialize the pinned SDK without needing a patched worktree. Published SDK
 * directories are retained because another process may still be importing from
 * their real paths. Pointer replacement and physical-directory migration are
 * atomic; an unsupported directory exchange fails without touching the old SDK.
 */
export async function ensurePinnedSdk(root: string): Promise<string> {
  root = resolve(root);
  const cache = join(root, ".tools", "sdk");
  const destination = join(cache, "typescript");
  mkdirSync(cache, { recursive: true });

  return withFileLock(join(cache, ".typescript.lock"), async () => {
    const version = pinnedVersion(root);
    const commit = resolveRelease(root, version, false);
    if (hasPinnedSdk(destination, cache, version, commit)) {
      if (pinnedVersion(root) !== version || resolveRelease(root, version, false) !== commit) {
        throw new Error("SDK version or upstream ref changed during materialization");
      }
      ensureSdkDependencies(root, destination);
      return destination;
    }

    const work = mkdtempSync(join(cache, ".typescript-build-"));
    const tree = join(work, "tree");
    const archive = join(work, "source.tar");
    const target = join(cache, `.typescript-${randomUUID()}`);
    const link = join(cache, `.typescript-previous-${randomUUID()}`);
    let published = false;
    let targetCreated = false;
    let linkCreated = false;
    try {
      mkdirSync(tree);
      run(root, "git", ["archive", "--format=tar", `--output=${archive}`, commit, "sdk/typescript"]);
      run(root, "tar", ["-xf", archive, "--strip-components=2", "-C", tree]);
      if (!isFile(join(tree, "package.json")) || !isFile(join(tree, "src", "index.ts")) || !lstatSync(join(tree, "src")).isDirectory()) {
        throw new Error(`upstream SDK at ${commit} is missing package.json or src/index.ts`);
      }
      ensureSdkDependencies(root, tree);
      await atomicWrite(join(tree, MARKER), `${JSON.stringify({ version, commit }, null, 2)}\n`);

      // Do not publish a stale extraction if the pin or its ref changed while building.
      if (pinnedVersion(root) !== version || resolveRelease(root, version, false) !== commit) {
        throw new Error("SDK version or upstream ref changed during materialization");
      }

      renameSync(tree, target);
      targetCreated = true;
      symlinkSync(basename(target), link, "dir");
      linkCreated = true;
      if (existsSync(destination) && lstatSync(destination).isDirectory()) {
        // Exchange in one syscall: the old physical tree remains at `link`.
        exchangeSdkPaths(destination, link);
      } else {
        renameSync(link, destination);
      }
      published = true;
      return destination;
    } finally {
      rmSync(work, { recursive: true, force: true });
      if (!published) {
        if (linkCreated) rmSync(link, { force: true });
        if (targetCreated) rmSync(target, { recursive: true, force: true });
      }
    }
  });
}
