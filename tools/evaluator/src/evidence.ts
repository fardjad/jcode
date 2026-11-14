import { readdir, lstat, readFile } from "node:fs/promises";
import { join } from "node:path";
import type { EvidenceBundle, SessionEvidence } from "./types";

type ObjectValue = Record<string, unknown>;
const object = (v: unknown): ObjectValue | undefined => v !== null && typeof v === "object" && !Array.isArray(v) ? v as ObjectValue : undefined;
const text = (v: unknown): string | undefined => typeof v === "string" && v.length > 0 ? v : undefined;

/** The narrow SDK surface also permits offline transports without inventing native evidence. */
export interface EvidenceClient {
  listSessions(options: { includeArchived: boolean }): Promise<unknown[]>;
  getHistory(sessionId: string): Promise<unknown[]>;
  listTools(sessionId: string): Promise<unknown[]>;
}

export async function collectEvidence(client: EvidenceClient, jcodeHome: string, blueprintsDir: string, options: { excludeSessionIds?: Iterable<string>; attachedSessionId?: string } = {}): Promise<EvidenceBundle> {
  const bundle: EvidenceBundle = { sessions: [], records: [], citations: {}, gaps: [], costs: { totalUsd: null, bySession: {}, inputTokens: 0, outputTokens: 0, cacheReadInputTokens: 0 } };
  const { gaps, citations } = bundle;
  async function files(dir: string): Promise<string[]> {
    try {
      const stat = await lstat(dir);
      if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("not a safe directory");
      return (await readdir(dir)).sort();
    } catch (e) { gaps.push(`unavailable evidence directory ${dir}: ${String(e)}`); return []; }
  }
  async function file(path: string): Promise<string | undefined> {
    try {
      const stat = await lstat(path);
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error("not a safe regular file");
      return await readFile(path, "utf8");
    } catch (e) { gaps.push(`unreadable evidence ${path}: ${String(e)}`); return undefined; }
  }
  function parse(raw: string, label: string): ObjectValue | undefined {
    try { const value = object(JSON.parse(raw)); if (!value) throw new Error("expected object"); return value; }
    catch (e) { gaps.push(`invalid ${label}: ${String(e)}`); return undefined; }
  }
  const native = new Map<string, ObjectValue>();
  const excluded = new Set(options.excludeSessionIds ?? []);
  const dir = join(jcodeHome, "sessions");
  const names = await files(dir);
  for (const name of names.filter(n => n.endsWith(".json") && !n.endsWith(".journal.jsonl"))) {
    const raw = await file(join(dir, name)); if (raw === undefined) continue;
    const snapshot = parse(raw, name); if (!snapshot) continue;
    const id = text(snapshot.session_id) ?? text(snapshot.id) ?? name.slice(0, -5);
    if (excluded.has(id)) continue;
    if (native.has(id)) { gaps.push(`duplicate native session ${id}`); continue; }
    native.set(id, snapshot);
  }
  // Retain usage from all journal versions even if transcript replacement removed a message.
  const usage = new Map<string, Map<string, ObjectValue>>();
  const messages = new Map<string, Map<string, ObjectValue>>();
  function remember(id: string, values: unknown, replace = false): void {
    if (!Array.isArray(values)) { gaps.push(`invalid native messages for ${id}`); return; }
    let active = messages.get(id); if (!active || replace) { active = new Map(); messages.set(id, active); }
    let all = usage.get(id); if (!all) { all = new Map(); usage.set(id, all); }
    for (const value of values) {
      const msg = object(value); const mid = text(msg?.id);
      if (!msg || !mid) { gaps.push(`native message without id in ${id}`); continue; }
      active.set(mid, msg);
      const old = all.get(mid);
      if (!old || (!object(old.token_usage) && object(msg.token_usage))) all.set(mid, msg);
    }
  }
  for (const [id, snapshot] of native) {
    if (snapshot.messages === undefined) gaps.push(`native session snapshot has no messages: ${id}`);
    else remember(id, snapshot.messages);
  }
  for (const name of names.filter(n => n.endsWith(".journal.jsonl"))) {
    const filenameId = name.slice(0, -".journal.jsonl".length);
    if (excluded.has(filenameId)) continue;
    const snapshot = [...native.entries()].find(([id]) => id === filenameId)?.[1];
    const id = snapshot ? filenameId : filenameId;
    if (!snapshot) gaps.push(`journal without native snapshot: ${id}`);
    const raw = await file(join(dir, name)); if (raw === undefined) continue;
    let seenNew = false;
    let journalMeta: ObjectValue | undefined;
    for (const [index, line] of raw.split(/\r?\n/).entries()) {
      if (!line.trim()) continue;
      const entry = parse(line, `${name}:${index + 1}`); if (!entry) continue;
      citations[`session:${id}:journal:${index + 1}`] = entry;
      // A journal can contain the snapshot's already-applied prefix.
      const appended = entry.append_messages;
      const ids = Array.isArray(appended) ? appended.map(v => text(object(v)?.id)) : [];
      const current = messages.get(id);
      const meta = object(entry.meta);
      const stale = !seenNew && entry.replace_messages === undefined && (ids.length > 0 ? ids.every(mid => mid && current?.has(mid)) : typeof meta?.updated_at === "number" && typeof snapshot?.updated_at === "number" && meta.updated_at <= snapshot.updated_at);
      // Capture usage even on stale entries, without altering active transcript.
      if (stale) {
        if (Array.isArray(appended)) for (const v of appended) { const m = object(v); const mid = text(m?.id); if (m && mid && object(m.token_usage)) { let all = usage.get(id); if (!all) usage.set(id, all = new Map()); if (!object(all.get(mid)?.token_usage)) all.set(mid, m); } }
        continue;
      }
      seenNew = true;
      if (entry.replace_messages !== undefined) remember(id, entry.replace_messages, true);
      if (entry.append_messages !== undefined) remember(id, entry.append_messages);
      if (meta) {
        journalMeta ??= { id };
        for (const key of ["parent_id", "worker_blueprint_policy", "title", "model", "provider_key", "reasoning_effort", "updated_at"]) {
          if (meta[key] !== undefined) {
            journalMeta[key] = meta[key];
            if (snapshot && (key === "parent_id" || key === "worker_blueprint_policy")) snapshot[key] = meta[key];
          }
        }
      }
    }
    if (!snapshot && journalMeta) native.set(id, journalMeta);
  }
  let listed: unknown[] = [];
  try { listed = await client.listSessions({ includeArchived: true }); if (!Array.isArray(listed)) throw new Error("not an array"); }
  catch (e) { gaps.push(`SDK session listing unavailable: ${String(e)}`); listed = []; }
  const listings = new Map<string, ObjectValue>();
  for (const value of listed) { const item = object(value); const id = text(item?.session_id); if (!item || !id) { gaps.push("SDK session listing without session_id"); continue; } if (excluded.has(id)) continue; if (listings.has(id)) gaps.push(`duplicate SDK session ${id}`); listings.set(id, item); }
  const attachedId = options.attachedSessionId && !excluded.has(options.attachedSessionId) ? options.attachedSessionId : undefined;
  const sessionIds = new Set([...listings.keys(), ...native.keys(), ...messages.keys(), ...(attachedId ? [attachedId] : [])]);
  for (const id of sessionIds) {
    const snapshot = native.get(id); const listing = listings.get(id);
    if (!snapshot) gaps.push(`listed session has no native snapshot: ${id}`);
    if (!listing) gaps.push(`native session absent from SDK listing: ${id}`);
    const session: SessionEvidence = { id, history: [], tools: [], persisted: snapshot ? { ...snapshot, messages: [...(messages.get(id)?.values() ?? [])], listing } : listing ? { listing } : undefined };
    if (listing || id === attachedId) {
      try { session.history = await client.getHistory(id); if (!Array.isArray(session.history)) throw new Error("not an array"); } catch (e) { session.history = []; gaps.push(`SDK history unavailable for ${id}: ${String(e)}`); }
      try { session.tools = await client.listTools(id); if (!Array.isArray(session.tools)) throw new Error("not an array"); } catch (e) { session.tools = []; gaps.push(`SDK tools unavailable for ${id}: ${String(e)}`); }
    } else {
      gaps.push(`SDK history unavailable for ${id}: session absent from SDK listing`);
      gaps.push(`SDK tools unavailable for ${id}: session absent from SDK listing`);
    }
    bundle.sessions.push(session); citations[`session:${id}`] = session;
    for (const [mid, msg] of messages.get(id) ?? []) citations[`session:${id}:message:${mid}`] = msg;
    let cost = 0, count = 0, missing = false;
    for (const msg of usage.get(id)?.values() ?? []) {
      const u = object(msg.token_usage); if (!u) continue;
      count++;
      for (const [source, target] of [["input_tokens", "inputTokens"], ["output_tokens", "outputTokens"], ["cache_read_input_tokens", "cacheReadInputTokens"]] as const) {
        const v = u[source]; if (typeof v === "number" && Number.isFinite(v) && v >= 0) bundle.costs[target] += v;
      }
      const value = u.cost_usd;
      if (typeof value === "number" && Number.isFinite(value) && value >= 0) cost += value; else missing = true;
    }
    bundle.costs.bySession[id] = count && !missing && Number.isFinite(cost) ? cost : null;
    if (!count || missing || !Number.isFinite(cost)) gaps.push(`provider-reported cost unknown for session ${id}`);
  }
  const costs = Object.values(bundle.costs.bySession);
  if (costs.length && costs.every((c): c is number => c !== null)) { const sum = costs.reduce((a, b) => a + b, 0); if (Number.isFinite(sum)) bundle.costs.totalUsd = sum; }
  const dispatchDir = join(jcodeHome, "dispatch-operations");
  for (const name of (await files(dispatchDir)).filter(n => /^(record|operation|task)-.*\.json$/.test(n))) {
    const raw = await file(join(dispatchDir, name)); if (raw === undefined) continue;
    const record = parse(raw, name); if (!record) continue;
    const taskId = text(object(record.input)?.task_id) ?? text(object(object(record.task)?.assignment)?.task_id) ?? text(object(record.assignment)?.task_id) ?? text(record.task_id);
    if (!taskId) { gaps.push(`dispatch record lacks task_id: ${name}`); continue; }
    const citation = `dispatch:${taskId}`;
    // Preserve multiple native files for one task without silently replacing them.
    if (citations[citation] !== undefined) { const prior = citations[citation]; citations[citation] = Array.isArray(prior) ? [...prior, record] : [prior, record]; } else citations[citation] = record;
    bundle.records.push({ ...record, citation });
  }
  for (const name of (await files(blueprintsDir)).filter(n => n.endsWith(".md"))) {
    const raw = await file(join(blueprintsDir, name)); if (raw !== undefined) citations[`blueprint:${name.slice(0, -3)}`] = raw;
  }
  return bundle;
}
