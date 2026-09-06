---
engine:
  id: jcode
  display-name: jcode
  description: jcode coding agent with headless run mode and OpenRouter EU support
  runtime-id: jcode
  experimental: true
  provider:
    name: github
  auth:
    - role: api-key
      secret: OPENROUTER_EU_API_KEY
  behaviors:
    secret-strategy: none
    supported-env-var-keys:
      - OPENROUTER_EU_API_KEY
    capabilities:
      max-turns: true
    manifest:
      files:
        - AGENTS.md
        - .jcode/skills/*/SKILL.md
        - .agents/skills/*/SKILL.md
      path-prefixes:
        - .jcode/
        - .jcode/skills/
        - .jcode/plugins/
        - .agents/skills/
    network:
      defaults:
        - host.docker.internal
        - github.com
        - raw.githubusercontent.com
        - api.github.com
        - objects.githubusercontent.com
      provider-domains:
        openrouter-eu: eu.openrouter.ai
    installation:
      package-manager: custom
      step-name: Install jcode CLI
      binary-name: jcode
      verify-command: jcode version
      verify-step-name: Verify jcode installation
      docs-url: https://github.com/fardjad/jcode
    config-file:
      path: .jcode/config.toml
      step-name: Write jcode Config
      content: |-
        [provider]
        default_provider = "openrouter-eu"
        allowed_providers = ["openrouter-eu"]
        openai_reasoning_effort = "low"
        openai_service_tier = "flex"
        cross_provider_failover = "countdown"
        preserve_reasoning_context = true
        max_retries = 8
        retry_backoff_cap_secs = 300
        stream_idle_timeout_secs = 180

        [tools]
        profile = "full"
        mcp_tools = "none"

        [hooks]
        pre_tool_transform = "~/.jcode/plugins/rtk-transform"
        pre_tool_transform_timeout_ms = 500
        pre_tool_timeout_ms = 5000
    execution:
      command-name: jcode
      args:
        - run
        - --json
        - --quiet
        - --no-update
        - --no-selfdev
        - --provider
        - openrouter-eu
      step-name: Execute jcode CLI
      model-env-var: JCODE_MODEL
      model-flag: --model
      mcp-config-env-var: GH_AW_MCP_CONFIG
      write-timestamp: true
      env:
        OPENROUTER_EU_API_KEY: ${{ secrets.OPENROUTER_EU_API_KEY }}
        JCODE_RUN_AUTO_POKE: "0"
    mcp:
      config-path: .jcode/mcp.json
    log-parser: |
      function parseLog(logContent) {
        const lines = logContent.split("\n");
        const logEntries = [];
        const mcpFailures = [];
        let maxTurnsHit = false;
        let inputTokens = 0;
        let outputTokens = 0;
        let toolCallIndex = 0;
        let turnCount = 0;
        let pendingText = [];

        function flushText() {
          if (pendingText.length === 0) return;
          const text = pendingText.join("\n").trim();
          if (text) {
            logEntries.push({ type: "assistant", message: { content: [{ type: "text", text }] } });
            turnCount++;
          }
          pendingText = [];
        }

        logEntries.push({ type: "system", subtype: "init", model: null, session_id: null });

        for (const line of lines) {
          if (!line.trim()) continue;
          if (/max.?turns|maximum.*turns.*reached|turn limit/i.test(line)) maxTurnsHit = true;
          if (/MCP server .* failed|MCP.*connection.*error|Failed to connect to MCP/i.test(line)) {
            const serverMatch = line.match(/MCP server ['"]?([^\s'"]+)['"]?/i);
            mcpFailures.push(serverMatch ? serverMatch[1] : line.trim());
          }

          let parsed = null;
          try {
            if (line.trim().startsWith("{")) parsed = JSON.parse(line.trim());
          } catch (e) { /* not JSON */ }

          if (parsed) {
            const entryType = parsed.type != null ? String(parsed.type) : "log";

            if (parsed.input_tokens) inputTokens += parsed.input_tokens;
            if (parsed.output_tokens) outputTokens += parsed.output_tokens;

            if (entryType === "done") {
              flushText();
              if (parsed.usage) {
                if (parsed.usage.input_tokens) inputTokens = parsed.usage.input_tokens;
                if (parsed.usage.output_tokens) outputTokens = parsed.usage.output_tokens;
              }
              if (parsed.text) {
                logEntries.push({ type: "assistant", message: { content: [{ type: "text", text: parsed.text }] } });
              }
            } else if (entryType === "tool_start") {
              toolCallIndex++;
            } else if (entryType === "text_delta") {
              pendingText.push(parsed.text || "");
            } else if (entryType === "start") {
              if (parsed.model) logEntries[0].model = parsed.model;
              if (parsed.session_id) logEntries[0].session_id = parsed.session_id;
            }
          } else {
            pendingText.push(line.trim());
          }
        }
        flushText();

        const usage = {};
        if (inputTokens) usage.input_tokens = inputTokens;
        if (outputTokens) usage.output_tokens = outputTokens;
        logEntries.push({ type: "result", num_turns: turnCount, usage });
        const parts = [`**Turns:** ${turnCount}`, `**Tool calls:** ${toolCallIndex}`];
        if (inputTokens || outputTokens) parts.push(`**Tokens:** ${((inputTokens ?? 0) + (outputTokens ?? 0)).toLocaleString()}`);
        if (mcpFailures.length) parts.push(`**MCP failures:** ${mcpFailures.length}`);
        if (maxTurnsHit) parts.push("**Max turns reached**");
        return { markdown: parts.join(" · "), logEntries, mcpFailures, maxTurnsHit };
      }
