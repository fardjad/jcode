#!/usr/bin/env bun
import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { withIsolatedEnv } from "./isolation";
import { atomicWrite } from "./primitives";
import { ensurePinnedSdk } from "./sdk";
import { checkCatalog, checkIsolation, createPatched, snapshotPatches, listPatches } from "./patches";
import { validateStack, learnUpstream, testAll, testPatch } from "./validate";
import { prepareWorkerEvalBuild, runWorkerEvaluation } from "./eval";

const root = fileURLToPath(new URL("../../..", import.meta.url));
const help = `Develop and validate the jcode patch catalog.

Usage: bun tools/catalog/src/cli.ts COMMAND

  patches check | isolation | create | validate | snapshot [--prune] | list
  patches test [PATCH] [--all] [--only PATCH]... [--force] [--compat-each]
  patches learn
  workers build | evaluate [SCENARIO]
  version set RELEASE [EXPECTED_COMMIT]
  install | test | push | sdk-sync
`;

async function run(args: string[], cwd = root, env = process.env): Promise<void> {
  const child = Bun.spawn(args, { cwd, env, stdio: ["inherit", "inherit", "inherit"] });
  const status = await child.exited;
  if (status !== 0) throw new Error(`${args[0]} exited with status ${status}`);
}

function git(...args: string[]): string {
  const child = Bun.spawnSync(["git", ...args], { cwd: root, stdin: "ignore", stdout: "pipe", stderr: "pipe" });
  if (child.exitCode !== 0) throw new Error(`git ${args[0]}: ${child.stderr.toString().trim()}`);
  return child.stdout.toString().trim();
}

function expectNoArgs(args: string[]): void {
  if (args.length) throw new Error(`unexpected arguments: ${args.join(" ")}`);
}

function patchTestArgs(args: string[]) {
  const options: { only: string[]; force: boolean; compatEach: boolean; all: boolean; patch?: string } = {
    only: [], force: false, compatEach: false, all: false,
  };
  for (let i = 0; i < args.length; i++) {
    const value = args[i]!;
    if (value === "--all") options.all = true;
    else if (value === "--force") options.force = true;
    else if (value === "--compat-each") options.compatEach = true;
    else if (value === "--only") {
      const name = args[++i];
      if (!name || name.startsWith("-")) throw new Error("--only requires a patch name");
      options.only.push(name);
    } else if (value.startsWith("-") || options.patch) throw new Error(`unexpected argument: ${value}`);
    else options.patch = value;
  }
  if (options.patch && (options.all || options.only.length)) throw new Error("use PATCH or --all/--only, not both");
  if (!options.patch && !options.all && !options.only.length) throw new Error("provide PATCH or --all/--only");
  return options;
}

async function setVersion(args: string[]): Promise<void> {
  const [release, expected, ...extra] = args;
  if (!release || !/^v\d+\.\d+\.\d+$/.test(release) || extra.length) throw new Error("version set requires a stable vX.Y.Z release and optional commit hash");
  if (expected && !/^[0-9a-f]{7,40}$/.test(expected)) throw new Error("expected commit must be a lowercase hexadecimal hash prefix");
  if (git("branch", "--show-current") !== "personalized") throw new Error("set-version must run on personalized");
  if (git("status", "--porcelain")) throw new Error("set-version requires clean personalized checkout");
  const ref = `refs/jcode/upstream-tags/${release}`;
  await run(["git", "fetch", "--no-tags", "https://github.com/1jehuang/jcode.git", `+refs/tags/${release}:${ref}`]);
  const commit = git("rev-parse", "--verify", `${ref}^{commit}`);
  if (expected && !commit.startsWith(expected)) throw new Error(`release commit ${commit} does not match reviewed hash ${expected}`);
  await atomicWrite(join(root, "VERSION.txt"), `${release}\n`, 0o644);
  await ensurePinnedSdk(root);
  console.log(`VERSION.txt now pins ${release}`);
}

