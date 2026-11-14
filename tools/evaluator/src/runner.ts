import { copyFileSync, cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { JcodeClient, type RunOptions, type TurnResult } from "@1jehuang/jcode-sdk";
import { prepareBlindRuntime, runBlindCommand } from "./blind_runtime";
import { collectEvidence, type EvidenceClient } from "./evidence";
import { analyzeCoordination } from "./coordination";
import { runJudge } from "./judge";
import { startPluginBroker } from "./plugin";
import { prepareFixture, verifyFixture, type Scenario } from "./scenarios";
import { mergeSettings, type EvaluationSettings } from "./settings";
import { writeReport } from "./report";
import type { EvidenceBundle, Review } from "./types";

export interface RunArguments { binary: string; bridge: string; blueprints: string; plugins: string; output: string; buildProvenance?: string; }
interface ScenarioOutcome extends Record<string, unknown> { execution: { status: string; error: string | null }; acceptance: Record<string, unknown>; reviewError: string | null; }
const continuation = "Approved. Proceed with implementation and verification according to your plan. Make sound implementation decisions yourself without weakening acceptance.";

export function blueprintNames(directory: string): string[] {
  const info = lstatSync(directory);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("blueprints must be a real directory");
  const files = readdirSync(directory).filter(name => name.endsWith(".md")).sort();
  const names = files.map(name => name.slice(0, -3));
  if (!names.includes("coordinator") || names.length < 2 || names.some(name => !/^[a-z0-9][a-z0-9_-]*$/.test(name))) throw new Error("blueprints require coordinator and at least one valid worker ID");
  for (const name of files) { const file = lstatSync(join(directory, name)); if (!file.isFile() || file.isSymbolicLink()) throw new Error(`blueprint must be a regular file: ${name}`); }
  return names;
}

export function validateWorkerOverrides(settings: EvaluationSettings, names: string[]): void {
  for (const name of new Set([...Object.keys(settings.workerModels), ...Object.keys(settings.workerEffort)])) {
    if (name === "coordinator" || !names.includes(name)) throw new Error(`unknown worker override: ${name}`);
  }
}

function stageBlueprints(source: string, target: string, names: string[], scenario: Scenario): void {
  mkdirSync(target, { recursive: true, mode: 0o700 }); let guards = 0;
  for (const name of names) {
    let text = readFileSync(join(source, `${name}.md`), "utf8");
    if (scenario.instrumentation?.guardMaxBytes !== undefined) {
      text = text.replace(/output-guard:\s*\{\s*max-bytes:\s*\d+\s*\}/g, () => { guards++; return `output-guard: {max-bytes: ${scenario.instrumentation!.guardMaxBytes}}`; });
    }
    writeFileSync(join(target, `${name}.md`), text, { mode: 0o600, flag: "wx" });
  }
  if (scenario.instrumentation?.guardMaxBytes !== undefined && !guards) throw new Error("scenario guard override found no blueprint output guard");
}

function configuration(settings: EvaluationSettings, names: string[], blueprints: string, hook: string): string {
  const q = JSON.stringify;
  let text = `[features]\nmemory = false\n\n[agents]\nworker_blueprints_dir = ${q(blueprints)}\nswarm_timeout_minutes = ${settings.limits.swarmTimeoutMinutes}\n`;
  if (settings.effortFor("coordinator")) text += `swarm_root_effort = ${q(settings.effortFor("coordinator"))}\n`;
  if (settings.effortFor("swarm")) text += `swarm_effort = ${q(settings.effortFor("swarm"))}\n`;
  for (const name of names.filter(name => name !== "coordinator")) {
    text += `\n[agents.worker_blueprints.${q(name)}]\nmodel = ${q(`worker_acceptance:${settings.modelForWorker(name)}`)}\n`;
    if (settings.effortForWorker(name)) text += `effort = ${q(settings.effortForWorker(name))}\n`;
  }
  text += `\n[hooks]\npre_tool_transform = [${q(hook)}]\npre_tool_transform_timeout_ms = 2000\n\n[providers.worker_acceptance]\ntype = ${q(settings.provider === "openrouter" ? "open-router" : settings.provider)}\nbase_url = ${q(settings.url)}\ndefault_model = ${q(settings.modelFor("coordinator"))}\napi_key_env = "JCODE_ACCEPTANCE_API_KEY"\nauth = ${q(settings.auth)}\nrequires_api_key = true\nmodel_catalog = false\n`;
  if (settings.authHeader) text += `auth_header = ${q(settings.authHeader)}\n`;
  return text;
}

async function bounded<T>(work: Promise<T>, milliseconds: number, cancel: () => Promise<unknown>): Promise<T> {
  if (milliseconds <= 0) throw new Error("execution deadline exceeded");
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([work, new Promise<never>((_, reject) => { timer = setTimeout(() => { void cancel().catch(() => {}); reject(new Error(`turn timed out after ${milliseconds}ms`)); }, milliseconds); })]);
  } finally { clearTimeout(timer); }
}

