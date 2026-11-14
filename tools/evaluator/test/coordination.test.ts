import { expect, test } from "bun:test";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { collectEvidence } from "../src/evidence";
import { analyzeCoordination } from "../src/coordination";

const dispatchInput = { task_id: "repair", worker: "routine" };
const rootMessages = [
  { id: "d", role: "assistant", content: [{ type: "tool_use", id: "c", name: "dispatch_task", input: dispatchInput }] },
  { id: "dr", role: "user", content: [{ type: "tool_result", tool_use_id: "c", content: "ok" }] },
];
const base = () => ({
  principal: "root", owner_session_id: "worker", input: { ...dispatchInput, checks: [{ id: "test", command: "test" }] },
  task: { assignment: { task_id: "repair", session: "worker", attempt_cap: 2 }, policy: { retries: 2, assistance_sessions: 3, environment_retries: 1 }, retries: 0, assists: 0, blocker_retries: {}, operations: {}, pending: null, pending_resume: null, terminal: true },
  completed_checks: [], actors: [], inflight_checks: {},
});

async function fixture(record: unknown, workerMessages: unknown[] = [], roots: unknown[] = rootMessages) {
  const root = await mkdtemp(join(tmpdir(), "coord-")); const home = join(root, "home"), blueprints = join(root, "blueprints");
  await mkdir(join(home, "sessions"), { recursive: true }); await mkdir(join(home, "dispatch-operations")); await mkdir(blueprints);
  await writeFile(join(home, "sessions/root.json"), JSON.stringify({ id: "root", parent_id: null, worker_blueprint_policy: { name: "coordinator" }, messages: roots }));
  await writeFile(join(home, "sessions/worker.json"), JSON.stringify({ id: "worker", parent_id: "root", worker_blueprint_policy: { name: "routine", delegation: { output_guard: { enabled: true, max_bytes: 1024 } } }, messages: workerMessages }));
  await writeFile(join(home, "dispatch-operations/operation-x.json"), JSON.stringify(record));
  const client = { async listSessions() { return [{ session_id: "root" }, { session_id: "worker", parent_session_id: "root" }]; }, async getHistory() { return []; }, async listTools(id: string) { return id === "root" ? [{ name: "dispatch_task", parameters: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" } } } }] : [{ name: "check" }]; } };
  const evidence = await collectEvidence(client, home, blueprints);
  return { evidence, dispose: () => rm(root, { recursive: true, force: true }) };
}

test("grounded native operation does not invent violations", async () => {
  const f = await fixture(base());
  try { expect(analyzeCoordination(f.evidence)).toEqual([]); expect(f.evidence.gaps.some(g => g.includes("matching native"))).toBe(false); }
  finally { await f.dispose(); }
});

test("detects owner mismatch, budget and check violations and retains native citations", async () => {
  const record = base(); record.owner_session_id = "unknown"; record.task.retries = 3; record.task.assists = 4;
  record.completed_checks = Array.from({ length: 3 }, (_, i) => ({ invocation: { session: "worker", call_id: String(i), check_id: "unknown" }, success: false })) as never[];
  const f = await fixture(record, [{ id: "check", content: [{ type: "tool_use", id: "x", name: "check", input: { id: "unknown" } }] }]);
  try {
    const findings = analyzeCoordination(f.evidence); const text = findings.map(x => x.text).join("\n");
    expect(text).toContain("grounded owner"); expect(text).toContain("retries exceeds"); expect(text).toContain("assists exceeds"); expect(text).toContain("undeclared check"); expect(text).toContain("attempt cap");
    expect(findings.every(x => x.citations.length && x.citations.every(c => c in f.evidence.citations))).toBe(true);
  } finally { await f.dispose(); }
});

test("tracks assist, handback, pending work and guarded extraction", async () => {
  const record = base();
  record.task.operations = { a: { event: { Report: { session: "worker", report: { Blocked: {} } } }, decision: { Assist: { worker: "expert" } } }, b: { event: { Report: { session: "helper", report: "Resolved" } }, decision: { Handback: { session: "worker" } } }, c: { event: { Resume: { session: "worker" } }, decision: "Continue" } };
  record.actors = [{ session_id: "helper" }] as never[];
  const f = await fixture(record, [{ id: "guard", content: [{ type: "tool_result", tool_use_id: "x", content: "Tool output was withheld because it exceeds the worker output limit. Use `extract` for bounded extraction." }] }]);
  try { const findings = analyzeCoordination(f.evidence); expect(findings.map(x => x.text).join("\n")).toContain("extract is not an available tool"); expect(findings.some(x => x.text.includes("Handback"))).toBe(false); }
  finally { await f.dispose(); }
  const bad = base(); bad.task.operations = { a: { event: {}, decision: { Handback: { session: "worker" } } } }; bad.task.pending_resume = ["worker", "a"] as never;
  const b = await fixture(bad);
  try { const findings = analyzeCoordination(b.evidence); expect(findings.some(x => x.text.includes("Handback lacks"))).toBe(true); expect(findings.some(x => x.text.includes("unresolved pending"))).toBe(true); }
  finally { await b.dispose(); }
});
test("withheld output does not imply extract is unavailable when worker tools are unknown", async () => {
  const message = { id: "guard", content: [{ type: "tool_result", tool_use_id: "x", content: "Tool output was withheld because it exceeds the worker output limit. Use `extract` for bounded extraction." }] };
  const f = await fixture(base(), [message]);
  try {
    const worker = f.evidence.sessions.find(s => s.id === "worker")!;
    worker.tools = [];
    f.evidence.gaps.push("SDK tools unavailable for worker: not collected without attaching to another session");
    expect(analyzeCoordination(f.evidence).some(x => x.text.includes("extract is not an available tool"))).toBe(false);
  } finally { await f.dispose(); }
});


test("missing native dispatch is a gap, not successful delegation", async () => {
  const record = base(); record.principal = "other";
  const f = await fixture(record, [], [...rootMessages, { id: "bash", content: [{ type: "tool_use", id: "b", name: "bash", input: {} }] }]);
  try { const findings = analyzeCoordination(f.evidence); expect(findings.some(x => x.text.includes("directly invoked bash"))).toBe(true); expect(f.evidence.gaps.join("\n")).toContain("no matching native dispatch operation"); }
  finally { await f.dispose(); }
});

test("dispatch errors require evidence of rejection and retain unknown causes as gaps", async () => {
  const result = (content: unknown) => [rootMessages[0], { id: "dr", role: "user", content: [{ type: "tool_result", tool_use_id: "c", content, is_error: true }] }];
  for (const content of ["[Tool 'dispatch_task' interrupted by server reload after 46.4s]", "worker disconnected", 'worker "Routine" is not eligible for another reason', null]) {
    const record = base(); record.principal = "other";
    const f = await fixture(record, [], result(content));
    try {
      const findings = analyzeCoordination(f.evidence);
      expect(findings.some(x => x.text.includes("rejected"))).toBe(false);
      expect(f.evidence.gaps).toContain("dispatch_task error cause not classified for root/c");
      expect(f.evidence.gaps.some(g => g.includes("no matching native dispatch operation"))).toBe(true);
    } finally { await f.dispose(); }
  }
  const f = await fixture(base(), [], result("invalid dispatch attempt cap"));
  try {
    const findings = analyzeCoordination(f.evidence);
    expect(findings.some(x => x.category === "patch" && x.text.includes("rejected"))).toBe(false);
    expect(f.evidence.gaps).toContain("dispatch_task rejected by native input or policy validation for root/c: invalid dispatch attempt cap");
    expect(f.evidence.gaps.some(g => g.includes("error cause not classified"))).toBe(false);
  } finally { await f.dispose(); }
  for (const content of ["Error: worker is not eligible in coordinator routing", 'Error: worker "Routine" is not eligible in coordinator routing; eligible worker IDs (case-sensitive): ["routine", "advanced"]', 'Error: worker "wrong\\"name" is not eligible in coordinator routing; eligible worker IDs (case-sensitive): ["routine"]']) {
    const f = await fixture(base(), [], result(content));
    try {
      analyzeCoordination(f.evidence);
      expect(f.evidence.gaps).toContain(`dispatch_task rejected by native input or policy validation for root/c: ${content}`);
      expect(f.evidence.gaps.some(g => g.includes("error cause not classified"))).toBe(false);
    } finally { await f.dispose(); }
  }
});

test("invalid effective schema and missing result remain independently visible", async () => {
  const f = await fixture(base(), [], [{ id: "d", role: "assistant", content: [{ type: "tool_use", id: "c", name: "dispatch_task", input: { task_id: 42 } }] }]);
  try {
    expect(analyzeCoordination(f.evidence).find(x => x.text.includes("violates its effective schema"))?.category).toBe("blueprint");
    expect(f.evidence.gaps).toContain("dispatch_task result unavailable for root/c");
    expect(f.evidence.gaps).toContain("dispatch_task task identity unavailable for root");
  } finally { await f.dispose(); }
});