async function testCatalog(): Promise<void> {
  await ensurePinnedSdk(root);
  await run([process.execPath, "test", "tools/catalog/test", "tools/evaluator/test", "tools/scripts/setup_assets.test.ts"]);
  await run(["python3", "-m", "unittest", "customizations/plugins/rtk/test_rtk_transform.py"]);
  const dir = join(root, "tools/catalog/src/jcode_catalog");
  for (const name of (await readdir(dir)).filter((name) => name.endsWith("_test.sh")).sort()) {
    if (name === "install_patched_version_recipe_test.sh") continue;
    await run(["bash", join(dir, name)]);
  }
}

async function dispatch(args: string[], originalEnv: NodeJS.ProcessEnv): Promise<void> {
  const [group, command, ...rest] = args;
  if (!group || group === "--help" || group === "-h") { console.log(help); return; }
  if (group === "sdk-sync") { expectNoArgs(args.slice(1)); await ensurePinnedSdk(root); return; }
  if (group === "version" && command === "set") { await setVersion(rest); return; }
  if (group === "push") {
    expectNoArgs(args.slice(1));
    if (git("branch", "--show-current") !== "personalized") throw new Error("push must run on personalized");
    await run(["git", "push", "origin", "refs/heads/personalized:refs/heads/personalized"]);
    return;
  }
  if (group === "test") { expectNoArgs(args.slice(1)); await testCatalog(); return; }
  if (group === "install") {
    expectNoArgs(args.slice(1));
    await createPatched(root);
    const home = originalEnv.HOME;
    if (!home && !originalEnv.JCODE_INSTALL_DIR) throw new Error("HOME or JCODE_INSTALL_DIR is required for install");
    const env: NodeJS.ProcessEnv = { ...process.env, HOME: home, JCODE_INSTALL_DIR: originalEnv.JCODE_INSTALL_DIR ?? join(home!, ".local/bin") };
    if (process.platform === "darwin") env.CARGO_PROFILE_RELEASE_STRIP = "none";
    await run(["bash", "scripts/install_release.sh", "--fast"], join(root, ".patched-jcode"), env);
    return;
  }
  if (group === "workers") {
    if (command === "build") { expectNoArgs(rest); console.log(JSON.stringify(await prepareWorkerEvalBuild(root))); return; }
    if (command === "evaluate") { if (rest.length > 1) throw new Error("expected at most one scenario"); await runWorkerEvaluation(root, rest[0]); return; }
  }
  if (group === "patches") {
    if (command === "test") {
      const options = patchTestArgs(rest);
      if (options.patch) await testPatch(root, options.patch, { compatEach: options.compatEach });
      else await testAll(root, { only: options.only.length ? options.only : undefined, force: options.force, compatEach: options.compatEach });
      return;
    }
    if (command === "validate" || command === "learn") {
      expectNoArgs(rest);
      await (command === "validate" ? validateStack(root) : learnUpstream(root));
      return;
    }
    if (command === "snapshot") {
      if (rest.some((arg) => arg !== "--prune") || rest.length > 1) throw new Error("snapshot accepts only --prune");
      await snapshotPatches(root, rest.length === 1);
      return;
    }
    expectNoArgs(rest);
    if (command === "check") await checkCatalog(root);
    else if (command === "isolation") await checkIsolation(root);
    else if (command === "create") await createPatched(root);
    else if (command === "list") await listPatches(root);
    else throw new Error(`unknown patches command: ${command ?? ""}`);
    return;
  }
  throw new Error(`unknown command: ${group}`);
}

const originalEnv = { ...process.env };
try {
  await withIsolatedEnv(async (env) => {
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, env);
    try { await dispatch(process.argv.slice(2), originalEnv); }
    finally {
      for (const key of Object.keys(process.env)) delete process.env[key];
      Object.assign(process.env, originalEnv);
    }
  });
} catch (error) {
  console.error(`ERROR: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}
