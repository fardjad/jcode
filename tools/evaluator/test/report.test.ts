import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeReport } from "../src/report";

test("writes owner-only redacted reports retaining findings, gaps and provider costs", async () => {
  const root = await mkdtemp(join(tmpdir(), "judge-report-"));
  try {
    const secret = "provider-key/secret";
    const encoded = Buffer.from(secret).toString("base64");
    const result = { findings: [{ text: `finding ${secret}`, citations: ["session:s"] }], gaps: [`missing ${encoded}`], costs: { totalUsd: 1.25, bySession: { s: 1.25 } }, [secret]: "hidden" };
    await writeReport(root, result, [secret]);
    for (const name of ["summary.json", "report.md"]) {
      const path = join(root, name);
      const text = await readFile(path, "utf8");
      expect(text).not.toContain(secret);
      expect(text).not.toContain(encoded);
      expect(text).toContain("[REDACTED]");
      expect(text).toContain("1.25");
      expect(text).toContain("missing");
      expect(text).toContain("finding");
      if (process.platform !== "win32") expect((await stat(path)).mode & 0o777).toBe(0o600);
    }
    const summary = JSON.parse(await readFile(join(root, "summary.json"), "utf8"));
    expect(summary.costs.bySession.s).toBe(1.25);
    expect(summary["[REDACTED]"]).toBe("hidden");
  } finally { await rm(root, { recursive: true, force: true }); }
});
