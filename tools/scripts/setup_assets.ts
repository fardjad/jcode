#!/usr/bin/env bun
import { chmodSync, constants, copyFileSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, unlinkSync } from "node:fs";
import type { Stats } from "node:fs";
import { homedir } from "node:os";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repository = fileURLToPath(new URL("../..", import.meta.url));
const unsupportedLinks: Record<string, true> = { EPERM: true, EACCES: true, ENOSYS: true, ENOTSUP: true };
const blueprints = ["coordinator.md", "routine.md", "advanced.md", "expert.md"];

type Asset = { name: string; source: string; allowlist?: Set<string> };
type Options = { copy?: boolean; link?: typeof symlinkSync };

function allowlist(source: string): Set<string> {
  const entries = new Set<string>();
  for (const line of readFileSync(join(source, ".gitignore"), "utf8").split(/\r?\n/)) {
    const match = /^!\/([^/]+)\/$/.exec(line.trim());
    if (match) entries.add(match[1]!);
  }
  return entries;
}

function assets(root: string): Asset[] {
  const custom = join(root, "customizations");
  const plugins = join(custom, "plugins"), skills = join(custom, "skills");
  const pluginNames = allowlist(plugins), skillNames = allowlist(skills);
  if (!pluginNames.has("rtk") || !skillNames.has("author-plan")) throw new Error("asset allowlists must include rtk and author-plan");
  for (const path of [join(plugins, "rtk", "rtk-transform"), join(skills, "author-plan", "SKILL.md"), join(custom, "config.toml"), ...blueprints.map(name => join(custom, "workers", "blueprints", name))]) {
    if (!statSync(path).isFile()) throw new Error(`required asset is not a file: ${path}`);
  }
  return [
    { name: "plugins", source: plugins, allowlist: pluginNames },
    { name: "worker-blueprints", source: join(custom, "workers", "blueprints") },
    { name: "skills", source: skills, allowlist: skillNames },
  ];
}

function existing(path: string): Stats | undefined {
  try { return lstatSync(path); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}

function managedLink(destination: string, source: string): boolean {
  const info = existing(destination);
  if (!info?.isSymbolicLink()) return false;
  try { return realpathSync(destination) === realpathSync(source); } catch { return false; }
}

function excluded(name: string): boolean {
  return name === ".git" || name === ".gitignore" || name === ".DS_Store" || name === "__pycache__" || name.endsWith(".swp") || name.endsWith(".test.ts") || /^test_.*\.py$/.test(name) || /\.py[cod]$/.test(name);
}

function copyTree(source: string, destination: string, names?: Set<string>, includeRootFiles = false): void {
  mkdirSync(destination, { mode: 0o755 });
  for (const entry of readdirSync(source, { withFileTypes: true })) {
    if (excluded(entry.name)) continue;
    if (names && !(entry.isDirectory() && names.has(entry.name)) && !(includeRootFiles && entry.isFile())) continue;
    const from = join(source, entry.name), to = join(destination, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`refusing to copy symlink in asset: ${from}`);
    if (entry.isDirectory()) copyTree(from, to);
    else if (entry.isFile()) {
      copyFileSync(from, to, constants.COPYFILE_EXCL);
      chmodSync(to, (statSync(from).mode & 0o111) ? 0o755 : 0o644);
    } else throw new Error(`unsupported asset entry: ${from}`);
  }
}

function installCopy(home: string, source: string, destination: string, names?: Set<string>, includeRootFiles = false): void {
  const staging = mkdtempSync(join(home, ".jcode-asset-"));
  const prepared = join(staging, "asset");
  let removedLink = false;
  try {
    if (statSync(source).isDirectory()) copyTree(source, prepared, names, includeRootFiles);
    else { copyFileSync(source, prepared); chmodSync(prepared, (statSync(source).mode & 0o111) ? 0o755 : 0o644); }
    const prior = existing(destination);
    if (prior) {
      if (!managedLink(destination, source)) throw new Error(`refusing to overwrite unmanaged asset: ${destination}`);
      unlinkSync(destination);
      removedLink = true;
    }
    try {
      if (statSync(prepared).isDirectory()) copyTree(prepared, destination);
      else { copyFileSync(prepared, destination, constants.COPYFILE_EXCL); chmodSync(destination, (statSync(prepared).mode & 0o111) ? 0o755 : 0o644); }
    }
    catch (error) {
      if (removedLink && !existing(destination)) symlinkSync(source, destination, statSync(source).isDirectory() ? "dir" : "file");
      throw error;
    }
  } finally { rmSync(staging, { recursive: true, force: true }); }
}

/** Install only catalog-managed assets, never replacing an unmanaged destination. */
export function installAssets(root: string, targetHome: string, options: Options = {}): string[] {
  const home = resolve(targetHome), entries = assets(root);
  const config = join(root, "customizations", "config.toml");
  mkdirSync(home, { recursive: true, mode: 0o700 });
  for (const asset of entries) {
    const destination = join(home, asset.name);
    if (existing(destination) && !managedLink(destination, asset.source)) throw new Error(`refusing to overwrite unmanaged asset: ${destination}`);
  }
  const messages: string[] = [];
  const link = options.link ?? symlinkSync;
  for (const asset of entries) {
    const destination = join(home, asset.name);
    if (options.copy) {
      installCopy(home, asset.source, destination, asset.allowlist, asset.name === "plugins");
      messages.push(`${asset.name}: copied`);
    } else if (managedLink(destination, asset.source)) messages.push(`${asset.name}: already linked`);
    else {
      try { link(asset.source, destination, "dir"); messages.push(`${asset.name}: linked`); }
      catch (error) {
        if (!unsupportedLinks[(error as NodeJS.ErrnoException).code ?? ""]) throw error;
        installCopy(home, asset.source, destination, asset.allowlist, asset.name === "plugins");
        messages.push(`${asset.name}: copied (symlinks unavailable)`);
      }
    }
  }
  const destination = join(home, "config.toml");
  if (existing(destination) && !managedLink(destination, config)) messages.push("config.toml: skipped existing unmanaged file");
  else if (options.copy) { installCopy(home, config, destination); messages.push("config.toml: copied"); }
  else if (managedLink(destination, config)) messages.push("config.toml: already linked");
  else {
    try { link(config, destination, "file"); messages.push("config.toml: linked"); }
    catch (error) {
      if (!unsupportedLinks[(error as NodeJS.ErrnoException).code ?? ""]) throw error;
      installCopy(home, config, destination);
      messages.push("config.toml: copied (symlinks unavailable)");
    }
  }
  return messages;
}

if (import.meta.main) {
  try {
    const args = process.argv.slice(2);
    if (args.some(arg => arg !== "--copy") || args.length > 1) throw new Error(`usage: ${basename(process.argv[1]!)} [--copy]`);
    const home = process.env.JCODE_HOME ? resolve(process.env.JCODE_HOME) : join(homedir(), ".jcode");
    for (const message of installAssets(repository, home, { copy: args.includes("--copy") })) console.log(message);
  } catch (error) {
    console.error(`ERROR: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  }
}
