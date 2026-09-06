---
engine:
  id: jcode-byok
  display-name: jcode (BYOK)
  description: jcode coding agent with headless run mode and generic OpenAI-compatible BYOK support
  runtime-id: jcode
  experimental: true
  provider:
    name: github
  auth:
    - role: api-key
      secret: JCODE_MODEL_API_KEY
  behaviors:
    secret-strategy: none
    supported-env-var-keys:
      - JCODE_MODEL_API_KEY
      - JCODE_MODEL_BASE_URL
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
        anthropic: api.anthropic.com
        openai: api.openai.com
        openrouter: openrouter.ai
        openrouter-eu: eu.openrouter.ai
        groq: api.groq.com
        mistral: api.mistral.ai
        deepseek: api.deepseek.com
        xai: api.x.ai
    installation:
      package-manager: custom
      step-name: Install jcode CLI
      binary-name: jcode
      verify-command: jcode version
      verify-step-name: Verify jcode installation
      docs-url: https://github.com/fardjad/jcode
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
    config-file:
      path: .jcode/config.toml
      step-name: Write jcode Config
      content: |-
        [provider]
        default_provider = "openai-compatible"

        [providers.awf-byok]
        base_url_env = "JCODE_MODEL_BASE_URL"
        api_key_env = "JCODE_MODEL_API_KEY"
        auth = "bearer"

        [tools]
        profile = "full"
        mcp_tools = "none"

        # Hooks: external command integrations.
        # Uncomment to enable rtk or other pre_tool_transform plugins.
        # [hooks]
        # pre_tool_transform = "~/.jcode/plugins/rtk-transform"
        # pre_tool_transform_timeout_ms = 500
        # pre_tool_timeout_ms = 5000
    execution:
      command-name: jcode
      args:
        - run
        - --json
        - --quiet
        - --no-update
        - --no-selfdev
        - --provider-profile
        - awf-byok
      step-name: Execute jcode CLI
      model-env-var: JCODE_MODEL
      mcp-config-env-var: GH_AW_MCP_CONFIG
      write-timestamp: true
      env:
        JCODE_MODEL_BASE_URL: ${{ secrets.JCODE_MODEL_BASE_URL }}
        JCODE_MODEL_API_KEY: ${{ secrets.JCODE_MODEL_API_KEY }}
    mcp:
      config-path: .jcode/config.toml
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
---

<!--
  jcode BYOK engine definition for GitHub Agentic Workflows (gh-aw).

  This variant uses jcode's openai-compatible provider with a named profile
  (awf-byok) that reads the base URL and API key from environment variables.
  Use this when you want to point jcode at an arbitrary OpenAI-compatible API
  endpoint with your own key.

  ## Usage

  Import this file and set `engine: jcode-byok` in your workflow:

  ```yaml
  engine: jcode-byok
  imports:
    - shared/jcode-byok.md
  model: anthropic/claude-sonnet-4-5
  network:
    allowed:
      - defaults
      - .anthropic.com
  ```

  ## Required secrets

  - `JCODE_MODEL_BASE_URL` — the API endpoint URL (e.g., https://api.openai.com/v1)
  - `JCODE_MODEL_API_KEY` — the API key for that endpoint

  ## Network

  You must add your provider's domain to `network.allowed` in your workflow.
  Domain matching is exact by default. Use a leading dot to match subdomains:
  - `api.openai.com` — exact match, only this host
  - `.openrouter.ai` — matches openrouter.ai and all subdomains (e.g., eu.openrouter.ai)
  - `*.openrouter.ai` — matches subdomains only (not the bare domain)

  ## Skills

  jcode auto-discovers `SKILL.md` files from `.jcode/skills/` and
  `.agents/skills/` in the repo root. Just commit skill directories and
  they'll be available to the agent at runtime.

  ## Hooks

  Uncomment the `[hooks]` section in the config-file content above to enable
  external command integrations like the rtk pre_tool_transform plugin.
-->