function rejectSymlinks(root: string): void {
  for (const name of readdirSync(root)) {
    const path = join(root, name), info = lstatSync(path);
    if (info.isSymbolicLink()) throw new Error(`acceptance workspace contains a symlink: ${relative(root, path)}`);
    if (info.isDirectory()) rejectSymlinks(path);
    else if (!info.isFile()) throw new Error(`acceptance workspace contains a non-regular file: ${path}`);
  }
}

/** Run unchanged fixture verifier files against a separate snapshot of the produced code. */
export async function runAcceptance(scenario: Scenario, workspace: string, baseline: ReturnType<typeof prepareFixture>, timeoutMs: number): Promise<Record<string, unknown>> {
  const target = mkdtempSync("/var/tmp/jcode-accept-");
  try {
    cpSync(workspace, target, { recursive: true, dereference: false });
    rejectSymlinks(target);
    for (const arg of scenario.accept.slice(1)) {
      const path = resolve(target, arg), rel = relative(target, path).split(sep).join("/");
      if (rel && !rel.startsWith("../") && Object.hasOwn(baseline, rel)) copyFileSync(join(scenario.fixture, rel), path);
    }
    const result = await runBlindCommand({ workspace: target, argv: scenario.accept, timeoutMs, copyWorkspace: true });
    return { ...result, status: result.timedOut ? "timeout" : result.exitCode === 0 ? "pass" : "fail", command: scenario.accept };
  } finally { rmSync(target, { recursive: true, force: true }); }
}

export async function runCoordinatorLoop(client: Pick<JcodeClient, "run" | "cancel">, session: string, initialPrompt: string, maxTurns: number, turnTimeoutMs: number, executionTimeoutMs: number | null, accept: () => Promise<Record<string, unknown>>) {
  const conversation: Record<string, unknown>[] = [], events: unknown[] = [], checks: Record<string, unknown>[] = [];
  let prompt = initialPrompt, elapsed = 0, error: string | undefined, termination = "turn-cap";
  for (let i = 0; i < maxTurns; i++) {
    const budget = executionTimeoutMs === null ? turnTimeoutMs : Math.min(turnTimeoutMs, executionTimeoutMs - elapsed);
    if (budget <= 0) { error = "execution deadline exceeded before coordinator turn"; termination = "execution-deadline"; break; }
    conversation.push({ role: "user", content: prompt, origin: i ? "continuation" : "scenario" });
    const started = Date.now();
    try {
      const options: RunOptions = { autoApprove: true, onEvent: event => events.push(event) };
      const result: TurnResult = await bounded(client.run(session, prompt, options), budget, () => client.cancel(session));
      conversation.push({ role: "assistant", content: result.finalText, stopReason: result.stopReason });
      if (result.stopReason) throw new Error(`coordinator stopped: ${result.stopReason} ${result.stopMessage ?? ""}`);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); termination = "coordinator-error"; }
    finally { elapsed += Date.now() - started; }
    try { checks.push(await accept()); }
    catch (cause) { checks.push({ status: "error", error: cause instanceof Error ? cause.message : String(cause) }); error ??= "independent acceptance failed to run"; termination = "acceptance-error"; }
    if (error) break;
    if (checks.at(-1)?.status === "pass") { termination = "accepted"; break; }
    if (checks.at(-1)?.status === "error") { error = "independent acceptance failed to run"; termination = "acceptance-error"; break; }
    prompt = continuation;
  }
  if (!error && termination !== "accepted") error = "coordinator turn cap reached before acceptance";
  return { conversation, events, checks, acceptance: checks.at(-1) ?? { status: "not-run" }, error, termination };
}

