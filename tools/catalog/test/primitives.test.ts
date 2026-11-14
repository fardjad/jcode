import { expect, test } from "bun:test";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { atomicWrite, withFileLock } from "../src/primitives";

test("atomic writes replace complete contents without leaving temporary files", async () => {
  const dir = await mkdtemp(join(tmpdir(), "jcode-atomic-"));
  try {
    const path = join(dir, "nested", "result.json");
    await atomicWrite(path, "before");
    await atomicWrite(path, "after");
    expect(await readFile(path, "utf8")).toBe("after");
    expect(await readdir(join(dir, "nested"))).toEqual(["result.json"]);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("process locks reject concurrent owners and release on process exit", async () => {
  const dir = await mkdtemp(join(tmpdir(), "jcode-lock-"));
  const path = join(dir, "runner.lock");
  const module = new URL("../src/primitives.ts", import.meta.url).pathname;
  const script = `import { withFileLock } from ${JSON.stringify(module)}; await withFileLock(${JSON.stringify(path)}, async () => { console.log('held'); const { promise, resolve } = Promise.withResolvers(); process.stdin.once('end', resolve); process.stdin.resume(); await promise; });`;
  const child = Bun.spawn([process.execPath, "-e", script], { stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  try {
    const reader = child.stdout.getReader();
    const first = await reader.read();
    expect(new TextDecoder().decode(first.value)).toContain("held");
    await expect(withFileLock(path, async () => undefined)).rejects.toThrow("Lock already held");
    child.kill();
    await child.exited;
    expect(await withFileLock(path, async () => "acquired")).toBe("acquired");
  } finally {
    child.kill();
    await child.exited;
    await rm(dir, { recursive: true, force: true });
  }
});

test.skipIf(process.platform === "win32")("child retains lock if the parent dies", async () => {
  const dir = await mkdtemp(join(tmpdir(), "jcode-inherited-lock-"));
  const path = join(dir, "runner.lock");
  const module = new URL("../src/primitives.ts", import.meta.url).pathname;
  const worker = `console.log('held'); const { promise, resolve } = Promise.withResolvers(); process.stdin.once('end', resolve); process.stdin.resume(); await promise;`;
  const parent = `import { withFileLock } from ${JSON.stringify(module)}; await withFileLock(${JSON.stringify(path)}, async fd => { const child = Bun.spawn([process.execPath, '-e', ${JSON.stringify(worker)}], {stdio: ['inherit', 'inherit', 'inherit', fd]}); await child.exited; });`;
  const owner = Bun.spawn([process.execPath, "-e", parent], { stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  const reader = owner.stdout.getReader();
  try {
    expect(new TextDecoder().decode((await reader.read()).value)).toContain("held");
    owner.kill();
    await owner.exited;
    await expect(withFileLock(path, async () => undefined)).rejects.toThrow("Lock already held");
    owner.stdin.end();
    while (!(await reader.read()).done) { /* Drain until the child exits. */ }
    expect(await withFileLock(path, async () => "acquired")).toBe("acquired");
  } finally {
    owner.stdin.end();
    owner.kill();
    await owner.exited;
    await rm(dir, { recursive: true, force: true });
  }
});
