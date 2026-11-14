import { constants, closeSync, fstatSync, openSync, readFileSync, writeFileSync, lstatSync } from "node:fs";
import { basename } from "node:path";

export type Role = "coordinator" | "swarm" | "judge" | "simulator";
export type Effort = "low" | "medium" | "high" | "xhigh";
type Table = Record<string, unknown>;
export type SettingsOverrides = Record<string, Table>;
export interface EvaluationLimits {
  turnTimeoutSeconds: number;
  executionTimeoutSeconds: number | null;
  swarmTimeoutMinutes: number;
  acceptanceTimeoutSeconds: number;
}
export interface EvaluationSettings {
  provider: string; url: string; model: string; apiKey: string;
  auth: string; authHeader: string | null;
  models: Record<Role, string>; workerModels: Record<string, string>;
  limits: EvaluationLimits; effort: Partial<Record<Role, Effort>>; workerEffort: Record<string, Effort>;
  modelFor(role: Role): string; modelForWorker(id: string): string;
  effortFor(role: Role): Effort | undefined; effortForWorker(id: string): Effort | undefined;
}
const roles = ["coordinator", "swarm", "judge", "simulator"] as const;
const efforts = ["low", "medium", "high", "xhigh"];
const tableKeys: Record<string, string[] | null> = {
  provider: ["type", "url", "model", "api_key", "auth", "auth_header"], models: [...roles],
  worker_models: null, limits: ["turn_timeout_seconds", "execution_timeout_seconds", "swarm_timeout_minutes", "acceptance_timeout_seconds"], effort: [...roles], worker_effort: null,
};
const credentials: Record<string, true> = { api_key: true, apikey: true, password: true, passwd: true, token: true, secret: true, secrets: true, credential: true, credentials: true, private_key: true, client_secret: true };
const dataFor = new WeakMap<EvaluationSettings, SettingsOverrides>();
function table(value: unknown): value is Table { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function text(value: unknown): value is string { return typeof value === "string" && !!value.trim() && !/[\x00-\x1f\x7f]/.test(value); }
function workerKey(key: string) { if (!text(key) || key !== key.trim() || key === "coordinator") throw new Error("worker IDs must be nonempty, trimmed, and not coordinator"); }
function rejectCredentials(value: unknown): void {
  if (Array.isArray(value)) { for (const v of value) rejectCredentials(v); }
  else if (table(value)) for (const [key, v] of Object.entries(value)) {
    if (credentials[key.trim().toLowerCase()]) throw new Error("scenario settings must not contain credentials");
    rejectCredentials(v);
  }
}
function validUrl(value: string): boolean {
  try { const url = new URL(value); return (url.protocol === "https:" || (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) && !!url.hostname && !url.username && !url.password && !url.search && !url.hash; } catch { return false; }
}
function validatePartial(value: unknown, publicOverrides = false): SettingsOverrides {
  if (!table(value)) throw new Error("settings must contain tables");
  if (publicOverrides) rejectCredentials(value);
  for (const [name, entries] of Object.entries(value)) {
    if (!Object.hasOwn(tableKeys, name) || !table(entries)) throw new Error(`unsupported settings table: ${name}`);
    const keys = tableKeys[name];
    for (const [key, v] of Object.entries(entries)) {
      if (keys && !keys.includes(key)) throw new Error(`unsupported ${name} field: ${key}`);
      if (name === "provider") {
        if (publicOverrides && key === "api_key") throw new Error("scenario settings must not contain credentials");
        if (!text(v)) throw new Error(`provider ${key} must be nonempty text`);
        if (key === "type" && !["openai-compatible", "anthropic-compatible", "openrouter", "open-router"].includes(v)) throw new Error("unsupported provider type");
        if (key === "url" && !validUrl(v)) throw new Error("API URL must use HTTPS or HTTP loopback without credentials, query or fragment");
        if (key === "auth" && !["bearer", "header"].includes(v)) throw new Error("auth must be bearer or header");
        if (key === "auth_header" && !/^[A-Za-z0-9-]+$/.test(v)) throw new Error("invalid auth_header name");
      } else if (name === "limits") {
        if (typeof v !== "number" || !Number.isSafeInteger(v) || v < 1) throw new Error(`${key} must be a positive integer`);
      } else if (name === "effort" || name === "worker_effort") {
        if (name === "worker_effort") workerKey(key);
        if (typeof v !== "string" || !efforts.includes(v)) throw new Error("effort must be low, medium, high or xhigh");
      } else {
        if (name === "worker_models") workerKey(key);
        if (!text(v) || (name === "worker_models" && v !== v.trim())) throw new Error(`${name} ${key} must be nonempty model text`);
      }
    }
    if (name === "provider" && entries.auth_header !== undefined && entries.auth !== undefined && entries.auth !== "header") throw new Error("auth_header is only valid for header authentication");
  }
  return value as SettingsOverrides;
}
export function validateScenarioOverrides(value: unknown): SettingsOverrides { return validatePartial(value, true); }
function build(data: SettingsOverrides): EvaluationSettings {
  validatePartial(data);
  const p = data.provider;
  if (!p) throw new Error("settings require [provider]");
  for (const key of ["type", "url", "model", "api_key"]) if (!text(p[key])) throw new Error(`evaluation ${key} is required`);
  const concrete = (v: unknown, label: string): string => {
    if (!text(v) || v !== v.trim() || v.startsWith("REPLACE_")) throw new Error(`set a concrete ${label} without surrounding whitespace`);
    return v;
  };
  const model = concrete(p.model, "provider model"), apiKey = concrete(p.api_key, "API key");
  const auth = (p.auth ?? "bearer") as string;
  if (auth === "header" && !p.auth_header) throw new Error("header authentication requires auth_header");
  if (auth !== "header" && p.auth_header !== undefined) throw new Error("auth_header is only valid for header authentication");
  const models = Object.fromEntries(roles.map(role => [role, concrete(data.models?.[role] ?? model, `${role} model`)])) as Record<Role, string>;
  const workerModels = Object.fromEntries(Object.entries(data.worker_models ?? {}).map(([k, v]) => [k, concrete(v, `worker ${k} model`)]));
  const l = data.limits ?? {};
  const result: EvaluationSettings = {
    provider: p.type as string, url: p.url as string, model, apiKey, auth, authHeader: (p.auth_header as string | undefined) ?? null,
    models, workerModels, limits: { turnTimeoutSeconds: (l.turn_timeout_seconds as number) ?? 600, executionTimeoutSeconds: (l.execution_timeout_seconds as number) ?? null, swarmTimeoutMinutes: (l.swarm_timeout_minutes as number) ?? 10, acceptanceTimeoutSeconds: (l.acceptance_timeout_seconds as number) ?? 300 },
    effort: { ...data.effort } as Partial<Record<Role, Effort>>, workerEffort: { ...data.worker_effort } as Record<string, Effort>,
    modelFor(role) { if (!roles.includes(role)) throw new Error("unknown evaluation model role"); return this.models[role]; },
    modelForWorker(id) { return this.workerModels[id] ?? this.modelFor("swarm"); },
    effortFor(role) { if (!roles.includes(role)) throw new Error("unknown evaluation effort role"); return this.effort[role]; },
    effortForWorker(id) { return this.workerEffort[id] ?? this.effortFor("swarm"); },
  };
  dataFor.set(result, data);
  return result;
}
export function loadSettings(path: string): EvaluationSettings {
  if (process.platform === "win32") throw new Error("private credential file ACL checks are not supported on Windows");
  let fd: number;
  try { fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); } catch { throw new Error(`cannot securely open ${basename(path)}; create it with mode 600`); }
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.uid !== process.getuid?.() || (stat.mode & 0o077)) throw new Error("credential settings must be a regular file owned by you with mode 600");
    if (stat.size > 16384) throw new Error("credential settings exceed the supported size");
    let data: unknown;
    try { data = Bun.TOML.parse(new TextDecoder("utf-8", { fatal: true }).decode(readFileSync(fd))); } catch { throw new Error("invalid evaluation settings UTF-8 TOML; check syntax without sharing secrets"); }
    return build(validatePartial(data));
  } finally { closeSync(fd); }
}
export function mergeSettings(settings: EvaluationSettings, overrides: SettingsOverrides): EvaluationSettings {
  const base = dataFor.get(settings); if (!base) throw new Error("settings must come from loadSettings");
  validateScenarioOverrides(overrides);
  const merged: SettingsOverrides = {};
  for (const key of Object.keys(tableKeys)) if (base[key] || overrides[key]) merged[key] = { ...base[key], ...overrides[key] };
  return build(merged);
}
export function initializeSettings(path: string): string {
  const example = `${path}.example`;
  let info; try { info = lstatSync(example); } catch { throw new Error(`missing ${basename(example)} template`); }
  if (!info.isFile() || info.isSymbolicLink()) throw new Error("settings template must be a regular non-symlink file");
  const contents = readFileSync(example);
  try { writeFileSync(path, contents, { flag: "wx", mode: 0o600 }); } catch (e) { if ((e as NodeJS.ErrnoException).code === "EEXIST") throw new Error(`${basename(path)} already exists, refusing to overwrite`); throw e; }
  return path;
}