// Never attach to workers for observation: an attach can restore a session and
// start provider activity. Only the already-attached coordinator may be read.
export async function collectLiveEvidence(client: EvidenceClient, coordinatorId: string | undefined, jcodeHome: string, blueprints: string): Promise<EvidenceBundle> {
  const check = (id: string) => {
    if (!coordinatorId || id !== coordinatorId) throw new Error("not collected without attaching to another session");
  };
  return collectEvidence({
    listSessions: options => client.listSessions(options),
    getHistory: async id => { check(id); return client.getHistory(id); },
    listTools: async id => { check(id); return client.listTools(id); },
  }, jcodeHome, blueprints, { attachedSessionId: coordinatorId });
}

/** Preserve exactly what the judge cited before journals or task records change. */
export function captureReviewCitations(review: Review, citations: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries([...new Set(review.findings.flatMap(finding => finding.citations))].map(ref => [ref, citations[ref]]));
}

async function collectCachedEvidence(live: EvidenceBundle, listSessions: EvidenceClient["listSessions"], jcodeHome: string, blueprints: string, excludeSessionIds?: Iterable<string>): Promise<EvidenceBundle> {
  const byId = new Map(live.sessions.map(session => [session.id, session]));
  const cached = (id: string, field: "history" | "tools") => {
    const prefix = `SDK ${field} unavailable for ${id}: `;
    const gap = live.gaps.find(message => message.startsWith(prefix));
    if (gap) throw gap.slice(prefix.length);
    const session = byId.get(id);
    if (!session) throw new Error("not collected while the coordinator was attached");
    return session[field];
  };
  const evidence = await collectEvidence({ listSessions, getHistory: async id => cached(id, "history"), listTools: async id => cached(id, "tools") }, jcodeHome, blueprints, { excludeSessionIds });
  // A later listing can fail or omit a session. Keep already observed SDK data
  // without claiming that the session appeared in the newer listing.
  for (const session of evidence.sessions) {
    const previous = byId.get(session.id);
    if (!previous || !evidence.gaps.includes(`native session absent from SDK listing: ${session.id}`)) continue;
    for (const field of ["history", "tools"] as const) {
      if (live.gaps.some(gap => gap.startsWith(`SDK ${field} unavailable for ${session.id}: `))) continue;
      session[field] = previous[field];
      const unavailable = `SDK ${field} unavailable for ${session.id}: session absent from SDK listing`;
      const index = evidence.gaps.indexOf(unavailable); if (index !== -1) evidence.gaps.splice(index, 1);
    }
  }
  evidence.gaps.push(...live.gaps.filter(gap => !evidence.gaps.includes(gap)));
  return evidence;
}

/** Refresh listings after the judge without switching its connection back. */
export async function collectPostJudgeEvidence(client: Pick<EvidenceClient, "listSessions">, live: EvidenceBundle, jcodeHome: string, blueprints: string, excludeSessionIds?: Iterable<string>): Promise<EvidenceBundle> {
  return collectCachedEvidence(live, options => client.listSessions(options), jcodeHome, blueprints, excludeSessionIds);
}

/** Refresh persisted evidence after shutdown without inventing SDK listings. */
export async function collectFinalEvidence(live: EvidenceBundle, jcodeHome: string, blueprints: string, excludeSessionIds?: Iterable<string>): Promise<EvidenceBundle> {
  // Only listings actually returned before shutdown may be replayed.
  const listings = live.sessions.flatMap(session => {
    const persisted = session.persisted;
    if (!persisted || typeof persisted !== "object" || Array.isArray(persisted) || !("listing" in persisted)) return [];
    const listing = persisted.listing;
    return listing && typeof listing === "object" && !Array.isArray(listing) ? [listing] : [];
  });
  return collectCachedEvidence(live, async () => listings, jcodeHome, blueprints, excludeSessionIds);
}

