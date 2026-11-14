import { expect, test } from "bun:test";
import { SandboxManager } from "@anthropic-ai/sandbox-runtime";
import { once } from "node:events";
import { chmod, mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { createServer as createHttpServer } from "node:http";
import { createServer, type AddressInfo, type Server } from "node:net";
import { join } from "node:path";
import { prepareBlindRuntime, runBlindCommand } from "../src/blind_runtime";
import { startPluginBroker } from "../src/plugin";

const python = "/usr/bin/python3";
const trueBinary = "/usr/bin/true";

async function fixture() {
  const root = await mkdtemp("/var/tmp/jc-blind-");
  const sdkHome = await mkdtemp("/tmp/jc-sdk-");
  return {
    root, sdkHome,
    options: { stagingRoot: root, sdkHome, binary: python, bridge: trueBinary },
    cleanup: async () => { await rm(root, { recursive: true, force: true }); await rm(sdkHome, { recursive: true, force: true }); },
  };
}

const sandboxAvailable = process.platform === "linux" && (await SandboxManager.checkDependenciesAsync()).errors.length === 0;

async function invoke(binary: string, argv: string[], env: Record<string, string> = {}) {
  const child = Bun.spawn([binary, ...argv], { env: { ...process.env, ...env }, stdin: "ignore", stdout: "pipe", stderr: "pipe" });
  const [status, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
  return { status, stdout, stderr };
}

test.skipIf(!sandboxAvailable)("hook rewrites run with the same RTK inside the sandbox", async () => {
  const f = await fixture();
  try {
    const toolDir = join(f.sdkHome, "tools");
    await mkdir(toolDir);
    const rtk = join(toolDir, "rtk");
    await writeFile(rtk, "#!/bin/sh\nif [ \"$1\" = rewrite ]; then echo 'rtk git status'; elif [ \"$1 $2\" = 'git status' ]; then echo 'sandbox rtk ran'; else exit 1; fi\n", { mode: 0o755 });
    const { binary } = await prepareBlindRuntime(f.options);
    const broker = await startPluginBroker(join(import.meta.dir, "../../../customizations/plugins"), f.sdkHome, join(f.root, "bin"), join(f.root, "workspace"), `${toolDir}:/usr/bin:/bin`);
    try {
      const code = [
        "import json, os, subprocess",
        "env = dict(os.environ, JCODE_HOOK_TOOL_NAME='bash')",
        `hook = subprocess.run([${JSON.stringify(broker.command)}], input=json.dumps({'command': 'git status --short'}), text=True, capture_output=True, check=True, env=env)`,
        "command = json.loads(hook.stdout)['command']",
        "assert command == 'rtk git status', command",
        "result = subprocess.run(['/bin/sh', '-c', command], text=True, capture_output=True, check=True)",
        "print(result.stdout.strip())",
      ].join("\n");
      const result = await invoke(binary, ["-c", code]);
      expect(result.status).toBe(0);
      expect(result.stdout.trim()).toBe("sandbox rtk ran");
    } finally { await broker.close(); }
  } finally { await f.cleanup(); }
}, 15_000);

test.skipIf(!sandboxAvailable)("sandbox proxy reaches the allowed host and denies other hosts", async () => {
  const f = await fixture();
  const server = createHttpServer((_request, response) => response.end("local provider"));
  try {
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const port = (server.address() as AddressInfo).port;
    const { binary } = await prepareBlindRuntime({ ...f.options, allowedDomains: ["127.0.0.1"] });
    const program = [
      "import urllib.request, urllib.error",
      "def request(url):",
      " try:",
      "  with urllib.request.urlopen(url, timeout=5) as r: print(r.status, r.read().decode())",
      " except urllib.error.HTTPError as e: print(e.code)",
      `request('http://127.0.0.1:${port}/')`,
      "request('http://not-allowed.invalid/')",
    ].join("\n");
    const result = await invoke(binary, ["-c", program]);
    expect(result.status).toBe(0);
    expect(result.stdout).toContain("200 local provider");
    expect(result.stdout).toContain("403");
  } finally {
    server.close();
    await f.cleanup();
  }
}, 15_000);

test("rejects unsafe control and staging paths before launching", async () => {
  const f = await fixture();
  try {
    await expect(prepareBlindRuntime({ ...f.options, sdkHome: f.root })).rejects.toThrow("separate");
    await expect(prepareBlindRuntime({ ...f.options, stagingRoot: f.sdkHome })).rejects.toThrow("/var/tmp");
    await chmod(f.root, 0o755);
    await expect(prepareBlindRuntime(f.options)).rejects.toThrow("private");
    await chmod(f.root, 0o700);
    const link = join(f.root, "control-link");
    await symlink(f.sdkHome, link);
    await expect(prepareBlindRuntime({ ...f.options, sdkHome: link })).rejects.toThrow("symlink");
    await expect(prepareBlindRuntime({ ...f.options, readonlyPaths: [f.sdkHome] })).rejects.toThrow("inside the staging root");
    await expect(prepareBlindRuntime({ ...f.options, readonlyPaths: [link] })).rejects.toThrow("without symlinks");
    await writeFile(join(f.sdkHome, "servers.json"), "synthetic registry");
    await expect(prepareBlindRuntime(f.options)).rejects.toThrow("server registry");
    await expect(runBlindCommand({ workspace: f.sdkHome, argv: ["true"], timeoutMs: 1 })).rejects.toThrow("/var/tmp");
    await expect(runBlindCommand({ workspace: f.root, argv: [], timeoutMs: 1 })).rejects.toThrow("empty");
  } finally { await f.cleanup(); }
});

test("fails closed if sandbox-runtime dependencies are unavailable", async () => {
  if (sandboxAvailable) return;
  const f = await fixture();
  try {
    await expect(prepareBlindRuntime(f.options)).rejects.toThrow(/Linux|Node.js|ripgrep|socat|sandbox/i);
  } finally { await f.cleanup(); }
});

test.skipIf(!sandboxAvailable)("SDK wrapper isolates files, process IDs, sockets and inherited environment", async () => {
  const f = await fixture();
  const secretDir = await mkdtemp("/var/tmp/jc-private-");
  const secret = join(secretDir, "secret.txt");
  const hostSocket = join(secretDir, "session.sock");
  const hostServer = createServer((socket) => socket.end());
  const hostLoopback = createServer((socket) => socket.end());
  const abstractServer = createServer((socket) => socket.end());
  let apiServer: Server | undefined;
  try {
    await writeFile(secret, "synthetic secret");
    hostServer.listen(hostSocket);
    await once(hostServer, "listening");
    hostLoopback.listen(0, "127.0.0.1");
    await once(hostLoopback, "listening");
    const loopbackAddress = hostLoopback.address() as AddressInfo;
    const abstractName = "\0jc-blind-abstract";
    abstractServer.listen(abstractName);
    await once(abstractServer, "listening");
    const originalMode = (await stat(python)).mode;
    const policyDir = join(f.root, "policy");
    await mkdir(policyDir);
    const policy = join(policyDir, "policy.txt");
    await writeFile(policy, "immutable policy");
    const apiSocket = join(f.sdkHome, "run", "jcode-api.sock");
    const { binary } = await prepareBlindRuntime({ ...f.options, readonlyPaths: [policyDir], allowedDomains: ["provider.example.invalid"] });
    const sandboxPolicy = JSON.parse(await readFile(join(f.root, "runtime", "sandbox-settings.json"), "utf8"));
    expect(sandboxPolicy.network.allowedDomains).toEqual(["provider.example.invalid"]);
    expect(sandboxPolicy.network.strictAllowlist).toBe(true);
    expect(sandboxPolicy.network.allowAllUnixSockets).toBe(true);
    expect(sandboxPolicy.filesystem.denyRead).toContain("/");
    expect(sandboxPolicy.filesystem.allowWrite).toContain(f.root);
    expect(sandboxPolicy.filesystem.denyWrite).toContain(policyDir);
    expect(sandboxPolicy.filesystem.denyWrite).toContain(join(f.root, "bin"));
    expect((await stat(python)).mode).toBe(originalMode);
    expect(await readFile(join(f.root, "bin", "jcode"))).toEqual(await readFile(python));
    expect(await readFile(join(f.root, "bin", "jcode-harness-api-bridge"))).toEqual(await readFile(trueBinary));
    await writeFile(join(f.sdkHome, "servers.json"), "host-only registry");
    apiServer = createServer((socket) => socket.end("ok"));
    apiServer.listen(apiSocket);
    await once(apiServer, "listening");
    const program = `import os, sys, json, socket\n` +
      `def connect(p):\n try:\n  s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(p); s.recv(2); s.close(); return True\n except OSError: return False\n` +
      `def connect_tcp():\n try:\n  s=socket.create_connection(('127.0.0.1', ${loopbackAddress.port}), timeout=2); s.close(); return True\n except OSError: return False\n` +
      `def modify(p):\n try:\n  open(p,'w').write('tampered'); return True\n except OSError: return False\n` +
      `open(os.path.join(os.environ['JCODE_HOME'],'servers.json'),'w').write('sandbox pid registry')\n` +
      `print(json.dumps({'args':sys.argv[1:], 'env':dict(os.environ), 'cwd':os.getcwd(), ` +
      `'secret':os.path.exists(${JSON.stringify(secret)}), 'registry':os.path.exists(${JSON.stringify(join(f.sdkHome, "servers.json"))}), ` +
      `'host_socket':connect(${JSON.stringify(hostSocket)}), 'host_loopback':connect_tcp(), 'abstract_socket':connect(${JSON.stringify(abstractName)}), 'api_socket':connect(${JSON.stringify(apiSocket)}), ` +
      `'proc_pid':os.getpid(), 'usr_readonly':bool(os.statvfs('/usr').f_flag & os.ST_RDONLY), ` +
      `'etc_readonly':bool(os.statvfs('/etc').f_flag & os.ST_RDONLY), 'policy_modified':modify(${JSON.stringify(policy)}), ` +
      `'home_secret':os.path.exists(${JSON.stringify(process.env.HOME)}), 'run_user':os.path.exists('/run/user')}))`;
    const result = await invoke(binary, ["-c", program, "argument with spaces", "'quoted'"], {
      MALICIOUS_SECRET: "do-not-forward", SSH_AUTH_SOCK: hostSocket, DBUS_SESSION_BUS_ADDRESS: `unix:path=${hostSocket}`,
      XDG_RUNTIME_DIR: secretDir, JCODE_HOME: secretDir, JCODE_SOCKET: hostSocket, PATH: secretDir,
      JCODE_ACCEPTANCE_API_KEY: "explicit 'synthetic' key", JCODE_SWARM_MODEL: "worker_acceptance:synthetic",
    });
    expect(result.status).toBe(0);
    const data = JSON.parse(result.stdout);
    expect(data.args).toEqual(["argument with spaces", "'quoted'"]);
    expect(data.cwd).toBe(join(f.root, "workspace"));
    expect(data.secret).toBe(false);
    expect(data.registry).toBe(false);
    expect(data.host_socket).toBe(false);
    expect(data.host_loopback).toBe(false);
    expect(data.abstract_socket).toBe(false);
    expect(data.api_socket).toBe(true);
    expect(data.proc_pid).toBeLessThan(64);
    expect(data.usr_readonly).toBe(true);
    expect(data.etc_readonly).toBe(true);
    expect(data.policy_modified).toBe(false);
    expect(await readFile(policy, "utf8")).toBe("immutable policy");
    expect(await readFile(join(f.sdkHome, "servers.json"), "utf8")).toBe("host-only registry");
    expect(await readFile(join(f.root, "jcode-home", "servers.json"), "utf8")).toBe("sandbox pid registry");
    expect(data.home_secret).toBe(false);
    expect(data.env.JCODE_HOME).toBe(join(f.root, "jcode-home"));
    expect(data.env.JCODE_API_SOCKET).toBe(apiSocket);
    expect(data.env.JCODE_ACCEPTANCE_API_KEY).toBe("explicit 'synthetic' key");
    expect(data.env.JCODE_SWARM_MODEL).toBe("worker_acceptance:synthetic");
    for (const key of ["MALICIOUS_SECRET", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]) expect(data.env[key]).toBeUndefined();
    expect(data.env.PATH).not.toContain(secretDir);
  } finally {
    hostServer.close();
    hostLoopback.close();
    abstractServer.close();
    apiServer?.close();
    await rm(secretDir, { recursive: true, force: true });
    await f.cleanup();
  }
});

test.skipIf(!sandboxAvailable)("coordinator can copy its workspace without modifying protected paths", async () => {
  const f = await fixture();
  try {
    const workspace = join(f.root, "workspace");
    await mkdir(workspace);
    await writeFile(join(workspace, "input.txt"), "original");
    await writeFile(join(workspace, ".bashrc"), "existing fixture config");
    const { binary } = await prepareBlindRuntime(f.options);
    const code = `import json, pathlib, shutil, tempfile\n` +
      `shutil.copytree(pathlib.Path.cwd(), pathlib.Path(tempfile.mkdtemp()) / 'copy')\n` +
      `def blocked(path):\n try:\n  pathlib.Path(path).write_text('tampered'); return False\n except OSError:\n  return True\n` +
      `print(json.dumps([blocked('.bashrc'), blocked('.gitconfig'), blocked('.claude/agents/agent.md'), pathlib.Path('input.txt').read_text()]))`;
    const result = await invoke(binary, ["-c", code]);
    expect(result.status).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual([true, true, true, "original"]);
    expect(await readFile(join(workspace, ".bashrc"), "utf8")).toBe("existing fixture config");
  } finally { await f.cleanup(); }
}, 15_000);

test.skipIf(!sandboxAvailable)("acceptance runs with workspace only and no credentials", async () => {
  const f = await fixture();
  try {
    await prepareBlindRuntime(f.options);
    await writeFile(join(f.root, "jcode-home", "secret"), "synthetic credential");
    await writeFile(join(f.sdkHome, "servers.json"), "synthetic registry");
    const workspace = join(f.root, "workspace");
    const code = `import json, os\nopen('result.txt','w').write('ok')\nprint(json.dumps({'env':dict(os.environ), 'cwd':os.getcwd(), 'home_secret':os.path.exists(${JSON.stringify(join(f.root, "jcode-home", "secret"))}), 'registry':os.path.exists(${JSON.stringify(join(f.sdkHome, "servers.json"))})}))`;
    const dirty = { JCODE_ACCEPTANCE_API_KEY: "synthetic-credential", PYTHONPATH: f.sdkHome, LD_PRELOAD: join(f.sdkHome, "fake.so"), SSH_AUTH_SOCK: join(f.sdkHome, "sock") };
    const previous = Object.fromEntries(Object.keys(dirty).map((key) => [key, process.env[key]]));
    Object.assign(process.env, dirty);
    try {
      const result = await runBlindCommand({ workspace, argv: [python, "-c", code], timeoutMs: 10_000 });
      expect(result.exitCode).toBe(0);
      expect(result.timedOut).toBe(false);
      const output = JSON.parse(result.stdout);
      expect(output.cwd).toBe(workspace);
      expect(output.home_secret).toBe(false);
      expect(output.registry).toBe(false);
      for (const key of Object.keys(dirty).filter(key => key !== "PYTHONPATH")) expect(output.env[key]).toBeUndefined();
      expect(output.env.PYTHONPATH).not.toContain(f.sdkHome);
      expect(output.env.PYTHONPATH.split(":").at(-1)).toBe(workspace);
      expect(output.env.PYTHONSAFEPATH).toBe("1");
      expect(output.env.PYTHONDONTWRITEBYTECODE).toBe("1");
      expect(await readFile(join(workspace, "result.txt"), "utf8")).toBe("ok");
    } finally {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
  } finally { await f.cleanup(); }
});

test.skipIf(!sandboxAvailable)("acceptance copies a read-only snapshot into private writable tmpfs", async () => {
  const f = await fixture();
  try {
    const workspace = join(f.root, "workspace");
    await mkdir(workspace);
    await writeFile(join(workspace, "input.txt"), "original");
    const code = `import json, os, pathlib, shutil, tempfile\n` +
      `source = pathlib.Path(${JSON.stringify(join(workspace, "input.txt"))})\n` +
      `try:\n source.write_text('host change'); denied = False\nexcept OSError:\n denied = True\n` +
      `pathlib.Path('input.txt').write_text('sandbox change')\n` +
      `shutil.copytree(pathlib.Path.cwd(), pathlib.Path(tempfile.mkdtemp()) / 'copy')\n` +
      `print(json.dumps({'denied': denied, 'cwd': os.getcwd(), 'input': pathlib.Path('input.txt').read_text()}))`;
    const result = await runBlindCommand({ workspace, argv: [python, "-c", code], timeoutMs: 10_000, copyWorkspace: true });
    expect(result.exitCode).toBe(0);
    expect(result.timedOut).toBe(false);
    const output = JSON.parse(result.stdout);
    expect(output.denied).toBe(true);
    expect(output.cwd).not.toBe(workspace);
    expect(output.input).toBe("sandbox change");
    expect(await readFile(join(workspace, "input.txt"), "utf8")).toBe("original");
  } finally { await f.cleanup(); }
}, 15_000);

test.skipIf(!sandboxAvailable)("terminating wrapper and timeout kill sandbox descendants", async () => {
  const f = await fixture();
  try {
    const { binary } = await prepareBlindRuntime(f.options);
    const workspace = join(f.root, "workspace");
    const marker = join(workspace, "survived");
    const code = `import os, time\np=os.fork()\nif p == 0:\n os.setsid(); time.sleep(1.5); open(${JSON.stringify(marker)}, 'w').write('escaped')\nelse:\n print('ready',flush=True); time.sleep(20)`;
    const child = Bun.spawn([binary, "-c", code], { stdout: "pipe", stderr: "pipe", stdin: "ignore" });
    const reader = child.stdout.getReader();
    try {
      expect(new TextDecoder().decode((await reader.read()).value)).toContain("ready");
      child.kill("SIGKILL");
      await child.exited;
      // The fork retains stdout, so EOF confirms all namespace descendants exited.
      expect((await reader.read()).done).toBe(true);
      await expect(readFile(marker, "utf8")).rejects.toThrow();
    } finally {
      child.kill("SIGKILL");
      await child.exited;
      reader.releaseLock();
    }
    // This integration case exercises real process and timeout teardown.
    const timed = await runBlindCommand({ workspace, argv: [python, "-c", code], timeoutMs: 300 });
    expect(timed.timedOut).toBe(true);
    expect(timed.exitCode).not.toBe(0);
    await expect(readFile(marker, "utf8")).rejects.toThrow();
  } finally { await f.cleanup(); }
}, 15_000);
