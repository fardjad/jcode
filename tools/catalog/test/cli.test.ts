import { expect, test } from "bun:test";
import { fileURLToPath } from "node:url";

const cli = fileURLToPath(new URL("../src/cli.ts", import.meta.url));

async function invoke(...args: string[]) {
  const child = Bun.spawn([process.execPath, cli, ...args], { stdout: "pipe", stderr: "pipe" });
  const [stdout, stderr, code] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ]);
  return { stdout, stderr, code };
}

test("CLI rejects an invalid release before changing the pin", async () => {
  const result = await invoke("version", "set", "master");
  expect(result.code).not.toBe(0);
  expect(result.stderr).toContain("stable vX.Y.Z");
});

test("CLI rejects ambiguous patch selection before launching validation", async () => {
  const result = await invoke("patches", "test", "one.patch", "--all");
  expect(result.code).not.toBe(0);
  expect(result.stderr).toContain("use PATCH or --all/--only");
});

test("CLI rejects an --only option without its patch", async () => {
  const result = await invoke("patches", "test", "--only", "--force");
  expect(result.code).not.toBe(0);
  expect(result.stderr).toContain("--only requires a patch name");
});