pre-agent-steps:
  - name: Install jcode CLI
    run: |
      VERSION="${GH_AW_ENGINE_VERSION:-v0.81.7}"
      ARCH=$(uname -m)
      case "$ARCH" in
        x86_64)  ARCH="x86_64" ;;
        aarch64) ARCH="aarch64" ;;
      esac
      curl -fsSL "https://github.com/fardjad/jcode/releases/download/${VERSION}/jcode-linux-${ARCH}.tar.gz" \
        | tar xz -C /usr/local/bin
      mv "/usr/local/bin/jcode-linux-${ARCH}" /usr/local/bin/jcode
  - name: Verify jcode installation
    run: jcode version
  - name: Install rtk
    run: |
      ARCH=$(uname -m)
      case "$ARCH" in
        x86_64)  RTK_ARCH="x86_64-unknown-linux-musl" ;;
        aarch64) RTK_ARCH="aarch64-unknown-linux-gnu" ;;
      esac
      RTK_VERSION=$(curl -fsSI https://github.com/rtk-ai/rtk/releases/latest | grep -i '^location:' | sed 's|.*/tag/||' | tr -d '\r\n')
      curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/${RTK_VERSION}/rtk-${RTK_ARCH}.tar.gz" \
        | tar xz -C /usr/local/bin rtk
  - name: Install rtk-transform hook
    run: |
      mkdir -p ~/.jcode/plugins
      cat > ~/.jcode/plugins/rtk-transform << 'HOOK'
      #!/usr/bin/env python3
      import json, os, subprocess, sys
      tool_input = json.load(sys.stdin)
      if os.environ.get("JCODE_HOOK_TOOL_NAME") != "bash":
          sys.exit(0)
      command = tool_input.get("command", "")
      if not command:
          sys.exit(0)
      result = subprocess.run(["rtk", "rewrite", command], capture_output=True, text=True, timeout=0.4)
      rewritten = result.stdout.strip()
      if rewritten and rewritten != command:
          tool_input["command"] = rewritten
          json.dump(tool_input, sys.stdout, separators=(",", ":"))
      HOOK
      chmod +x ~/.jcode/plugins/rtk-transform
---

<!--
  jcode engine definition for GitHub Agentic Workflows (gh-aw).

  This variant uses jcode's built-in OpenRouter EU provider. The base URL
  (https://eu.openrouter.ai/api/v1) is hardcoded in jcode's provider spec,
  so the user only needs to provide their OpenRouter API key.

  ## Usage

  Import this file and set `engine: id: jcode` in your workflow:

  ```yaml
  engine:
    id: jcode
  imports:
    - shared/jcode.md
  model: gpt-5.6-terra
  network:
    allowed:
      - defaults
      - eu.openrouter.ai
  ```

  ## Required secrets

  - `OPENROUTER_EU_API_KEY` — your OpenRouter API key

  ## Safe outputs

  gh-aw mounts `safeoutputs` as a CLI on PATH. The agent must call at least
  one safe-output tool before finishing. For workflows where no GitHub action
  is needed, instruct the agent to call `noop`:

  ```
  If no action is needed, run: printf '{"message":"done"}' | safeoutputs noop .
  ```

  ## MCP gateway bridge (future)

  jcode only supports stdio MCP transport. gh-aw's MCP gateway uses
  Streamable HTTP. A bridge via `supergateway` (--streamableHttp) can convert
  HTTP to stdio, but the config-adapter from imported engine definitions is
  not yet emitted by gh-aw's compiler. When this is resolved, a harness-script
  or config-adapter can write a `.jcode/mcp.json` that points each gateway
  server to a `supergateway` stdio bridge.

  ## Skills

  jcode auto-discovers `SKILL.md` files from `.jcode/skills/` and
  `.agents/skills/` in the repo root. Just commit skill directories and
  they'll be available to the agent at runtime.

  ## Hooks

  The `[hooks]` section is enabled by default. The engine's pre-agent-steps
  install rtk and the hook script automatically.
-->
