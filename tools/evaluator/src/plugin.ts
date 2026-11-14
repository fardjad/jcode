import { createServer, type Server } from "node:net";
import { chmodSync, copyFileSync, lstatSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const limit = 2 * 1024 * 1024;
const allowed = ["JCODE_HOOK_TOOL_NAME", "JCODE_HOOK_TOOL_INPUT", "JCODE_HOOK_TOUCHED_PATHS"] as const;
export interface HookTrace { plugin: string; inputBytes: number; outputBytes: number; exitCode: number; }

/** Keep production hook code and traces outside the model-visible filesystem. */
export async function startPluginBroker(pluginRoot: string, privateHome: string, publicBin: string, workspace: string, path: string): Promise<{ command: string; traces: HookTrace[]; close(): Promise<void> }> {
  const source = join(pluginRoot, "rtk", "rtk-transform");
  const info = lstatSync(source);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error("required rtk hook must be a regular file");
  // The broker and sandbox must use the same RTK: a rewrite invokes it again
  // inside the sandbox. Without RTK, the adapter retains the original command.
  const rtk = Bun.which("rtk", { PATH: path });
  if (rtk) {
    const staged = join(publicBin, "rtk");
    copyFileSync(rtk, staged);
    chmodSync(staged, 0o755);
  }
  const privateDir = join(privateHome, "plugins");
  mkdirSync(privateDir, { mode: 0o700 });
  const executable = join(privateDir, "rtk-transform");
  copyFileSync(source, executable);
  chmodSync(executable, 0o700);
  const socket = join(privateHome, "run", "hook.sock");
  const command = join(publicBin, "hook-proxy");
  const script = `#!/usr/bin/python3
import base64, json, os, socket, sys
keys = ${JSON.stringify(allowed)}
data = sys.stdin.buffer.read(${limit + 1})
if len(data) > ${limit}: sys.exit(1)
req = json.dumps({'env': {k: os.environ[k] for k in keys if k in os.environ}, 'input': base64.b64encode(data).decode()}).encode()
s = socket.socket(socket.AF_UNIX)
s.settimeout(2)
s.connect(${JSON.stringify(socket)})
s.sendall(req)
s.shutdown(socket.SHUT_WR)
out = bytearray()
while True:
    chunk = s.recv(65536)
    if not chunk: break
    out.extend(chunk)
    if len(out) > ${limit * 2}: sys.exit(1)
result = json.loads(out)
sys.stdout.buffer.write(base64.b64decode(result['output']))
sys.exit(result['exitCode'])
`;
  writeFileSync(command, script, { mode: 0o755, flag: "wx" });
  const traces: HookTrace[] = [];
  const sockets = new Set<import("node:net").Socket>();
  const server: Server = createServer({ allowHalfOpen: true }, connection => {
    sockets.add(connection);
    connection.on("close", () => sockets.delete(connection));
    connection.setTimeout(2000, () => connection.destroy());
    const chunks: Buffer[] = []; let size = 0;
    connection.on("data", data => { const chunk = Buffer.isBuffer(data) ? data : Buffer.from(data); size += chunk.length; if (size > limit * 2) connection.destroy(); else chunks.push(chunk); });
    connection.on("error", () => {});
    connection.on("end", async () => {
      try {
        const request: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!request || typeof request !== "object" || Array.isArray(request)) throw new Error("invalid hook request");
        const { env, input } = request as { env?: unknown; input?: unknown };
        if (!env || typeof env !== "object" || Array.isArray(env) || typeof input !== "string") throw new Error("invalid hook request");
        const forwarded: Record<string, string> = {};
        for (const key of allowed) {
          const value = (env as Record<string, unknown>)[key];
          if (value !== undefined) {
            if (typeof value !== "string" || value.length > limit || value.includes("\0")) throw new Error("invalid hook environment");
            forwarded[key] = value;
          }
        }
        const bytes = Buffer.from(input, "base64");
        if (bytes.length > limit) throw new Error("hook input too large");
        const child = Bun.spawn([executable], { cwd: workspace, env: { PATH: `${publicBin}:/usr/bin:/bin`, HOME: privateHome, PYTHONDONTWRITEBYTECODE: "1", ...forwarded }, stdin: "pipe", stdout: "pipe", stderr: "ignore" });
        child.stdin.write(bytes); child.stdin.end();
        const timer = setTimeout(() => child.kill("SIGKILL"), 1800);
        let exitCode: number; let output: Uint8Array;
        try { [exitCode, output] = await Promise.all([child.exited, new Response(child.stdout).bytes()]); }
        finally { clearTimeout(timer); }
        if (output.length > limit) throw new Error("hook output too large");
        traces.push({ plugin: "rtk", inputBytes: bytes.length, outputBytes: output.length, exitCode });
        connection.end(JSON.stringify({ exitCode, output: Buffer.from(output).toString("base64") }));
      } catch {
        connection.end(JSON.stringify({ exitCode: 1, output: "" }));
      }
    });
  });
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(socket, () => { server.off("error", reject); resolve(); }); });
  return { command, traces, close: () => new Promise<void>((resolve, reject) => { for (const connection of sockets) connection.destroy(); server.close(error => error ? reject(error) : resolve()); }) };
}