export async function runScenario(args: RunArguments, scenario: Scenario, shared: EvaluationSettings, names: string[]): Promise<ScenarioOutcome> {
  const settings = mergeSettings(shared, scenario.settings); validateWorkerOverrides(settings, names);
  const buildProvenance: unknown = args.buildProvenance ? JSON.parse(readFileSync(args.buildProvenance, "utf8")) : null;
  const staging = mkdtempSync("/var/tmp/jcode-eval-");
  const sdkHome = mkdtempSync("/tmp/jcode-sdk-");
  const workspace = join(staging, "workspace"), configDir = join(staging, "config"), blueprintDir = join(configDir, "blueprints"), bin = join(staging, "bin");
  const output = join(args.output, scenario.id);
  const originalEnv = { ...process.env }; const toolPath = process.env.PATH ?? "/usr/bin:/bin";
  let client: JcodeClient | undefined; let broker: Awaited<ReturnType<typeof startPluginBroker>> | undefined;
  let coordinatorId: string | undefined;
  let baseline: ReturnType<typeof prepareFixture> | undefined;
  let evidence: EvidenceBundle | undefined; let executionError: string | undefined; let reviewError: string | undefined;
  let reviewSkipped: string | null = "execution did not reach judge";
  let acceptance: Record<string, unknown> = { status: "not-run" }; let review: Review | undefined; let reviewCitations: Record<string, unknown> | null = null; let diff: unknown;
  let conversation: Record<string, unknown>[] = [], events: unknown[] = [], acceptanceChecks: Record<string, unknown>[] = [], initialTools: unknown = null, termination = "startup-error";
  try {
    const jcodeHome = join(staging, "jcode-home");
    mkdirSync(workspace, { mode: 0o700 }); mkdirSync(bin, { mode: 0o700 }); mkdirSync(jcodeHome, { mode: 0o700 });
    baseline = prepareFixture(scenario, workspace);
    stageBlueprints(args.blueprints, blueprintDir, names, scenario);
    const configFile = join(jcodeHome, "config.toml");
    writeFileSync(configFile, configuration(settings, names, blueprintDir, join(bin, "hook-proxy")), { mode: 0o600, flag: "wx" });
    const runtime = await prepareBlindRuntime({ stagingRoot: staging, sdkHome, binary: resolve(args.binary), bridge: resolve(args.bridge), readonlyPaths: [configDir, configFile], allowedDomains: [new URL(settings.url).hostname] });
    broker = await startPluginBroker(args.plugins, sdkHome, bin, workspace, toolPath);
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, { PATH: "/usr/bin:/bin", HOME: sdkHome, LANG: "C.UTF-8" });
    client = await JcodeClient.launch({ binary: runtime.binary, jcodeHome: sdkHome, inheritLogins: false, startupTimeoutMs: 30000, requestTimeoutMs: 30000, env: { JCODE_ACCEPTANCE_API_KEY: settings.apiKey, JCODE_SWARM_ENABLED: "true", JCODE_SWARM_MODEL: `worker_acceptance:${settings.modelFor("swarm")}`, JCODE_SWARM_SPAWN_MODE: "headless" } });
    try {
      const coordinator = await client.createSession({ workingDir: workspace });
      coordinatorId = coordinator.session_id;
      await client.setModel(coordinator.session_id, `worker_acceptance:${settings.modelFor("coordinator")}`);
      if (settings.effortFor("coordinator")) await client.setReasoningEffort(coordinator.session_id, settings.effortFor("coordinator")!);
      initialTools = await client.listTools(coordinator.session_id);
      const loop = await runCoordinatorLoop(client, coordinator.session_id, scenario.prompt, scenario.simulatedUser.maxTurns, settings.limits.turnTimeoutSeconds * 1000, settings.limits.executionTimeoutSeconds === null ? null : settings.limits.executionTimeoutSeconds * 1000, () => runAcceptance(scenario, workspace, baseline!, settings.limits.acceptanceTimeoutSeconds * 1000));
      ({ conversation, events, checks: acceptanceChecks, acceptance, error: executionError, termination } = loop);
    } catch (error) { executionError = error instanceof Error ? error.message : String(error); }
    verifyFixture(scenario, baseline);
    const changes = await runBlindCommand({ workspace, argv: ["git", "diff", "--no-ext-diff", "--no-color", "HEAD"], timeoutMs: 10000 });
    const status = await runBlindCommand({ workspace, argv: ["git", "status", "--porcelain", "--untracked-files=all"], timeoutMs: 10000 });
    diff = { diff: changes, status };
    evidence = await collectLiveEvidence(client, coordinatorId, jcodeHome, blueprintDir);
    const deterministic = analyzeCoordination(evidence);
    const excludedSessions = new Set<string>();
    if (!executionError) {
      reviewSkipped = null;
      let judgeClient: JcodeClient | undefined;
      try {
        const apiSocket = join(sdkHome, "run", "jcode-api.sock");
        const activeJudge = existsSync(apiSocket)
          ? (judgeClient = await JcodeClient.connect({ socketPath: apiSocket, requestTimeoutMs: settings.limits.turnTimeoutSeconds * 1000 }))
          : client;
        review = await runJudge(activeJudge, workspace, `worker_acceptance:${settings.modelFor("judge")}`, settings.effortFor("judge"), { scenario: { id: scenario.id, prompt: scenario.prompt, criteria: scenario.judge.criteria }, conversation, events, initialTools, acceptance, acceptanceChecks, diff, evidence, deterministic }, evidence.citations, settings.limits.turnTimeoutSeconds * 1000, id => { if (id !== coordinatorId) excludedSessions.add(id); });
        reviewCitations = captureReviewCitations(review, evidence.citations);
      } catch (error) { reviewError = error instanceof Error ? error.message : String(error); }
      finally { if (judgeClient) { try { await judgeClient.close(); } catch {} } }
      evidence = await collectPostJudgeEvidence(client, evidence, jcodeHome, blueprintDir, excludedSessions);
    } else reviewSkipped = "execution failed";
    const live = evidence;
    await client.close(); client = undefined;
    evidence = await collectFinalEvidence(live, jcodeHome, blueprintDir, excludedSessions);
  } catch (error) { executionError ??= error instanceof Error ? error.message : String(error); }
  finally {
    if (client) { try { await client.close(); } catch (error) { executionError ??= `SDK shutdown: ${String(error)}`; } }
    if (broker) { try { await broker.close(); } catch (error) { executionError ??= `hook shutdown: ${String(error)}`; } }
    for (const key of Object.keys(process.env)) delete process.env[key]; Object.assign(process.env, originalEnv);
    if (baseline) { try { verifyFixture(scenario, baseline); } catch (error) { executionError ??= String(error); } }
  }
  const result = { id: scenario.id, scenarioSource: { path: scenario.source, sha256: scenario.sourceSha256 }, execution: { status: executionError ? "error" : "completed", error: executionError ?? null, termination }, conversation, events, initialTools, acceptance, acceptanceChecks, diff, findings: evidence ? analyzeCoordination(evidence) : [], review: review ?? null, reviewCitations, reviewError: reviewError ?? null, reviewSkipped, evidence: evidence ?? null, hookTrace: broker?.traces ?? [], buildProvenance };
  try { await writeReport(output, result, [settings.apiKey]); }
  finally { rmSync(staging, { recursive: true, force: true }); rmSync(sdkHome, { recursive: true, force: true }); }
  return result;
}

export async function runScenarios(args: RunArguments, scenarios: Scenario[], settings: EvaluationSettings): Promise<number> {
  const names = blueprintNames(args.blueprints);
  for (const scenario of scenarios) validateWorkerOverrides(mergeSettings(settings, scenario.settings), names);
  mkdirSync(args.output, { recursive: true, mode: 0o700 });
  const results = [];
  for (const scenario of scenarios) {
    const result = await runScenario(args, scenario, settings, names);
    results.push(result);
    if (result.execution.status !== "completed" || result.acceptance.status !== "pass" || result.reviewError !== null) break;
  }
  await writeReport(args.output, { scenarios: results, skippedScenarios: scenarios.slice(results.length).map(scenario => scenario.id) }, [settings.apiKey]);
  return results.every(result => result.execution.status === "completed" && result.acceptance.status === "pass" && result.reviewError === null) ? 0 : 1;
}
