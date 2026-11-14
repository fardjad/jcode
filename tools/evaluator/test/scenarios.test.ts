import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, readFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { loadScenarios, prepareFixture, verifyFixture } from "../src/scenarios";

const scenarios = resolve(import.meta.dir, "../../../customizations/workers/evals/scenarios");
const solutions: Record<string, { file: string; code: string; command?: string; repeat?: boolean }> = {
  extraction: { file: "aggregate.py", command: "aggregate.py", code: `import json\nfrom pathlib import Path\nfacts = {}\nfor path in sorted(Path("facts").glob("*.txt")):\n    for line in path.read_text().splitlines():\n        key, value = line.split(":", 1)\n        facts[key.strip()] = value.strip()\nPath("summary.json").write_text(json.dumps(facts))\n` },
  "blocker-assistance": { file: "parser.py", command: "check.py", code: `def parse(line):\n    if line.count("=") != 1:\n        raise ValueError("separator")\n    name, value = line.split("=")\n    if not name.strip():\n        raise ValueError("name")\n    return name.strip(), int(value.strip())\n` },
  "nested-handback": { file: "stages.py", command: "check.py", code: `def split_record(line):\n    parts = line.split("|")\n    if len(parts) != 2:\n        raise ValueError("separator")\n    return parts\n\ndef clean_record(parts):\n    name, quantity = parts\n    if not name.strip():\n        raise ValueError("name")\n    return name.strip(), int(quantity.strip())\n` },
  "retry-budget": { file: "normalize.py", command: "check.py", repeat: true, code: `import csv\nwith open("input.csv", newline="") as source, open("output.csv", "w", newline="") as dest:\n    reader, writer = csv.reader(source), csv.writer(dest)\n    writer.writerow(next(reader))\n    for row in reader:\n        writer.writerow([row[0], row[1].strip().lower()])\n` },
  "output-guard": { file: "summarize.py", command: "summarize.py", code: `import csv, json\nfrom pathlib import Path\nwith open("lookup.csv", newline="") as source:\n    rows = list(csv.DictReader(source))\ngroups = sorted({row["group"] for row in rows})\nPath("summary.json").write_text(json.dumps({"rows": len(rows), "groups": groups}))\n` },
  recovery: { file: "batch.py", command: "batch.py", repeat: true, code: `import json\nfrom pathlib import Path\nimport checkpoint\nrecords = [line for line in Path("input.txt").read_text().splitlines() if line]\nstate = Path("checkpoint.json")\ncompleted = json.loads(state.read_text())["completed"] if state.exists() else 0\nfor item in records[completed:]:\n    with open("results.txt", "a") as output:\n        output.write(item.upper() + "\\n")\n        output.flush()\n    completed += 1\n    state.write_text(json.dumps({"completed": completed}))\n    checkpoint.saved(completed)\n` },
  "polyglot-rest-api-fixer": { file: "rest_api.py", code: `import json\nclass RestAPI:\n    def __init__(self, database):\n        self.users = database['users']\n    def get(self, url, payload=None):\n        names = json.loads(payload)['users'] if payload else None\n        return json.dumps({'users': sorted([u for u in self.users if names is None or u['name'] in names], key=lambda u: u['name'])})\n    def post(self, url, payload=None):\n        data = json.loads(payload)\n        if url == '/add':\n            user = {'name': data['user'], 'owes': {}, 'owed_by': {}, 'balance': 0.0}\n            self.users.append(user)\n            return json.dumps(user)\n        lender = next(u for u in self.users if u['name'] == data['lender'])\n        borrower = next(u for u in self.users if u['name'] == data['borrower'])\n        ln, bn, amount = lender['name'], borrower['name'], data['amount']\n        net = borrower['owes'].get(ln, 0) - lender['owes'].get(bn, 0) + amount\n        for mapping, key in [(borrower['owes'], ln), (lender['owed_by'], bn), (lender['owes'], bn), (borrower['owed_by'], ln)]:\n            mapping.pop(key, None)\n        if net > 0:\n            borrower['owes'][ln] = net\n            lender['owed_by'][bn] = net\n        elif net < 0:\n            lender['owes'][bn] = -net\n            borrower['owed_by'][ln] = -net\n        lender['balance'] += amount\n        borrower['balance'] -= amount\n        return json.dumps({'users': sorted([lender, borrower], key=lambda u: u['name'])})\n` },
};
test("all seven fixtures fail baseline and accept independent known-good solutions", () => {
  const loaded = loadScenarios(scenarios); expect(loaded).toHaveLength(7);
  for (const scenario of loaded) {
    const root = mkdtempSync(join(tmpdir(), "eval-fixture-")), workspace = join(root, "workspace"), home = join(root, "home");
    try {
      mkdirSync(home); const baseline = prepareFixture(scenario, workspace); const solution = solutions[scenario.id]; expect(solution).toBeDefined();
      const run = (argv: string[]) => spawnSync(argv[0], argv.slice(1), { cwd: workspace, env: { PATH: process.env.PATH, HOME: home, PYTHONDONTWRITEBYTECODE: "1" }, encoding: "utf8" });
      expect(run(scenario.accept).status).not.toBe(0);
      writeFileSync(join(workspace, solution.file), solution.code);
      if (solution.command) {
        if (solution.repeat) expect(run(["python3", solution.command]).status).not.toBe(0);
        const runResult = run(["python3", solution.command]); expect({ id: scenario.id, status: runResult.status, stderr: runResult.stderr }).toEqual({ id: scenario.id, status: 0, stderr: "" });
      }
      const accepted = run(scenario.accept); expect({ id: scenario.id, status: accepted.status, stderr: accepted.status === 0 ? "" : accepted.stderr }).toEqual({ id: scenario.id, status: 0, stderr: "" });
      verifyFixture(scenario, baseline);
    } finally { rmSync(root, { recursive: true, force: true }); }
  }
});
test("scenario schema rejects symlinks and detects source mutation", () => {
  const root = mkdtempSync(join(tmpdir(), "eval-schema-"));
  try {
    const fixture = join(root, "fixtures"); mkdirSync(fixture); writeFileSync(join(fixture, "file"), "one");
    const file = join(root, "config.toml"); writeFileSync(file, `id="sample"\nprompt="test"\naccept=["true"]\n[judge]\ncriteria=["test"]\n[simulated_user]\nmax_turns=1\ninstructions="proceed"\n`);
    const scenario = loadScenarios(file)[0], baseline = prepareFixture(scenario, join(root, "workspace"));
    writeFileSync(join(fixture, "file"), "two"); expect(() => verifyFixture(scenario, baseline)).toThrow();
    symlinkSync(join(fixture, "file"), join(fixture, "link")); expect(() => loadScenarios(file)).toThrow();
    expect(readFileSync(join(root, "workspace", "file"), "utf8")).toBe("one");
  } finally { rmSync(root, { recursive: true, force: true }); }
});
