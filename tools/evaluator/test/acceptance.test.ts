import { expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { loadScenarios, prepareFixture } from "../src/scenarios";
import { prepareBlindRuntime } from "../src/blind_runtime";
import { runAcceptance } from "../src/runner";

function writeExtractionSolution(workspace: string) {
  writeFileSync(join(workspace, "aggregate.py"), [
    "import json",
    "from pathlib import Path",
    "facts = (line.split(':', 1) for path in Path('facts').glob('*.txt') for line in path.read_text().splitlines() if line.strip())",
    "Path('summary.json').write_text(json.dumps({name.strip(): color.strip() for name, color in facts}))",
  ].join("\n") + "\n");
  writeFileSync(join(workspace, "summary.json"), JSON.stringify({ ant: "red", bee: "gold", cat: "black", dog: "brown", eel: "blue", fox: "orange", hen: "white", ibis: "pink" }));
}

test.skipIf(process.platform !== "linux")("independent acceptance ignores a modified verifier but tests the produced code", async () => {
  const root = mkdtempSync("/var/tmp/jcode-accept-test-");
  try {
    const scenario = loadScenarios(join(import.meta.dir, "../../../customizations/workers/evals/scenarios"), "blocker-assistance")[0]!;
    const workspace = join(root, "workspace");
    const baseline = prepareFixture(scenario, workspace);
    writeFileSync(join(workspace, "accept_test.py"), "import unittest\n");
    writeFileSync(join(workspace, "unittest.py"), "raise RuntimeError('workspace must not replace unittest')\n");
    expect((await runAcceptance(scenario, workspace, baseline, 10000)).status).toBe("fail");
    writeFileSync(join(workspace, "parser.py"), "def parse(line):\n    parts = line.split('=')\n    if len(parts) != 2 or not parts[0].strip():\n        raise ValueError('invalid')\n    return parts[0].strip(), int(parts[1].strip())\n");
    expect(await runAcceptance(scenario, workspace, baseline, 10000)).toMatchObject({ status: "pass", stderr: expect.any(String) });
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test.skipIf(process.platform !== "linux")("extraction acceptance copies its workspace and checks fresh facts", async () => {
  const root = mkdtempSync("/var/tmp/jcode-accept-test-");
  try {
    const scenario = loadScenarios(join(import.meta.dir, "../../../customizations/workers/evals/scenarios"), "extraction")[0]!;
    const workspace = join(root, "workspace");
    const baseline = prepareFixture(scenario, workspace);
    expect((await runAcceptance(scenario, workspace, baseline, 10_000)).status).toBe("fail");
    writeExtractionSolution(workspace);
    expect(await runAcceptance(scenario, workspace, baseline, 10_000)).toMatchObject({ status: "pass", stderr: expect.any(String) });
    writeFileSync(join(workspace, "aggregate.py"), "raise RuntimeError('incorrect solution')\n");
    expect((await runAcceptance(scenario, workspace, baseline, 10_000)).status).toBe("fail");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test.skipIf(process.platform !== "linux")("worker can run the real extraction verifier in the coordinator sandbox", async () => {
  const root = mkdtempSync("/var/tmp/jcode-worker-test-");
  const sdkHome = mkdtempSync("/tmp/jcode-worker-sdk-");
  try {
    const scenario = loadScenarios(join(import.meta.dir, "../../../customizations/workers/evals/scenarios"), "extraction")[0]!;
    const workspace = join(root, "workspace");
    prepareFixture(scenario, workspace);
    writeExtractionSolution(workspace);
    const { binary } = await prepareBlindRuntime({ stagingRoot: root, sdkHome, binary: "/usr/bin/python3", bridge: "/usr/bin/true" });
    const child = Bun.spawn([binary, "-m", "unittest", "accept_test.py"], { stdout: "pipe", stderr: "pipe" });
    const [status, stderr] = await Promise.all([child.exited, new Response(child.stderr).text()]);
    expect({ status, stderr }).toMatchObject({ status: 0, stderr: expect.stringContaining("OK") });
  } finally { rmSync(root, { recursive: true, force: true }); rmSync(sdkHome, { recursive: true, force: true }); }
}, 15_000);
