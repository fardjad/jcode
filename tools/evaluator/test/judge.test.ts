import { describe, expect, test } from "bun:test";
import { JcodeClient } from "@1jehuang/jcode-sdk";
import { runJudge, validateReview } from "../src/judge";
import type { Finding } from "../src/types";

const finding: Finding = { severity: "medium", category: "patch", text: "A possible issue", fix_target: "patches/example", citations: ["session:s:message:m"] };


test("runs the SDK structured protocol over a mock transport", async () => {
  let listener: (chunk: string) => void = () => {};
  const requests: Record<string, unknown>[] = [];
  let invalid = false;
  const send = (frame: object) => listener(JSON.stringify({ v: 1, ...frame }) + "\n");
  const transport = {
    onData: (fn: (chunk: string | Buffer) => void) => { listener = fn; },
    onClose: (_fn: (error?: Error) => void) => {},
    close: () => {},
    write: (data: string) => {
      const request = JSON.parse(data) as Record<string, unknown>;
      requests.push(request);
      queueMicrotask(() => {
        if (request.req === "hello") send({ reply_to: request.id, ev: "hello_ok", server: "mock", capabilities: ["session_tools"] });
        else if (request.req === "create_session") send({ reply_to: request.id, ev: "attached", session: { session_id: "judge" } });
        else if (request.req === "send_message") {
          send({ ev: "message_accepted", session_id: "judge" });
          send({ ev: "text_delta", session_id: "judge", text: invalid ? "not JSON" : JSON.stringify({ findings: [finding] }) });
          send({ ev: "turn_done", session_id: "judge" });
        } else send({ reply_to: request.id, ev: "ok" });
      });
    },
  };
  const client = await JcodeClient.connect({ transport });
  try {
    expect(await runJudge(client, "/workspace", "model", undefined, {}, { "session:s:message:m": {} }, 1000)).toEqual({ findings: [finding] });
    expect(requests.find(request => request.req === "configure_tools")).toMatchObject({ session_id: "judge", tools: { enabled: [] } });
    invalid = true;
    const before = requests.filter(request => request.req === "send_message").length;
    await expect(runJudge(client, "/workspace", "model", undefined, {}, {}, 1000)).rejects.toThrow();
    expect(requests.filter(request => request.req === "send_message").length - before).toBe(1);
  } finally { await client.close(); }
});
describe("diagnostic judge", () => {
  test("creates a toolless session and uses structured output with registered citations", async () => {
    const calls: unknown[][] = [];
    const client = {
      createSession: async (...args: unknown[]) => { calls.push(["create", ...args]); return { session_id: "judge" }; },
      setModel: async (...args: unknown[]) => { calls.push(["model", ...args]); },
      setReasoningEffort: async (...args: unknown[]) => { calls.push(["effort", ...args]); },
      runStructured: async (...args: unknown[]) => { calls.push(["run", ...args]); return { data: { findings: [finding] } }; },
    } as unknown as JcodeClient;
    const citations = { "session:s:message:m": { content: "evidence" } };
    expect(await runJudge(client, "/workspace", "provider:model", "high", { scenario: "test" }, citations, 1000)).toEqual({ findings: [finding] });
    expect(calls[0]?.[1]).toMatchObject({ workingDir: "/workspace", tools: { enabled: [] } });
    expect(calls[1]).toEqual(["model", "judge", "provider:model"]);
    expect(calls[2]).toEqual(["effort", "judge", "high"]);
    expect(JSON.parse(calls[3]?.[2] as string)).toEqual({ evidence: { scenario: "test" }, citations });
    expect(calls[3]?.[3]).toMatchObject({ schema: { additionalProperties: false } });
  });

  test("rejects invented, duplicate, empty and malformed findings", () => {
    const refs = { "session:s:message:m": {} };
    for (const changes of [{ citations: ["toString"] }, { citations: [] }, { citations: [finding.citations[0], finding.citations[0]] }, { text: "  " }, { category: "other" }, { extra: true }]) {
      expect(() => validateReview({ findings: [{ ...finding, ...changes }] }, refs)).toThrow();
    }
    expect(() => validateReview({ findings: [], extra: true }, refs)).toThrow();
  });

  test("cancels a timed out judge turn", async () => {
    let cancelled = "";
    const client = {
      createSession: async () => ({ session_id: "judge" }),
      setModel: async () => {},
      runStructured: () => Promise.withResolvers().promise,
      cancel: async (id: string) => { cancelled = id; },
    } as unknown as JcodeClient;
    await expect(runJudge(client, "/workspace", "model", undefined, {}, {}, 5)).rejects.toThrow("timed out");
    expect(cancelled).toBe("judge");
  });
});
