import Ajv from "ajv";
import type { EvidenceBundle, Finding } from "./types";

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj | undefined => v !== null && typeof v === "object" && !Array.isArray(v) ? v as Obj : undefined;
const str = (v: unknown): string | undefined => typeof v === "string" && v.length ? v : undefined;
const list = (v: unknown): unknown[] => Array.isArray(v) ? v : [];

type Call = { session: string; citation: string; name: string; input: Obj | undefined; id: string | undefined; result?: Obj; resultCitation?: string };

// Exact messages emitted by the native dispatch input/policy checks. Other tool
// errors, including reload interruptions, do not establish a rejection cause.
const dispatchRejections: Record<string, true> = {
  "read-only dispatch requires allowed_paths=[]; list read inputs in inputs": true,
  "invalid dispatch acceptance or time budget": true,
  "declared checks require unique nonempty IDs and commands": true,
  "check timeout must be positive and within the task time budget": true,
  "assistance check IDs must be unique declared checks": true,
  "dispatch purpose is server-owned": true,
  "invalid dispatch mode": true,
  "invalid dispatch attempt cap": true,
  "dispatch write paths must be relative without traversal": true,
  "worker is not eligible in coordinator routing": true,
};

/** Analyze only structured native facts. Missing facts remain gaps, never inferred successes. */
export function analyzeCoordination(bundle: EvidenceBundle): Finding[] {
  const findings: Finding[] = [];
  function finding(text: string, category: Finding["category"], refs: string[], severity: Finding["severity"] = "high"): void {
    findings.push({ severity, category, text, fix_target: category === "blueprint" ? "worker blueprints" : category === "evaluator" ? "evidence collection" : "dispatcher", citations: [...new Set(refs.filter(ref => ref in bundle.citations))] });
  }
  function gap(message: string): void { if (!bundle.gaps.includes(message)) bundle.gaps.push(message); }
  const sessions = new Map(bundle.sessions.map(s => [s.id, s]));
  const calls: Call[] = [];
  for (const session of bundle.sessions) {
    const persisted = obj(session.persisted);
    const native = list(persisted?.messages);
    const results = new Map<string, { result: Obj; citation?: string }>();
    for (const message of native) for (const block of list(obj(message)?.content)) {
      const b = obj(block); const mid = str(obj(message)?.id);
      if (b?.type === "tool_result" && str(b.tool_use_id)) results.set(b.tool_use_id as string, { result: b, citation: mid ? `session:${session.id}:message:${mid}` : undefined });
    }
    for (const message of native) {
      const msg = obj(message); const mid = str(msg?.id); if (!msg || !mid) continue;
      const citation = `session:${session.id}:message:${mid}`;
      for (const block of list(msg.content)) {
        const b = obj(block); const name = str(b?.name);
        if (b?.type === "tool_use" && name) {
          const result = results.get(str(b.id) ?? "");
          calls.push({ session: session.id, citation, name, input: obj(b.input), id: str(b.id), result: result?.result, resultCitation: result?.citation });
        }
        if (b?.type === "tool_result") {
          const output = typeof b.content === "string" ? b.content : list(b.content).map(v => str(obj(v)?.text) ?? "").join("\n");
          const policy = obj(obj(persisted?.worker_blueprint_policy)?.delegation);
          const guard = obj(policy?.["output-guard"] ?? policy?.output_guard);
          const limit = guard?.max_bytes ?? guard?.["max-bytes"];
          if (/Tool output was withheld/i.test(output)) {
            if (!guard || typeof limit !== "number") gap(`output guard policy unavailable for session ${session.id}`);
            else {
              gap(`withheld payload size unavailable for session ${session.id}/${mid}`);
              if (/Use `extract`/.test(output) && !session.tools.some(v => obj(v)?.name === "extract") && !bundle.gaps.some(g => g.startsWith(`SDK tools unavailable for ${session.id}: `))) finding("Output guard directs the worker to extract, but extract is not an available tool.", "patch", [citation]);
              else if (!/Use `extract`|Report this blocker/i.test(output)) finding("Output guard withheld output without a permitted extraction or blocker instruction.", "patch", [citation]);
            }
          } else if (guard?.enabled !== false && typeof limit === "number" && Buffer.byteLength(output) > limit) {
            finding(`Worker tool result exceeds the configured output guard limit of ${limit} bytes without withholding.`, "patch", [citation]);
          }

        }
      }
    }
  }
  const coordinators = bundle.sessions.filter(s => {
    const p = obj(s.persisted); const policy = obj(p?.worker_blueprint_policy); const listing = obj(p?.listing);
    return policy?.name === "coordinator" || (!!p && !policy && p.parent_id == null && listing?.parent_session_id == null);
  });
  if (!coordinators.length) gap("coordinator session identity unavailable");
  for (const coordinator of coordinators) {
    if (!coordinator.tools.length) gap(`coordinator tools unavailable for ${coordinator.id}`);
    else if (!coordinator.tools.some(t => obj(t)?.name === "dispatch_task")) finding("Coordinator lacks dispatch_task in its effective tools.", "blueprint", [`session:${coordinator.id}`]);
    for (const call of calls.filter(c => c.session === coordinator.id && ["bash", "edit", "patch", "check"].includes(c.name))) finding(`Coordinator directly invoked ${call.name} instead of delegating.`, "blueprint", [call.citation]);
  }
  const ajv = new Ajv({ strict: false });
  for (const call of calls.filter(c => c.name === "dispatch_task")) {
    const session = sessions.get(call.session);
    const tool = session?.tools.map(obj).find(t => t?.name === "dispatch_task");
    const schema = obj(tool?.parameters ?? tool?.input_schema);
    if (schema) {
      try { if (!ajv.validate(schema, call.input)) finding(`dispatch_task input violates its effective schema: ${ajv.errorsText()}`, "blueprint", [call.citation], "medium"); }
      catch { gap(`dispatch_task schema could not be evaluated for ${call.session}`); }
    } else gap(`dispatch_task schema unavailable for ${call.session}`);
    if (call.result?.is_error === true) {
      const content = str(call.result.content); const reason = content?.replace(/^Error: /, "").trim();
      if (reason && (Object.hasOwn(dispatchRejections, reason) || /^worker "(?:[^"\\]|\\.)*" is not eligible in coordinator routing; eligible worker IDs \(case-sensitive\): \[.*\]$/.test(reason))) {
        gap(`dispatch_task rejected by native input or policy validation for ${call.session}/${call.id ?? "unknown"}: ${content}`);
      } else {
        gap(`dispatch_task error cause not classified for ${call.session}/${call.id ?? "unknown"}`);
      }
    } else if (!call.result) gap(`dispatch_task result unavailable for ${call.session}/${call.id ?? "unknown"}`);
    const taskId = str(call.input?.task_id);
    const records = bundle.records.filter(r => obj(r.input)?.task_id === taskId && r.principal === call.session);
    if (!taskId) gap(`dispatch_task task identity unavailable for ${call.session}`);
    else if (!records.length) gap(`dispatch_task ${taskId} has no matching native dispatch operation for ${call.session}`);
    else if (!records.some(r => str(r.owner_session_id) && sessions.has(r.owner_session_id as string) && obj(obj(r.task)?.assignment)?.session === r.owner_session_id)) finding(`dispatch_task ${taskId} has no grounded owner session matching its native assignment.`, "patch", [call.citation, ...records.map(r => String(r.citation))]);
  }
  for (const record of bundle.records) {
    const ref = str(record.citation); if (!ref) continue;
    const task = obj(record.task); const input = obj(record.input); const assignment = obj(task?.assignment); const policy = obj(task?.policy);
    if (!task || !input || !assignment || !policy) { gap(`incomplete dispatch operation ${ref}`); continue; }
    const owner = str(record.owner_session_id); const taskId = str(input.task_id);
    if (!owner || !sessions.has(owner)) gap(`dispatch owner session unavailable for ${ref}`);
    if (owner && assignment.session !== owner || taskId && assignment.task_id !== taskId) finding("Dispatch operation owner or task identity differs from its native assignment.", "patch", [ref]);
    for (const [field, ceiling, limit] of [["retries", 2, policy.retries], ["assists", 3, policy.assistance_sessions]] as const) {
      const n = task[field];
      if (typeof n !== "number" || typeof limit !== "number") gap(`dispatch ${field} budget unavailable for ${ref}`);
      else if (n > ceiling || limit > ceiling || n > limit) finding(`Dispatch ${field} exceeds its configured or maximum budget.`, "patch", [ref]);
    }
    const blockerRetries = obj(task.blocker_retries);
    if (blockerRetries) for (const [id, count] of Object.entries(blockerRetries)) if (typeof count === "number" && (count > 1 || (typeof policy.environment_retries === "number" && count > policy.environment_retries))) finding(`Environment blocker ${id} exceeded its retry budget.`, "patch", [ref]);
    const declared = new Set(list(input.checks).map(v => str(obj(v)?.id)).filter((v): v is string => !!v));
    const failed = new Map<string, number>();
    const assistanceChecks = new Set(list(input.assistance_checks).filter((v): v is string => typeof v === "string"));
    if (!Array.isArray(record.completed_checks)) gap(`completed check outcomes unavailable for ${ref}`);
    for (const value of list(record.completed_checks)) {
      const outcome = obj(value); const invocation = obj(outcome?.invocation); const checkId = str(invocation?.check_id); const sid = str(invocation?.session);
      if (!checkId || !sid || typeof outcome?.success !== "boolean") { gap(`invalid completed check outcome for ${ref}`); continue; }
      if (!declared.has(checkId)) finding(`Worker ran undeclared check ${checkId}.`, "patch", [ref]);
      if (!outcome.success) failed.set(sid, (failed.get(sid) ?? 0) + 1);
      if (sid !== owner && !assistanceChecks.has(checkId)) finding(`Assistance session ran unauthorized check ${checkId}.`, "patch", [ref]);
    }
    const cap = assignment.attempt_cap;
    if (typeof cap !== "number" || cap <= 0) gap(`attempt cap unavailable for ${ref}`);
    else for (const [sid, count] of failed) if (count > cap) finding(`Worker ${sid} exceeded the failed-check attempt cap (${count} > ${cap}).`, "patch", [ref]);
    const actors = new Set([owner, ...list(record.actors).map(a => str(obj(a)?.session_id))].filter((v): v is string => !!v));
    for (const call of calls.filter(c => c.name === "check" && actors.has(c.session))) {
      const checkId = str(call.input?.id ?? call.input?.check_id);
      if (!checkId) gap(`check call identity unavailable for ${call.session}`);
      else if (!declared.has(checkId)) finding(`Worker invoked undeclared check ${checkId}.`, "patch", [call.citation, ref]);
      else if (call.session !== owner && !assistanceChecks.has(checkId)) finding(`Assistance session invoked unauthorized check ${checkId}.`, "patch", [call.citation, ref]);
    }
    const operations = Object.values(obj(task.operations) ?? {}).map(obj).filter((v): v is Obj => !!v);
    const decisions = operations.map(o => obj(o.decision));
    const assists = decisions.filter(d => obj(d?.Assist));
    const handbacks = decisions.filter(d => obj(d?.Handback));
    for (const handback of handbacks) {
      const target = str(obj(handback?.Handback)?.session);
      const resolved = operations.some(o => {
        const event = obj(obj(o.event)?.Report); const report = obj(event?.report); const session = str(event?.session);
        return !!session && session !== target && actors.has(session) && (report?.Resolved !== undefined || event?.report === "Resolved");
      });
      if (!assists.length || !resolved || !target) finding("Handback lacks native Assist, resolved helper report, or owner identity.", "patch", [ref]);
      else if (!actors.has(target)) finding("Handback targets a session outside the native dispatch actors.", "patch", [ref]);
      else if (!operations.some(o => obj(obj(o.event)?.Resume)?.session === target) && !task.pending_resume && task.terminal === true) finding("Handback was not resumed before dispatch termination.", "patch", [ref]);
    }
    if (task.pending != null || task.pending_resume != null || Object.keys(obj(record.inflight_checks) ?? {}).length) finding("Dispatch has unresolved pending action or check requiring coordinator handling.", "patch", [ref], "medium");
  }
  return findings;
}
