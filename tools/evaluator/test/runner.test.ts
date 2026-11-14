import { expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { loadSettings } from "../src/settings";
import { captureReviewCitations, collectFinalEvidence, collectLiveEvidence, collectPostJudgeEvidence, runCoordinatorLoop, runScenarios } from "../src/runner";
import type { Scenario } from "../src/scenarios";
import { writeReport } from "../src/report";

test("coordinator continues only after independent failure and stops when acceptance passes", async () => {
  const actions: string[] = [];
  const client = {
    run: async (_id: string, prompt: string) => { actions.push(`turn:${prompt}`); return { text: "done", finalText: "done", messages: [], reasoning: "", toolCalls: [] }; },
    cancel: async (_id: string) => {},
  };
  let calls = 0;
  const result = await runCoordinatorLoop(client, "coordinator", "initial task", 4, 1000, null, async () => { calls++; actions.push(`accept:${calls}`); return { status: calls === 1 ? "fail" : "pass" }; });
  expect(result.termination).toBe("accepted");
  expect(result.error).toBeUndefined();
  expect(actions).toEqual(["turn:initial task", "accept:1", "turn:Approved. Proceed with implementation and verification according to your plan. Make sound implementation decisions yourself without weakening acceptance.", "accept:2"]);
  expect(result.acceptance.status).toBe("pass");
});

test("a stopped coordinator is still independently checked but is not resumed", async () => {
  const actions: string[] = [];
  const client = {
    run: async (_id: string, _prompt: string) => { actions.push("turn"); throw new Error("runtime stopped"); },
    cancel: async (_id: string) => {},
  };
  const result = await runCoordinatorLoop(client, "coordinator", "task", 3, 1000, null, async () => { actions.push("accept"); return { status: "pass" }; });
  expect(actions).toEqual(["turn", "accept"]);
  expect(result.termination).toBe("coordinator-error");
  expect(result.error).toBe("runtime stopped");
});

test("a startup failure stops the batch before the next scenario or judge", async () => {
  const root = mkdtempSync("/var/tmp/jc-batch-test-");
  try {
    const blueprints = join(root, "blueprints"), fixture = join(root, "fixture"), output = join(root, "out");
    mkdirSync(blueprints); mkdirSync(fixture);
    writeFileSync(join(blueprints, "coordinator.md"), "coordinator");
    writeFileSync(join(blueprints, "worker.md"), "worker");
    writeFileSync(join(fixture, "fixture.txt"), "fixture");
    const config = join(root, "settings.toml");
    writeFileSync(config, '[provider]\ntype = "openrouter"\nurl = "http://127.0.0.1:1/v1"\nmodel = "test"\napi_key = "synthetic"\n', { mode: 0o600 });
    const first: Scenario = { id: "first", fixture, prompt: "test", accept: ["true"], judge: { criteria: ["test"] }, simulatedUser: { maxTurns: 1, instructions: "test" }, settings: {}, source: config, sourceSha256: "test" };
    const second = { ...first, id: "second" };
    const code = await runScenarios({ binary: join(root, "missing"), bridge: "/usr/bin/true", blueprints, plugins: root, output }, [first, second], loadSettings(config));
    const report = JSON.parse(readFileSync(join(output, "summary.json"), "utf8"));
    expect(code).toBe(1);
    expect(report.scenarios.map((scenario: Scenario) => scenario.id)).toEqual(["first"]);
    expect(report.scenarios[0].execution.status).toBe("error");
    expect(report.scenarios[0].reviewSkipped).toBe("execution did not reach judge");
    expect(report.skippedScenarios).toEqual(["second"]);
  } finally { rmSync(root, { recursive: true, force: true }); }
}, 15_000);

test("coordinator evidence survives the judge and final refresh without worker attachments or extra reads", async () => {
  const root = mkdtempSync("/tmp/jc-evidence-");
  try {
    const home = join(root, "home"), blueprints = join(root, "blueprints");
    mkdirSync(join(home, "sessions"), { recursive: true }); mkdirSync(join(home, "dispatch-operations")); mkdirSync(blueprints);
    const workerMessage = { id: "m", role: "assistant", content: [{ type: "tool_use", name: "check", id: "c", input: {} }] };
    writeFileSync(join(home, "sessions/root.json"), JSON.stringify({ id: "root", parent_id: null, messages: [] }));
    writeFileSync(join(home, "sessions/worker.json"), JSON.stringify({ id: "worker", parent_id: "root", messages: [workerMessage] }));
    writeFileSync(join(home, "sessions/unlisted.json"), JSON.stringify({ id: "unlisted", parent_id: "root", messages: [] }));
    const operation = { principal: "root", owner_session_id: "worker", input: { task_id: "t" }, task: { assignment: { task_id: "t", session: "worker" } } };
    writeFileSync(join(home, "dispatch-operations", `task-${"a".repeat(64)}.json`), JSON.stringify(operation));
    const actions: string[] = []; let phase = "coordinator";
    const owner = {
      async listSessions() { actions.push(`list:${phase}`); return [{ session_id: "root", status: "idle" }, { session_id: "worker", parent_session_id: "root", status: "idle" }, ...(phase === "judge" ? [{ session_id: "judge", status: "idle" }] : [])]; },
      async attachSession(id: string) { actions.push(`attach:${id}`); throw Error("must not attach"); },
      async getHistory(id: string) { actions.push(`history:${id}`); if (phase !== "coordinator" || id !== "root") throw Error("wrong session"); return [{ role: "assistant", content: "coordinator" }]; },
      async listTools(id: string) { actions.push(`tools:${id}`); if (phase !== "coordinator" || id !== "root") throw Error("wrong session"); return [{ name: "dispatch_task" }]; },
    };
    const live = await collectLiveEvidence(owner, "root", home, blueprints);
    phase = "judge";
    writeFileSync(join(home, "sessions/judge.json"), JSON.stringify({ id: "judge", parent_id: null, messages: [{ id: "jm", role: "assistant", content: [], token_usage: { input_tokens: 99, output_tokens: 11, cost_usd: 0.5 } }] }));
    writeFileSync(join(home, "sessions/judge.journal.jsonl"), JSON.stringify({ append_messages: [{ id: "jm", role: "assistant", content: [] }] }));
    const afterJudge = await collectPostJudgeEvidence(owner, live, home, blueprints, ["judge"]);
    const final = await collectFinalEvidence(afterJudge, home, blueprints, ["judge"]);
    expect(actions).toEqual(["list:coordinator", "history:root", "tools:root", "list:judge"]);
    expect(final.sessions.map(s => s.id)).not.toContain("judge");
    expect(final.costs.bySession.judge).toBeUndefined();
    expect(final.costs.inputTokens).toBe(0);
    expect(final.sessions.find(s => s.id === "root")?.history).toEqual([{ role: "assistant", content: "coordinator" }]);
    expect(final.sessions.find(s => s.id === "root")?.tools).toEqual([{ name: "dispatch_task" }]);
    expect(final.sessions.find(s => s.id === "worker")?.history).toEqual([]);
    expect(final.sessions.find(s => s.id === "worker")?.tools).toEqual([]);
    expect(final.citations["session:worker:message:m"]).toEqual(workerMessage);
    expect(final.citations["dispatch:t"]).toEqual(operation);
    expect(final.records).toEqual([{ ...operation, citation: "dispatch:t" }]);
    expect(final.gaps).toContain("SDK history unavailable for worker: Error: not collected without attaching to another session");
    expect(final.gaps).toContain("SDK tools unavailable for worker: Error: not collected without attaching to another session");
    expect(final.gaps).toContain("native session absent from SDK listing: unlisted");
    expect(final.gaps.some(gap => gap.includes("judge"))).toBe(false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("failed listing after judge retains previously observed coordinator data and gaps", async () => {
  const root = mkdtempSync("/tmp/jc-evidence-");
  try {
    const home = join(root, "home"), blueprints = join(root, "blueprints");
    mkdirSync(join(home, "sessions"), { recursive: true }); mkdirSync(join(home, "dispatch-operations")); mkdirSync(blueprints);
    for (const id of ["root", "worker"]) writeFileSync(join(home, "sessions", `${id}.json`), JSON.stringify({ id, messages: [] }));
    const calls: string[] = [];
    const owner = {
      async listSessions() { calls.push("list"); return [{ session_id: "root" }, { session_id: "worker" }]; },
      async getHistory(id: string) { calls.push(`history:${id}`); return ["actual history"]; },
      async listTools(id: string) { calls.push(`tools:${id}`); return [{ name: "dispatch_task" }]; },
    };
    const live = await collectLiveEvidence(owner, "root", home, blueprints);
    const afterJudge = await collectPostJudgeEvidence({ async listSessions() { calls.push("list-failed"); throw Error("offline"); } }, live, home, blueprints);
    const final = await collectFinalEvidence(afterJudge, home, blueprints);
    expect(calls).toEqual(["list", "history:root", "tools:root", "list-failed"]);
    expect(final.sessions.find(s => s.id === "root")?.history).toEqual(["actual history"]);
    expect(final.sessions.find(s => s.id === "root")?.tools).toEqual([{ name: "dispatch_task" }]);
    expect(final.gaps).toContain("SDK session listing unavailable: Error: offline");
    expect(final.gaps).toContain("SDK history unavailable for worker: Error: not collected without attaching to another session");
    expect(final.gaps).toContain("SDK tools unavailable for worker: Error: not collected without attaching to another session");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("saved judge citations retain the evidence reviewed when journals are replaced", async () => {
  const root = mkdtempSync("/tmp/jc-citations-");
  try {
    const home = join(root, "home"), blueprints = join(root, "blueprints"), output = join(root, "report");
    mkdirSync(join(home, "sessions"), { recursive: true }); mkdirSync(join(home, "dispatch-operations")); mkdirSync(blueprints);
    writeFileSync(join(home, "sessions/root.json"), JSON.stringify({ id: "root", messages: [] }));
    const journal = join(home, "sessions/root.journal.jsonl");
    const oldEntries = [{ append_messages: [{ id: "a", role: "assistant", content: "first" }] }, { append_messages: [{ id: "b", role: "assistant", content: "second" }] }];
    writeFileSync(journal, oldEntries.map(x => JSON.stringify(x)).join("\n") + "\n");
    const client = { async listSessions() { return [{ session_id: "root" }]; }, async getHistory() { return []; }, async listTools() { return []; } };
    const live = await collectLiveEvidence(client, "root", home, blueprints);
    const refs = ["session:root:journal:1", "session:root:journal:2"];
    const review = { findings: [{ severity: "medium" as const, category: "evaluator" as const, text: "finding", fix_target: "target", citations: refs }] };
    const reviewCitations = captureReviewCitations(review, live.citations);
    const replacement = { append_messages: [{ id: "c", role: "assistant", content: "replacement" }] };
    writeFileSync(journal, JSON.stringify(replacement) + "\n");
    const afterJudge = await collectPostJudgeEvidence(client, live, home, blueprints);
    const evidence = await collectFinalEvidence(afterJudge, home, blueprints);
    mkdirSync(output); await writeReport(output, { review, reviewCitations, evidence }, []);
    const saved = JSON.parse(readFileSync(join(output, "summary.json"), "utf8"));
    expect(saved.reviewCitations[refs[0]]).toEqual(oldEntries[0]);
    expect(saved.reviewCitations[refs[1]]).toEqual(oldEntries[1]);
    expect(saved.evidence.citations[refs[0]]).toEqual(replacement);
    expect(saved.evidence.citations[refs[1]]).toBeUndefined();
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("journal-only attached coordinator retains identity, tools, and costs when uncheckpointed", async () => {
  const root = mkdtempSync("/tmp/jc-journal-coord-");
  try {
    const home = join(root, "home"), blueprints = join(root, "blueprints");
    mkdirSync(join(home, "sessions"), { recursive: true }); mkdirSync(join(home, "dispatch-operations")); mkdirSync(blueprints);
    const journalLine = {
      meta: { parent_id: null, worker_blueprint_policy: null, model: "openai/gpt-6.1-sol" },
      append_messages: [{ id: "cm1", role: "assistant", content: [], token_usage: { input_tokens: 120, output_tokens: 30, cache_read_input_tokens: 80, cost_usd: 0.01 } }],
    };
    writeFileSync(join(home, "sessions/session_coord.journal.jsonl"), JSON.stringify(journalLine) + "\n");
    const owner = {
      async listSessions() { return []; },
      async getHistory(id: string) { if (id !== "session_coord") throw Error("wrong session"); return [{ role: "assistant", content: "plan" }]; },
      async listTools(id: string) { if (id !== "session_coord") throw Error("wrong session"); return [{ name: "dispatch_task", parameters: { type: "object" } }]; },
    };
    const live = await collectLiveEvidence(owner, "session_coord", home, blueprints);
    const afterJudge = await collectPostJudgeEvidence(owner, live, home, blueprints, ["session_judge"]);
    const final = await collectFinalEvidence(afterJudge, home, blueprints, ["session_judge"]);
    const coord = final.sessions.find(s => s.id === "session_coord");
    expect(coord?.history).toEqual([{ role: "assistant", content: "plan" }]);
    expect(coord?.tools).toEqual([{ name: "dispatch_task", parameters: { type: "object" } }]);
    expect(final.costs.bySession.session_coord).toBeCloseTo(0.01);
    expect(final.costs.inputTokens).toBe(120);
    expect(final.gaps).not.toContain("coordinator session identity unavailable");
  } finally { rmSync(root, { recursive: true, force: true }); }
});
