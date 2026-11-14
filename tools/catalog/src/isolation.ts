import { chmodSync, mkdtempSync, mkdirSync, realpathSync, rmSync, statSync, symlinkSync } from "node:fs";
import { devNull, tmpdir } from "node:os";
import { delimiter, join } from "node:path";

const MAX_SOCKET_PATH_BYTES = 104;
const SOCKET_NAMES = ["jcode.sock", "jcode-debug.sock"];
const PROVIDER_ROUTING = /^(?:JCODE_(?:PROVIDER|MODEL|ANTHROPIC_|BEDROCK_|COPILOT_|GEMINI_|OPENAI_|OPENROUTER_)|JCODE_(?:RUNTIME_PROVIDER|ACTIVE_PROVIDER|INITIAL_PROVIDER_EXPLICIT|NAMED_PROVIDER_PROFILE))/;
const TOOL_NAMES = ["bun", "node", "just", "git", "rg", "python", "python3", "cargo", "rustc", "rustup", "cc", "c++", "make", "cmake", "ninja", "pkg-config", "rtk"] as const;

/** Prefer a short, non-inherited temp parent so nested runs can bind sockets. */
export function shortTempRoot(): string {
  if (process.platform !== "win32") {
    const generic = process.platform === "darwin" ? "/private/tmp" : "/tmp";
    try {
      if (statSync(generic).isDirectory()) return generic;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  return tmpdir();
}

function privateDirectory(path: string): void {
  mkdirSync(path, { mode: 0o700 });
  chmodSync(path, 0o700);
}

/** Run a callback with disposable configuration, runtime, and temporary paths. */
export async function withIsolatedEnv<T>(
  fn: (env: Record<string, string>) => Promise<T> | T,
): Promise<T> {
  const root = mkdtempSync(join(shortTempRoot(), "jc-"));
  try {
    chmodSync(root, 0o700);
    const home = join(root, "home");
    const config = join(root, "xdg-config");
    const cache = join(root, "xdg-cache");
    const data = join(root, "xdg-data");
    const state = join(root, "xdg-state");
    const temp = join(root, "tmp");
    const runtime = join(root, "run");
    const tools = join(root, "bin");
    for (const name of SOCKET_NAMES) {
      if (Buffer.byteLength(join(runtime, name), "utf8") >= MAX_SOCKET_PATH_BYTES) {
        throw new Error(`Disposable runtime socket path is too long: ${join(runtime, name)}`);
      }
    }
    for (const path of [home, config, cache, data, state, temp, runtime, tools]) {
      privateDirectory(path);
    }
    for (const name of TOOL_NAMES) {
      const executable = name === "bun" ? process.execPath : Bun.which(name, { PATH: process.env.PATH });
      if (executable) symlinkSync(realpathSync(executable), join(tools, name));
    }

    const inherited = process.env;
    const env: Record<string, string> = {};
    for (const [key, value] of Object.entries(inherited)) {
      const name = process.platform === "win32" ? key.toUpperCase() : key;
      if (value !== undefined && name !== "PATH" && !name.startsWith("XDG_") && name !== "JCODE_DEBUG_CONTROL" && !PROVIDER_ROUTING.test(name)) {
        env[key] = value;
      }
    }
    Object.assign(env, {
      PATH: [tools, ...(process.platform === "win32" ? [join(inherited.SystemRoot ?? "C:\\Windows", "System32"), inherited.SystemRoot ?? "C:\\Windows"] : ["/usr/local/bin", "/usr/bin", "/bin"])].join(delimiter),
      HOME: home,
      JCODE_HOME: join(home, ".jcode"),
      XDG_CONFIG_HOME: config,
      XDG_CACHE_HOME: cache,
      XDG_DATA_HOME: data,
      XDG_STATE_HOME: state,
      XDG_RUNTIME_DIR: runtime,
      JCODE_RUNTIME_DIR: runtime,
      JCODE_SOCKET: join(runtime, "jcode.sock"),
      TMPDIR: temp,
      TMP: temp,
      TEMP: temp,
      JCODE_WORKFLOW_CONFIG_ISOLATED: "1",
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_CONFIG_GLOBAL: devNull,
      GIT_AUTHOR_NAME: "jcode validation",
      GIT_AUTHOR_EMAIL: "jcode-validation@example.invalid",
      GIT_COMMITTER_NAME: "jcode validation",
      GIT_COMMITTER_EMAIL: "jcode-validation@example.invalid",
    });
    if (inherited.HOME) {
      if (inherited.CARGO_HOME === undefined) env.CARGO_HOME = join(inherited.HOME, ".cargo");
      if (inherited.RUSTUP_HOME === undefined) env.RUSTUP_HOME = join(inherited.HOME, ".rustup");
    }
    return await fn(env);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}
