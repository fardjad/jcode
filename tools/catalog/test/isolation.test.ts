import { describe, expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { once } from "node:events";
import { createConnection, createServer } from "node:net";
import { constants } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { gitExecutable, shortTempRoot, withIsolatedEnv } from "../src/isolation";
import { git } from "../src/worktree";

const wrapper = fileURLToPath(new URL("../../scripts/isolate_config.ts", import.meta.url));

async function withInheritedEnv<T>(
  changes: Record<string, string | undefined>,
  fn: () => Promise<T>,
): Promise<T> {
  const previous = Object.fromEntries(Object.keys(changes).map((key) => [key, process.env[key]]));
  try {
    for (const [key, value] of Object.entries(changes)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    return await fn();
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

async function runWrapper(args: string[], env: Record<string, string | undefined> = process.env) {
  const child = Bun.spawn([process.execPath, wrapper, ...args], {
    env,
    stdout: "pipe",
    stderr: "pipe",
  });
  const [stdout, stderr, status] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ]);
  return { stdout, stderr, status };
}

async function bindAndConnect(path: string): Promise<void> {
  const server = createServer((socket) => socket.end("isolated"));
  server.listen(path);
  await once(server, "listening");
  try {
    const client = createConnection(path);
    let received = "";
    client.setEncoding("utf8");
    client.on("data", (chunk) => { received += chunk; });
    await once(client, "end");
    expect(received).toBe("isolated");
  } finally {
    const closed = once(server, "close");
    server.close();
    await closed;
  }
}

describe("disposable environment", () => {
  test("does not inherit personal PATH or extra XDG routing", async () => {
    const personalPath = join(shortTempRoot(), "private-tool-path");
    await withInheritedEnv({ PATH: personalPath, XDG_CONFIG_DIRS: "/private/config", XDG_DATA_DIRS: "/private/data" }, async () => {
      await withIsolatedEnv((env) => {
        expect(env.PATH.split(process.platform === "win32" ? ";" : ":")).not.toContain(personalPath);
        expect(env.XDG_CONFIG_DIRS).toBeUndefined();
        expect(env.XDG_DATA_DIRS).toBeUndefined();
      });
    });
  });

  test("discovers and runs Git from the isolated tool directory", async () => {
    const fixture = mkdtempSync(join(shortTempRoot(), "jc-git-"));
    try {
      const repo = join(fixture, "repo");
      const checkout = join(fixture, "checkout");
      mkdirSync(repo);
      const patch = join(fixture, "change.patch");
      writeFileSync(patch, [
        "From 0123456789012345678901234567890123456789 Mon Sep 17 00:00:00 2001",
        "From: Test <test@example.invalid>",
        "Date: Thu, 1 Jan 1970 00:00:00 +0000",
        "Subject: [PATCH] Add file",
        "",
        "---",
        "diff --git a/example.txt b/example.txt",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/example.txt",
        "@@ -0,0 +1 @@",
        "+isolated",
        "-- ",
        "2.0.0",
        "",
      ].join("\n"));
      await withIsolatedEnv((env) => {
        const tools = join(dirname(env.HOME), "bin");
        const executable = Bun.which("git", { PATH: env.PATH })!;
        expect(dirname(executable)).toBe(tools);
        expect(gitExecutable(repo, env)).toBe(process.platform === "win32" ? realpathSync(executable) : "git");
        if (process.platform === "win32") expect(() => gitExecutable(repo, { ...env, PATH: "" })).toThrow("git not found on PATH");
        // Match the CLI: replace the Bun process environment before Git runs.
        const inherited = { ...process.env };
        for (const key of Object.keys(process.env)) delete process.env[key];
        Object.assign(process.env, env);
        try {
          const runGit = (...args: string[]) => git(repo, args);
          runGit("init", "-q");
          runGit("am", patch);
          runGit("-c", "core.autocrlf=false", "-c", "core.eol=lf", "worktree", "add", "--detach", checkout, "HEAD");
          expect(readFileSync(join(checkout, "example.txt"), "utf8")).toBe("isolated\n");

          let diagnostic = "";
          try {
            git(repo, ["rev-parse", "--git-dir", "--verify", "not-a-ref"], { env: { ...env, TEST_SECRET: "do-not-log-me" } });
          } catch (error) {
            diagnostic = String(error);
          }
          expect(diagnostic).toContain("exitCode: 128");
          expect(diagnostic).toContain("stdout:\n.git");
          expect(diagnostic).toContain("stderr:\nfatal:");
          expect(diagnostic).not.toContain("do-not-log-me");
        } finally {
          for (const key of Object.keys(process.env)) delete process.env[key];
          Object.assign(process.env, inherited);
        }
      });
    } finally {
      rmSync(fixture, { recursive: true, force: true });
    }
  }, 240_000);

  test("replaces all config, runtime and temp paths and scrubs provider routing", async () => {
    const fixture = mkdtempSync(join(shortTempRoot(), "jc-fixture-"));
    const oldHome = join(fixture, "home");
    const oldJcodeHome = join(fixture, "user-jcode");
    const oldGitConfig = join(fixture, "user-gitconfig");
    mkdirSync(oldHome);
    mkdirSync(oldJcodeHome);
    writeFileSync(join(oldJcodeHome, "config.toml"), "secret-user-configuration");
    writeFileSync(oldGitConfig, "[user]\n\tname = personal git config\n");
    const changes: Record<string, string | undefined> = {
      HOME: oldHome,
      JCODE_HOME: oldJcodeHome,
      GIT_CONFIG_GLOBAL: oldGitConfig,
      XDG_CONFIG_HOME: join(fixture, "config"),
      XDG_CACHE_HOME: join(fixture, "cache"),
      XDG_DATA_HOME: join(fixture, "data"),
      XDG_STATE_HOME: join(fixture, "state"),
      XDG_RUNTIME_DIR: join(fixture, "xdg-runtime"),
      JCODE_RUNTIME_DIR: join(fixture, "runtime"),
      JCODE_SOCKET: join(fixture, "jcode.sock"),
      JCODE_DEBUG_CONTROL: join(fixture, "debug.sock"),
      TMPDIR: join(fixture, "x".repeat(120)),
      TMP: join(fixture, "old-tmp"),
      TEMP: join(fixture, "old-temp"),
      JCODE_PROVIDER: "provider",
      JCODE_PROVIDER_PROFILE_NAME: "profile",
      JCODE_MODEL: "model",
      JCODE_MODEL_EXTRA: "model",
      JCODE_RUNTIME_PROVIDER: "provider",
      JCODE_ACTIVE_PROVIDER: "provider",
      JCODE_INITIAL_PROVIDER_EXPLICIT: "1",
      JCODE_NAMED_PROVIDER_PROFILE: "profile",
      JCODE_ANTHROPIC_API_BASE: "secret",
      JCODE_BEDROCK_PROFILE: "secret",
      JCODE_COPILOT_MODEL: "secret",
      JCODE_GEMINI_FORCE_OAUTH: "secret",
      JCODE_OPENAI_COMPAT_API_KEY_NAME: "secret",
      JCODE_OPENROUTER_PROVIDER_FEATURES: "secret",
      CARGO_HOME: undefined,
      RUSTUP_HOME: undefined,
      ISOLATION_TEST_PRESERVED: "yes",
    };
    let root = "";
    try {
      await withInheritedEnv(changes, async () => {
        const result = await withIsolatedEnv(async (env) => {
          root = dirname(env.HOME);
          for (const key of ["HOME", "JCODE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "JCODE_RUNTIME_DIR", "JCODE_SOCKET", "TMPDIR", "TMP", "TEMP"]) {
            expect(env[key]).not.toBe(changes[key]);
          }
          expect(env.JCODE_HOME).toBe(join(env.HOME, ".jcode"));
          expect(existsSync(join(env.JCODE_HOME, "config.toml"))).toBe(false);
          expect(env.JCODE_RUNTIME_DIR).toBe(env.XDG_RUNTIME_DIR);
          expect(env.JCODE_SOCKET).toBe(join(env.JCODE_RUNTIME_DIR, "jcode.sock"));
          expect(env.TMPDIR).toBe(env.TMP);
          expect(env.TMPDIR).toBe(env.TEMP);
          expect(dirname(root)).toBe(shortTempRoot());
          for (const path of [root, env.HOME, env.XDG_CONFIG_HOME, env.XDG_CACHE_HOME, env.XDG_DATA_HOME, env.XDG_STATE_HOME, env.JCODE_RUNTIME_DIR, env.TMPDIR]) {
            expect(statSync(path).isDirectory()).toBe(true);
            if (process.platform !== "win32") expect(statSync(path).mode & 0o777).toBe(0o700);
          }
          for (const key of Object.keys(changes).filter((key) => key.startsWith("JCODE_") && !["JCODE_HOME", "JCODE_RUNTIME_DIR", "JCODE_SOCKET"].includes(key))) {
            expect(env[key]).toBeUndefined();
          }
          expect(env.ISOLATION_TEST_PRESERVED).toBe("yes");
          expect(env.CARGO_HOME).toBe(join(oldHome, ".cargo"));
          expect(env.RUSTUP_HOME).toBe(join(oldHome, ".rustup"));
          expect(env.JCODE_WORKFLOW_CONFIG_ISOLATED).toBe("1");
          expect(env.GIT_CONFIG_NOSYSTEM).toBe("1");
          expect(env.GIT_CONFIG_GLOBAL).not.toBe(oldGitConfig);
          expect(existsSync(env.GIT_CONFIG_GLOBAL)).toBe(true);
          expect(readFileSync(env.GIT_CONFIG_GLOBAL, "utf8")).toBe("");
          expect(git(fixture, ["config", "--global", "--list"], { env })).toBe("");
          expect(env.GIT_AUTHOR_NAME).toBe("jcode validation");
          expect(env.GIT_AUTHOR_EMAIL).toBe("jcode-validation@example.invalid");
          expect(env.GIT_COMMITTER_NAME).toBe("jcode validation");
          expect(env.GIT_COMMITTER_EMAIL).toBe("jcode-validation@example.invalid");
          for (const name of ["jcode.sock", "jcode-debug.sock"]) {
            const path = join(env.JCODE_RUNTIME_DIR, name);
            expect(Buffer.byteLength(path, "utf8")).toBeLessThan(104);
            if (process.platform !== "win32") await bindAndConnect(path);
          }
          return "callback result";
        });
        expect(result).toBe("callback result");
        expect(existsSync(root)).toBe(false);
        expect(process.env.HOME).toBe(oldHome);
        expect(process.env.JCODE_HOME).toBe(oldJcodeHome);
        expect(process.env.JCODE_PROVIDER).toBe("provider");
        expect(existsSync(join(oldJcodeHome, "config.toml"))).toBe(true);
      });
    } finally {
      rmSync(fixture, { recursive: true, force: true });
    }
  });

  test("preserves explicit Rust toolchain homes including empty values", async () => {
    await withInheritedEnv({ HOME: "/original-home", CARGO_HOME: "/custom-cargo", RUSTUP_HOME: "" }, async () => {
      await withIsolatedEnv((env) => {
        expect(env.CARGO_HOME).toBe("/custom-cargo");
        expect(env.RUSTUP_HOME).toBe("");
        expect(env.HOME).not.toBe("/original-home");
      });
    });
  });

  test("cleans up on synchronous and asynchronous errors", async () => {
    let root = "";
    await expect(withIsolatedEnv((env) => {
      root = dirname(env.HOME);
      throw new Error("sync failure");
    })).rejects.toThrow("sync failure");
    expect(existsSync(root)).toBe(false);
    await expect(withIsolatedEnv(async (env) => {
      root = dirname(env.HOME);
      await Promise.resolve();
      throw new Error("async failure");
    })).rejects.toThrow("async failure");
    expect(existsSync(root)).toBe(false);
  });

  test("nested environments and subprocesses use separate short roots", async () => {
    let outerRoot = "";
    let innerRoot = "";
    let childRoot = "";
    await withIsolatedEnv(async (outer) => {
      outerRoot = dirname(outer.HOME);
      await withIsolatedEnv(async (inner) => {
        innerRoot = dirname(inner.HOME);
        expect(innerRoot).not.toBe(outerRoot);
        expect(dirname(innerRoot)).toBe(shortTempRoot());
        const child = await runWrapper(["--", process.execPath, "-e", "process.stdout.write(JSON.stringify({home: process.env.HOME, runtime: process.env.JCODE_RUNTIME_DIR, tmp: process.env.TMPDIR}))"], outer);
        expect(child.status).toBe(0);
        const paths = JSON.parse(child.stdout) as { home: string; runtime: string; tmp: string };
        childRoot = dirname(paths.home);
        expect(childRoot).not.toBe(outerRoot);
        expect(childRoot).not.toBe(innerRoot);
        expect(dirname(childRoot)).toBe(shortTempRoot());
        expect(dirname(paths.runtime)).toBe(childRoot);
        expect(dirname(paths.tmp)).toBe(childRoot);
        expect(Buffer.byteLength(join(paths.runtime, "jcode-debug.sock"))).toBeLessThan(104);
        expect(existsSync(childRoot)).toBe(false);
        expect(existsSync(outerRoot)).toBe(true);
        expect(existsSync(innerRoot)).toBe(true);
      });
      expect(existsSync(innerRoot)).toBe(false);
      expect(existsSync(outerRoot)).toBe(true);
    });
    expect(existsSync(outerRoot)).toBe(false);
  });
});

describe("command wrapper", () => {
  test("runs commands with or without the separator and propagates exit status", async () => {
    const fixture = mkdtempSync(join(shortTempRoot(), "jc-wrapper-"));
    try {
      const inherited = {
        ...process.env,
        HOME: join(fixture, "caller-home"),
        JCODE_HOME: join(fixture, "caller-jcode"),
        JCODE_RUNTIME_DIR: join(fixture, "runtime"),
        XDG_RUNTIME_DIR: join(fixture, "xdg-runtime"),
        JCODE_DEBUG_CONTROL: join(fixture, "debug"),
        JCODE_ACTIVE_PROVIDER: "provider",
        CARGO_HOME: join(fixture, "cargo"),
        RUSTUP_HOME: join(fixture, "rustup"),
      };
      const program = "process.stdout.write(JSON.stringify({env: process.env, homeExists: require('node:fs').existsSync(process.env.HOME)})); process.exitCode = 37";
      for (const separator of [[], ["--"]]) {
        const result = await runWrapper([...separator, process.execPath, "-e", program], inherited);
        expect(result.status).toBe(37);
        const payload = JSON.parse(result.stdout) as { env: Record<string, string>; homeExists: boolean };
        const env = payload.env;
        expect(payload.homeExists).toBe(true);
        expect(env.HOME).not.toBe(inherited.HOME);
        expect(env.JCODE_HOME).toBe(join(env.HOME, ".jcode"));
        expect(env.JCODE_HOME).not.toBe(inherited.JCODE_HOME);
        expect(env.JCODE_RUNTIME_DIR).toBe(env.XDG_RUNTIME_DIR);
        expect(env.JCODE_RUNTIME_DIR).not.toBe(inherited.JCODE_RUNTIME_DIR);
        expect(env.JCODE_SOCKET).toBe(join(env.JCODE_RUNTIME_DIR, "jcode.sock"));
        expect(env.JCODE_DEBUG_CONTROL).toBeUndefined();
        expect(env.JCODE_ACTIVE_PROVIDER).toBeUndefined();
        expect(env.CARGO_HOME).toBe(inherited.CARGO_HOME);
        expect(env.RUSTUP_HOME).toBe(inherited.RUSTUP_HOME);
        expect(existsSync(dirname(env.HOME))).toBe(false);
      }
    } finally {
      rmSync(fixture, { recursive: true, force: true });
    }
  });

  test("rejects an empty command", async () => {
    for (const args of [[], ["--"]]) {
      const result = await runWrapper(args);
      expect(result.status).toBe(2);
      expect(result.stderr).toContain("usage:");
    }
  });

  test.skipIf(process.platform === "win32")("reports a signal-terminated child", async () => {
    const result = await runWrapper(["--", process.execPath, "-e", "process.kill(process.pid, 'SIGTERM')"]);
    expect(result.status).toBe(128 + constants.signals.SIGTERM);
  });
});
