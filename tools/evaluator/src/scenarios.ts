import { cpSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { validateScenarioOverrides, type SettingsOverrides } from "./settings";

export interface Scenario {
  id: string; fixture: string; prompt: string; accept: string[];
  judge: { criteria: string[] };
  simulatedUser: { maxTurns: number; instructions: string };
  settings: SettingsOverrides; source: string; sourceSha256: string;
  instrumentation?: { guardMaxBytes?: number };
}
export interface FileState { sha256: string; mode: number; binary: boolean }
export type FixtureBaseline = Record<string, FileState>;
function object(v: unknown): v is Record<string, unknown> { return v !== null && typeof v === "object" && !Array.isArray(v); }
function exact(v: unknown, keys: string[]): v is Record<string, unknown> { return object(v) && Object.keys(v).length === keys.length && keys.every(k => k in v); }
function stringList(v: unknown): v is string[] { return Array.isArray(v) && v.length > 0 && v.every(x => typeof x === "string" && !!x.trim()); }
function fileInfo(path: string) { try { return lstatSync(path); } catch { throw new Error(`missing path: ${path}`); } }
function hash(bytes: Uint8Array): string { return new Bun.CryptoHasher("sha256").update(bytes).digest("hex"); }
function snapshot(root: string): FixtureBaseline {
  const result: FixtureBaseline = {};
  function walk(dir: string) {
    for (const name of readdirSync(dir).sort()) {
      const path = join(dir, name), rel = relative(root, path).replaceAll("\\", "/"), stat = lstatSync(path);
      if (stat.isSymbolicLink()) throw new Error(`fixture contains a symlink: ${rel}`);
      if (stat.isDirectory()) walk(path);
      else if (stat.isFile()) {
        const bytes = readFileSync(path); let binary = bytes.includes(0);
        try { new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch { binary = true; }
        result[rel] = { sha256: hash(bytes), mode: stat.mode & 0o7777, binary };
      } else throw new Error(`fixture contains a non-regular file: ${rel}`);
    }
  }
  if (!fileInfo(root).isDirectory()) throw new Error("fixture must be a directory");
  walk(root); return result;
}
function loadFile(path: string, expectedId?: string): Scenario {
  const stat = fileInfo(path);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error("scenario config must be a regular non-symlink file");
  if (stat.size > 256 * 1024) throw new Error("scenario config exceeds 256 KiB");
  const raw = readFileSync(path); let value: unknown;
  try { value = Bun.TOML.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)); } catch { throw new Error(`scenario config must be valid UTF-8 TOML: ${path}`); }
  if (!object(value) || Object.keys(value).some(k => !["id", "fixture", "prompt", "accept", "judge", "simulated_user", "settings", "instrumentation"].includes(k))) throw new Error("unsupported scenario field");
  const id = value.id ?? expectedId;
  if (typeof id !== "string" || !/^[a-z0-9][a-z0-9-]{0,79}$/.test(id)) throw new Error("scenario ID must be a lowercase slug");
  if (expectedId && id !== expectedId) throw new Error("scenario id must match its scenario directory name");
  if (typeof value.prompt !== "string" || !value.prompt.trim()) throw new Error("scenario prompt must be nonempty text");
  if (!stringList(value.accept) || value.accept.some(arg => arg.includes("\0"))) throw new Error("scenario accept must be a nonempty argv string list");
  if (!exact(value.judge, ["criteria"]) || !stringList(value.judge.criteria)) throw new Error("scenario judge requires nonempty criteria");
  if (!exact(value.simulated_user, ["max_turns", "instructions"]) || typeof value.simulated_user.max_turns !== "number" || !Number.isSafeInteger(value.simulated_user.max_turns) || value.simulated_user.max_turns < 1 || typeof value.simulated_user.instructions !== "string" || !value.simulated_user.instructions.trim()) throw new Error("simulated_user requires positive max_turns and nonempty instructions");
  const fixtureValue = value.fixture ?? "fixtures";
  if (typeof fixtureValue !== "string" || !fixtureValue || isAbsolute(fixtureValue)) throw new Error("scenario fixture must be a relative directory path");
  const fixture = resolve(path, "..", fixtureValue), fixtureStat = fileInfo(fixture);
  if (!fixtureStat.isDirectory() || fixtureStat.isSymbolicLink()) throw new Error("scenario fixture must be an existing non-symlink directory");
  snapshot(fixture);
  let instrumentation: Scenario["instrumentation"];
  if (value.instrumentation !== undefined) {
    if (!object(value.instrumentation) || Object.keys(value.instrumentation).some(k => !["isolation", "guard_max_bytes"].includes(k)) || (value.instrumentation.isolation !== undefined && value.instrumentation.isolation !== "blind")) throw new Error("instrumentation allows blind isolation and guard_max_bytes only");
    const max = value.instrumentation.guard_max_bytes;
    if (max !== undefined && (typeof max !== "number" || !Number.isSafeInteger(max) || max < 1)) throw new Error("instrumentation guard_max_bytes must be a positive integer");
    instrumentation = max === undefined ? {} : { guardMaxBytes: max as number };
  }
  return { id, fixture, prompt: value.prompt.trim(), accept: value.accept, judge: { criteria: value.judge.criteria.map(x => x.trim()) }, simulatedUser: { maxTurns: value.simulated_user.max_turns as number, instructions: value.simulated_user.instructions.trim() }, settings: value.settings === undefined ? {} : validateScenarioOverrides(value.settings), source: resolve(path), sourceSha256: hash(raw), instrumentation };
}
export function loadScenarios(path: string, scenarioId?: string): Scenario[] {
  const stat = fileInfo(path);
  if (stat.isSymbolicLink()) throw new Error("scenario path must not be a symlink");
  let scenarios: Scenario[];
  if (stat.isFile()) scenarios = [loadFile(path)];
  else if (stat.isDirectory()) {
    const names = readdirSync(path).sort(); if (!names.length) throw new Error("scenario directory must contain at least one scenario directory");
    scenarios = names.map(name => { const child = join(path, name), info = fileInfo(child); if (!info.isDirectory() || info.isSymbolicLink()) throw new Error(`scenario directory entry must be a directory: ${name}`); return loadFile(join(child, "config.toml"), name); });
  } else throw new Error("scenario path must be a file or directory");
  if (scenarioId !== undefined) { scenarios = scenarios.filter(s => s.id === scenarioId); if (!scenarios.length) throw new Error(`unknown scenario ID: ${scenarioId}`); }
  return scenarios;
}
export function prepareFixture(scenario: Scenario, workspace: string): FixtureBaseline {
  const baseline = snapshot(scenario.fixture);
  if (existsSync(workspace) && (!fileInfo(workspace).isDirectory() || readdirSync(workspace).length)) throw new Error("scenario workspace must be empty");
  mkdirSync(workspace, { recursive: true });
  cpSync(scenario.fixture, workspace, { recursive: true, errorOnExist: true });
  if (JSON.stringify(snapshot(workspace)) !== JSON.stringify(baseline)) throw new Error("materialized fixture differs from its source");
  if (!existsSync(join(workspace, ".git"))) {
    const env = { PATH: process.env.PATH, HOME: workspace, XDG_CONFIG_HOME: workspace, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1", GIT_AUTHOR_DATE: "2000-01-01T00:00:00+0000", GIT_COMMITTER_DATE: "2000-01-01T00:00:00+0000" };
    for (const args of [["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "initial"]]) {
      const result = spawnSync("git", ["-c", "user.name=Workspace", "-c", "user.email=workspace@example.invalid", ...args], { cwd: workspace, env, encoding: "utf8" });
      if (result.status !== 0) throw new Error(`fixture git setup failed: ${result.stderr || result.error?.message}`);
    }
  }
  return baseline;
}
export function verifyFixture(scenario: Scenario, baseline: FixtureBaseline): void {
  if (JSON.stringify(snapshot(scenario.fixture)) !== JSON.stringify(baseline)) throw new Error("evaluation modified the original fixture");
}
