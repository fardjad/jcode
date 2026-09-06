---
on: workflow_dispatch
engine:
  id: jcode
imports:
  - shared/jcode.md
model: gpt-5.6-terra
network:
  allowed:
    - defaults
    - eu.openrouter.ai
permissions:
  contents: read
max-turns: 10
safe-outputs:
  noop:
    report-as-issue: false
---
Say hello world and nothing else. After responding, call the `noop` tool
by running this bash command:

  printf '{"message":"Hello world test completed"}' | safeoutputs noop .
