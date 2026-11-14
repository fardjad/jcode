import { expect, test } from "bun:test";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { startPluginBroker } from "../src/plugin";

test.skipIf(process.platform === "win32")("plugin broker runs the private hook with only allowed tool metadata", async () => {
  const root = mkdtempSync("/var/tmp/jcode-hook-test-");
  try {
    const plugins = join(root, "plugins"), home = join(root, "private"), bin = join(root, "bin"), workspace = join(root, "workspace");
    mkdirSync(join(plugins, "rtk"), { recursive: true }); mkdirSync(join(home, "run"), { recursive: true }); mkdirSync(bin); mkdirSync(workspace);
    const executable = join(plugins, "rtk", "rtk-transform");
    writeFileSync(executable, "#!/usr/bin/python3\nimport json, os, sys\nprint(json.dumps({'input': sys.stdin.read(), 'tool': os.environ.get('JCODE_HOOK_TOOL_NAME'), 'leak': os.environ.get('UNTRUSTED_SECRET')}))\n");
    chmodSync(executable, 0o700);
    const broker = await startPluginBroker(plugins, home, bin, workspace, "/usr/bin:/bin");
    try {
      const child = Bun.spawn([broker.command], { env: { PATH: "/usr/bin:/bin", JCODE_HOOK_TOOL_NAME: "bash", UNTRUSTED_SECRET: "not forwarded" }, stdin: "pipe", stdout: "pipe", stderr: "pipe" });
      child.stdin.write("test input"); child.stdin.end();
      const [exitCode, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
      expect({ exitCode, stderr }).toEqual({ exitCode: 0, stderr: "" });
      expect(JSON.parse(stdout)).toEqual({ input: "test input", tool: "bash", leak: null });
      expect(broker.traces).toEqual([{ plugin: "rtk", inputBytes: 10, outputBytes: Buffer.byteLength(stdout), exitCode: 0 }]);
    } finally { await broker.close(); }
  } finally { rmSync(root, { recursive: true, force: true }); }
});
