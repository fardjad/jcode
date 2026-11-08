# Worker evaluation fixtures

These pinned, licensed codebases back the real-code scenarios in
[`scenarios.md`](scenarios.md); each scenario keeps its fixture in the
adjacent `scenarios/<id>/fixtures/` directory. The runner copies a fixture
into a disposable workspace and never intentionally edits the original. Each
run verifies the original hash.

## Bundled fixtures

- `scenarios/refactorbench-flask-debug-flag/fixtures/`: Flask `get_debug_flag`
  from [Microsoft RefactorBench](https://github.com/microsoft/RefactorBench).
  Acceptance:
  `python3 refactorbench_verifiers/add-log-parameter-get-debug-flag-test.py`.
  BSD-3-Clause (Pallets/Flask) and MIT (RefactorBench); see `LICENSE.txt`.
- `scenarios/polyglot-rest-api-fixer/fixtures/`: the `rest-api` Python exercise
  from the [Aider Polyglot Benchmark][polyglot].
  Acceptance: `python3 -m unittest rest_api_test.py`. MIT (Exercism); see
  `LICENSE`.
- `scenarios/polyglot-dot-dsl-rust-fixer/fixtures/`: the `dot-dsl` Rust exercise
  from the [Aider Polyglot Benchmark][polyglot].
  Acceptance: `cargo test -- --include-ignored` (all nine tests). MIT
  (Exercism); see `LICENSE`.

The Flask fixture combines the package under `src/flask/`, its tests, and fast
AST verifiers under `refactorbench_verifiers/`. The REST fixture uses only the
Python standard library. Rust acceptance uses an external `CARGO_TARGET_DIR` so
build artifacts do not pollute the fixture. Evaluation processes also set
`PYTHONDONTWRITEBYTECODE=1`.

For the scenario format, see the
[evaluator guide](../../../tools/evaluator/README.md#scenario-format).

## Controls and updates

Before trusting a new or changed fixture/verifier, run acceptance from a
materialized workspace and establish both controls:

- the untouched baseline fails for the intended reason;
- a known-good solution passes, while a no-op cannot pass.

Keep provenance and license files with their fixture. Prefer local unit or AST
checks that need no network or containers. See [AGENTS.md](../../../AGENTS.md)
for evaluation principles.

[polyglot]: https://github.com/Aider-AI/polyglot-benchmark
