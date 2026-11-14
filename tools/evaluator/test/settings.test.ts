import { test, expect } from "bun:test";
import { chmodSync, mkdtempSync, rmSync, symlinkSync, writeFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { initializeSettings, loadSettings, mergeSettings, validateScenarioOverrides } from "../src/settings";

const good = `[provider]\ntype="openai-compatible"\nurl="https://example.org/v1"\nmodel="base"\napi_key="test-key"\n[models]\nswarm="swarm"\n[worker_models]\nexpert="expert"\n[effort]\nswarm="high"\n[limits]\nturn_timeout_seconds=42\n`;
test("private settings and public overrides resolve role and worker routing", () => {
  const dir = mkdtempSync(join(tmpdir(), "eval-settings-"));
  try {
    const file = join(dir, "config.toml"); writeFileSync(file, good, { mode: 0o600 });
    const base = loadSettings(file);
    expect(base.modelFor("coordinator")).toBe("base"); expect(base.modelForWorker("expert")).toBe("expert");
    expect(base.effortForWorker("unknown")).toBe("high");
    const merged = mergeSettings(base, validateScenarioOverrides({ models: { swarm: "other" }, limits: { acceptance_timeout_seconds: 7 } }));
    expect(merged.modelForWorker("unknown")).toBe("other"); expect(merged.limits.turnTimeoutSeconds).toBe(42); expect(merged.limits.acceptanceTimeoutSeconds).toBe(7);
    expect(() => validateScenarioOverrides({ provider: { api_key: "secret" } })).toThrow();
    expect(() => validateScenarioOverrides({ worker_effort: { coordinator: "high" } })).toThrow();
    expect(() => validateScenarioOverrides({ limits: { turn_timeout_seconds: 0 } })).toThrow();
    chmodSync(file, 0o644); expect(() => loadSettings(file)).toThrow();
    chmodSync(file, 0o600); symlinkSync(file, join(dir, "link")); expect(() => loadSettings(join(dir, "link"))).toThrow();
    writeFileSync(file, good.replace("test-key", "REPLACE_WITH_YOUR_API_KEY")); expect(() => loadSettings(file)).toThrow();
    writeFileSync(file, good.replace("https://example.org/v1", "http://example.org/v1")); expect(() => loadSettings(file)).toThrow();
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test("initialize copies adjacent template privately without overwrite", () => {
  const dir = mkdtempSync(join(tmpdir(), "eval-init-"));
  try {
    const file = join(dir, "config.toml"); writeFileSync(`${file}.example`, good);
    expect(initializeSettings(file)).toBe(file); expect(statSync(file).mode & 0o077).toBe(0);
    expect(loadSettings(file).apiKey).toBe("test-key"); expect(() => initializeSettings(file)).toThrow();
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
