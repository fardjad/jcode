# Worker evaluation fixtures

These pinned, licensed codebases back the real-code scenarios in
[`../scenarios/`](../scenarios/README.md). The runner copies a fixture into a
disposable workspace and never intentionally edits the original. Each run
verifies the original hash and records a complete final diff, including
generated files.

## Bundled fixtures

| Fixture | Provenance | Acceptance | License |
| --- | --- | --- | --- |
| `refactorbench-flask/` | [Microsoft RefactorBench](https://github.com/microsoft/RefactorBench), Flask `get_debug_flag` task | `python3 refactorbench_verifiers/add-log-parameter-get-debug-flag-test.py` | BSD-3-Clause (Pallets/Flask) and MIT (RefactorBench); see `LICENSE.txt` |
| `polyglot-rest-api/` | [Aider Polyglot Benchmark](https://github.com/Aider-AI/polyglot-benchmark), `python/exercises/practice/rest-api` | `python3 -m unittest rest_api_test.py` | MIT (Exercism); see `LICENSE` |
| `polyglot-dot-dsl/` | [Aider Polyglot Benchmark](https://github.com/Aider-AI/polyglot-benchmark), `rust/exercises/practice/dot-dsl` | `cargo test -- --include-ignored` (all nine tests) | MIT (Exercism); see `LICENSE` |

The Flask fixture combines the package under `src/flask/`, its tests, and fast
AST verifiers under `refactorbench_verifiers/`. The REST fixture uses only the
Python standard library. Rust acceptance uses an external `CARGO_TARGET_DIR` so
build artifacts do not pollute the fixture diff. Evaluation processes also set
`PYTHONDONTWRITEBYTECODE=1`; any remaining generated files are recorded rather
than silently excluded.

## Scenario references

Scenarios reference fixtures with relative paths, for example:

```yaml
id: polyglot-rest-api-fixer
fixture: ../fixtures/polyglot-rest-api
prompt: >-
  In the current workspace, implement RestAPI according to README.md so the
  unit tests pass completely.
accept: [python3, -m, unittest, rest_api_test.py]
metrics: [tool_health]
judge:
  criteria: [Define good controllable coordination and worker behavior here.]
simulated_user:
  max_turns: 8
  instructions: Approve reasonable plans and encourage independent decisions.
```

Prompts describe outcomes, not required worker/tool sequences. See
[`../scenarios/README.md`](../scenarios/README.md) for the complete unversioned
schema. Executable acceptance is the sole correctness gate; delegation,
instruction following, and tool health are reviewed separately from native
evidence.

## Controls and updates

Before trusting a new or changed fixture/verifier, run acceptance from a
materialized workspace and establish both controls:

- the untouched baseline fails for the intended reason;
- a known-good solution passes, while a no-op cannot pass.

Do not weaken tests to obtain a pass or copy historical verdicts into new runs.
Keep provenance and license files with their fixture. Prefer local unit or AST
checks that need no network or containers. New fixtures and scenarios do not
require worker registry changes: only `coordinator` is reserved, and worker
behavior is judged against whatever blueprint snapshot is actually used.
