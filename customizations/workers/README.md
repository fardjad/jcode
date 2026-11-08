# Worker blueprints

The shipped blueprints are `coordinator`, `routine`, `advanced`, and `expert`.
They define a planning coordinator and capability-based worker roles; their
purpose is described in the
[efficiency rationale](../../docs/llm-efficiency-rationale.md).

## Install

Follow the [get started guide](../../docs/get-started.md) to install the worker
blueprints with the binary, plugins, and skills.

## Configure

The shipped workers do not pin models. Choose models in your local config:

```toml
[agents.worker_blueprints.routine]
model = "openrouter-eu:openai/gpt-6-luna"
effort = "xhigh"

[agents.worker_blueprints.advanced]
model = "openrouter-eu:openai/gpt-6.1-terra"
effort = "medium"

[agents.worker_blueprints.expert]
model = "openrouter-eu:anthropic/claude-opus-5-5"
effort = "medium"
```

See [worker delegation features](../../docs/swarm-delegation-features.md) for
front matter, structured contracts, tool policy, overrides, and session modes.
For evaluations, see the [evaluator guide](../../tools/evaluator/README.md).
