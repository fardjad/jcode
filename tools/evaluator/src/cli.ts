#!/usr/bin/env bun
import { fileURLToPath } from "node:url";
import { join, resolve } from "node:path";
import { withIsolatedEnv } from "../../catalog/src/isolation";
import { ensurePinnedSdk } from "../../catalog/src/sdk";
import { loadSettings, initializeSettings, mergeSettings } from "./settings";
import { loadScenarios } from "./scenarios";
import { Redactor } from "./redaction";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const help = `Usage: evaluator <init-config|validate-settings|run> [options]

  init-config       --config PATH
  validate-settings --config PATH --scenarios PATH [--scenario ID] [--blueprints PATH]
  run               --config PATH --scenarios PATH --binary PATH --bridge PATH
                    --blueprints PATH --plugins PATH --output PATH
                    [--scenario ID] [--build-provenance PATH]`;

function options(args: string[], allowed: string[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (let i = 0; i < args.length; i++) {
    const key = args[i]!;
    if (!key.startsWith("--") || !allowed.includes(key.slice(2)) || Object.hasOwn(result, key.slice(2)) || !args[i + 1] || args[i + 1]!.startsWith("--")) throw new Error(`invalid option: ${key}`);
    result[key.slice(2)] = args[++i]!;
  }
  return result;
}
function required(values: Record<string, string>, key: string): string { const value = values[key]; if (!value) throw new Error(`missing --${key}`); return resolve(value); }

let redactor = new Redactor([]);
async function main(args: string[]): Promise<number> {
  const [command, ...rest] = args;
  if (!command || command === "--help" || command === "-h") { console.log(help); return 0; }
  if (command === "init-config") {
    const opts = options(rest, ["config"]);
    console.log(initializeSettings(required(opts, "config"))); return 0;
  }
  if (command !== "validate-settings" && command !== "run") throw new Error(`unknown command: ${command}`);
  const opts = options(rest, command === "run" ? ["config", "scenarios", "scenario", "binary", "bridge", "blueprints", "plugins", "output", "build-provenance"] : ["config", "scenarios", "scenario", "blueprints"]);
  const settings = loadSettings(required(opts, "config")); redactor = new Redactor([settings.apiKey]);
  const scenarios = loadScenarios(required(opts, "scenarios"), opts.scenario);
  const blueprints = opts.blueprints ? resolve(opts.blueprints) : join(root, "customizations", "workers", "blueprints");
  await ensurePinnedSdk(root);
  const { blueprintNames, validateWorkerOverrides, runScenarios } = await import("./runner");
  const names = blueprintNames(blueprints);
  for (const scenario of scenarios) validateWorkerOverrides(mergeSettings(settings, scenario.settings), names);
  if (command === "validate-settings") { console.log(`Settings valid for ${scenarios.length} scenario(s)`); return 0; }
  return runScenarios({ binary: required(opts, "binary"), bridge: required(opts, "bridge"), blueprints, plugins: required(opts, "plugins"), output: required(opts, "output"), buildProvenance: opts["build-provenance"] ? resolve(opts["build-provenance"]!) : undefined }, scenarios, settings);
}

const original = { ...process.env };
try {
  process.exitCode = await withIsolatedEnv(async env => {
    for (const key of Object.keys(process.env)) delete process.env[key]; Object.assign(process.env, env);
    try { return await main(process.argv.slice(2)); }
    finally { for (const key of Object.keys(process.env)) delete process.env[key]; Object.assign(process.env, original); }
  });
} catch (error) { console.error(`ERROR: ${redactor.text(error instanceof Error ? error.message : String(error))}`); process.exitCode = 1; }
