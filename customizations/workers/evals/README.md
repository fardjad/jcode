# Worker evaluation fixtures

The default collection contains seven live scenarios. Live runs require separate
explicit approval and can incur provider charges. The Bun evaluator uses the
pinned upstream TypeScript SDK. Setup, offline settings validation, and run
commands are in the [evaluator guide](../../../tools/evaluator/README.md).

The `polyglot-rest-api-fixer` fixture is the `rest-api` Python exercise from the
[Aider Polyglot Benchmark](https://github.com/Aider-AI/polyglot-benchmark),
licensed MIT by Exercism. Its acceptance command is
`python3 -m unittest rest_api_test.py`. The evaluator copies the fixture into a
disposable workspace and verifies that the original stays unchanged.

Six small, original fixtures cover distinct opportunities for coordination:

- `extraction` aggregates facts from eight small files.
- `blocker-assistance` repairs a parser after a bounded visible diagnostic.
- `nested-handback` repairs two stages with separate diagnostic reveals.
- `retry-budget` has a transient, genuinely failing service probe.
- `output-guard` emits a long lookup diagnostic and produces a short summary.
- `recovery` resumes a batch after a real tool process interruption at a
  checkpoint. It does not simulate a native worker crash or dispatcher restart.

The check scripts emit diagnostics directly. A diagnostic does not prove that
assistance occurred. The output guard limit affects copies of already-guarded
worker policies. Native session and tool-call evidence is needed to establish
whether a guard or delegation ran. Unobserved behavior remains unknown. Fixture
probe state uses the disposable workspace HOME and is not an acceptance input.

After each coordinator turn, acceptance runs in a separate blind sandbox on a
snapshot of the workspace. Verifier files explicitly named in the acceptance
argv are restored from the original fixture. A failed check triggers a fixed
continuation prompt if turns remain; a passing check stops the loop. Execution
or acceptance errors do not resume it. Live runs require Linux and the pinned
sandbox-runtime dependencies. Coordinator networking is limited to the
configured provider host; acceptance has no network allowlist. See the
[evaluator guide](../../../tools/evaluator/README.md) for isolation, scenario
format, and execution.

Before trusting a new fixture, check that its untouched baseline fails and a
known-good solution passes its independent acceptance command. Keep provenance
and licenses with external fixtures.
