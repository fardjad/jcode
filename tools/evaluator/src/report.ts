import { join } from "node:path";
import { atomicWrite } from "../../catalog/src/primitives";
import { Redactor } from "./redaction";

// The complete result is retained, including evidence gaps, findings and costs;
// the JSON rendering also accommodates per-scenario and run-level results.
export async function writeReport(output: string, result: unknown, secrets: readonly string[]): Promise<void> {
  const redactor = new Redactor(secrets);
  const sanitized = redactor.value(result);
  const json = JSON.stringify(sanitized, null, 2);
  if (json === undefined) throw new Error("Report result is not JSON serializable");
  const fence = "`".repeat(Math.max(3, ...Array.from(json.matchAll(/`+/g), match => match[0].length + 1)));
  await atomicWrite(join(output, "summary.json"), json + "\n", 0o600);
  await atomicWrite(join(output, "report.md"), `# Evaluation report\n\n${fence}json\n${json}\n${fence}\n`, 0o600);
}
