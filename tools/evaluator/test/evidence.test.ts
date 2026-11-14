import { describe, expect, test } from "bun:test";
import { mkdtemp, mkdir, readFile, writeFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { collectEvidence } from "../src/evidence";
import { analyzeCoordination } from "../src/coordination";
import { writeReport } from "../src/report";
import { collectLiveEvidence } from "../src/runner";

const client = {
  async listSessions() { return [{ session_id: "root" }]; },
  async getHistory() { return [{ role: "assistant", content: "summary" }]; },
  async listTools() { return [{ name: "dispatch_task", parameters: { type: "object" } }]; },
};

describe("native evidence collection", () => {
  test("replays journals, retains citations, dispatch operations and reported usage without double counting", async () => {
    const root = await mkdtemp(join(tmpdir(), "evidence-"));
    try {
      const home = join(root, "home"), blueprints = join(root, "blueprints");
      await mkdir(join(home, "sessions"), { recursive: true }); await mkdir(join(home, "dispatch-operations")); await mkdir(blueprints);
      await writeFile(join(blueprints, "coordinator.md"), "coordinator blueprint");
      const m1 = { id: "m1", role: "assistant", content: [], token_usage: { input_tokens: 10, output_tokens: 2, cache_read_input_tokens: 3, cost_usd: 0.2 } };
      const m2 = { id: "m2", role: "assistant", content: [], token_usage: { input_tokens: 4, output_tokens: 1, cost_usd: 0.1 } };
      await writeFile(join(home, "sessions/root.json"), JSON.stringify({ id: "root", parent_id: null, messages: [m1] }));
      await writeFile(join(home, "sessions/root.journal.jsonl"), [JSON.stringify({ append_messages: [m1] }), JSON.stringify({ append_messages: [m2] })].join("\n"));
      await writeFile(join(home, "dispatch-operations/operation-a.json"), JSON.stringify({ principal: "root", owner_session_id: "child", input: { task_id: "t" }, task: { assignment: { task_id: "t", session: "child" } } }));
      const bundle = await collectEvidence(client, home, blueprints);
      expect(bundle.sessions[0].history).toEqual([{ role: "assistant", content: "summary" }]);
      expect(bundle.citations["session:root:message:m2"]).toEqual(m2);
      expect(bundle.citations["dispatch:t"]).toBeDefined();
      expect(bundle.citations["blueprint:coordinator"]).toBe("coordinator blueprint");
      expect(bundle.costs.bySession.root).toBeCloseTo(0.3);
      expect(bundle.costs.inputTokens).toBe(14);
      expect(bundle.costs.cacheReadInputTokens).toBe(3);
      expect(bundle.gaps).toEqual([]);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  test("reports unknown costs, malformed and unsafe native evidence and unavailable SDK data", async () => {
    const root = await mkdtemp(join(tmpdir(), "evidence-"));
    try {
      const home = join(root, "home"), blueprints = join(root, "blueprints");
      await mkdir(join(home, "sessions"), { recursive: true }); await mkdir(join(home, "dispatch-operations")); await mkdir(blueprints);
      await writeFile(join(home, "sessions/root.json"), JSON.stringify({ id: "root", messages: [{ id: "m", token_usage: { input_tokens: 7 } }] }));
      await writeFile(join(home, "sessions/root.journal.jsonl"), "not-json\n");
      await symlink(join(home, "sessions/root.json"), join(home, "dispatch-operations/record-unsafe.json"));
      await symlink(join(home, "sessions/root.json"), join(home, "dispatch-operations/task-unsafe.json"));
      const bundle = await collectEvidence({ ...client, async getHistory() { throw Error("offline"); } }, home, blueprints);
      expect(bundle.costs.totalUsd).toBeNull(); expect(bundle.costs.bySession.root).toBeNull();
      expect(bundle.gaps.join("\n")).toContain("provider-reported cost unknown");
      expect(bundle.gaps.join("\n")).toContain("invalid root.journal.jsonl");
      expect(bundle.gaps.join("\n")).toContain("not a safe regular file");
      expect(bundle.gaps.some(gap => gap.includes("task-unsafe.json") && gap.includes("not a safe regular file"))).toBe(true);
      expect(bundle.gaps.join("\n")).toContain("SDK history unavailable");
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  test("reports native task and worker evidence without attaching the worker", async () => {
    const root = await mkdtemp(join(tmpdir(), "evidence-"));
    try {
      const home = join(root, "home"), blueprints = join(root, "blueprints"), output = join(root, "report");
      await mkdir(join(home, "sessions"), { recursive: true }); await mkdir(join(home, "dispatch-operations")); await mkdir(blueprints);
      const input = { task_id: "discover-inventory", worker: "routine" };
      const messages = [
        { id: "call", role: "assistant", content: [{ type: "tool_use", id: "c", name: "dispatch_task", input }] },
        { id: "result", role: "user", content: [{ type: "tool_result", tool_use_id: "c", content: "ok" }] },
      ];
      await writeFile(join(home, "sessions/root.json"), JSON.stringify({ id: "root", parent_id: null, worker_blueprint_policy: { name: "coordinator" }, messages }));
      const workerMessage = { id: "worker-message", role: "assistant", content: [{ type: "tool_use", id: "w", name: "check", input: { id: "verify" } }] };
      await writeFile(join(home, "sessions/worker.json"), JSON.stringify({ id: "worker", parent_id: "root", worker_blueprint_policy: { name: "routine" }, messages: [workerMessage] }));
      const record = {
        principal: "root", owner_session_id: "worker", input,
        task: { assignment: { task_id: input.task_id, session: "worker", attempt_cap: 2 }, policy: { retries: 2, assistance_sessions: 3, environment_retries: 1 }, retries: 0, assists: 0, blocker_retries: {}, operations: {}, pending: null, pending_resume: null, terminal: true },
        completed_checks: [], actors: [], inflight_checks: {},
      };
      // RuntimeStore writes task-<sha256>.json; alias files are not operations.
      await writeFile(join(home, "dispatch-operations", `task-${"a".repeat(64)}.json`), JSON.stringify(record));
      await writeFile(join(home, "dispatch-operations", `alias-${"b".repeat(64)}.json`), JSON.stringify(input.task_id));
      const actions: string[] = [];
      const sdk = {
        async listSessions() { actions.push("list"); return [{ session_id: "root" }, { session_id: "worker", parent_session_id: "root" }]; },
        async attachSession(id: string) { actions.push(`attach:${id}`); throw Error("evidence must not attach"); },
        async getHistory(id: string) { actions.push(`history:${id}`); return [{ role: "assistant", content: `history for ${id}` }]; },
        async listTools(id: string) { actions.push(`tools:${id}`); return [{ name: "dispatch_task", parameters: { type: "object" } }]; },
      };
      const evidence = await collectLiveEvidence(sdk, "root", home, blueprints);
      const findings = analyzeCoordination(evidence);
      await writeReport(output, { evidence, findings }, []);
      const report = JSON.parse(await readFile(join(output, "summary.json"), "utf8"));
      expect(actions).toEqual(["list", "history:root", "tools:root"]);
      expect(report.evidence.sessions.find((session: { id: string }) => session.id === "worker").history).toEqual([]);
      expect(report.evidence.sessions.find((session: { id: string }) => session.id === "worker").tools).toEqual([]);
      expect(report.evidence.citations["session:worker:message:worker-message"]).toEqual(workerMessage);
      expect(report.evidence.records).toEqual([{ ...record, citation: "dispatch:discover-inventory" }]);
      expect(report.evidence.citations["dispatch:discover-inventory"]).toEqual(record);
      expect(report.evidence.gaps.some((gap: string) => gap.includes("matching native dispatch operation"))).toBe(false);
      expect(report.evidence.gaps).toContain("SDK history unavailable for worker: Error: not collected without attaching to another session");
      expect(report.evidence.gaps).toContain("SDK tools unavailable for worker: Error: not collected without attaching to another session");
      expect(report.evidence.gaps.some((gap: string) => gap.includes("provider-reported cost unknown"))).toBe(true);
    } finally { await rm(root, { recursive: true, force: true }); }
  });

  test("failed coordinator reads and unavailable worker data stay explicit", async () => {
    const root = await mkdtemp(join(tmpdir(), "evidence-"));
    try {
      const home = join(root, "home"), blueprints = join(root, "blueprints");
      await mkdir(join(home, "sessions"), { recursive: true }); await mkdir(join(home, "dispatch-operations")); await mkdir(blueprints);
      for (const id of ["root", "worker"]) await writeFile(join(home, "sessions", `${id}.json`), JSON.stringify({ id, messages: [] }));
      const calls: string[] = [];
      const client = {
        async listSessions() { return [{ session_id: "root" }, { session_id: "worker" }]; },
        async attachSession(id: string) { calls.push(`attach:${id}`); throw Error("must not attach"); },
        async getHistory(id: string) { calls.push(`history:${id}`); throw Error("offline"); },
        async listTools(id: string) { calls.push(`tools:${id}`); throw Error("offline"); },
      };
      const evidence = await collectLiveEvidence(client, "root", home, blueprints);
      expect(calls).toEqual(["history:root", "tools:root"]);
      expect(evidence.gaps).toContain("SDK history unavailable for root: Error: offline");
      expect(evidence.gaps).toContain("SDK tools unavailable for root: Error: offline");
      expect(evidence.gaps).toContain("SDK history unavailable for worker: Error: not collected without attaching to another session");
      expect(evidence.gaps).toContain("SDK tools unavailable for worker: Error: not collected without attaching to another session");
    } finally { await rm(root, { recursive: true, force: true }); }
  });
});
