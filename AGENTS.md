# jcode patch catalog

The `personalized` branch is a catalog of patches, customizations, and tooling,
pinned to a stable upstream release, not a source checkout.

## Goals

1. An easy-to-install jcode distribution.
2. A few cohesive, intent-carrying patches cheap to carry across releases.
3. Customizations (skills, plugins, worker blueprints) centered on a
   token-efficient worker and routing setup.
4. Evaluation finds actionable issues in blueprints, plugins, patches, or,
   rarely, upstream.
5. Tooling, docs, and rules no bigger than these goals need.

## Goal-justified complexity

Rust in patches follows upstream conventions and test patterns. Everything this
catalog owns must serve a goal above, or it is not added and is removed if it
already exists. Test catalog-owned logic where a bug would cost real time; do
not test merely to restate code, a library, or a rule. Prefer one end-to-end
check over many unit tests of glue.

## Rules about rules

- Rules state principles or boundaries; mechanics live in code and `--help`.
- Activate `/unslop` when writing or editing prose.
- Procedures live in skills. One incident becomes a code check or is dropped.
- When unsure, ask the user rather than adding a rule for the uncertainty.
- Instruction edits name the defect and authoritative location and make the
  smallest fix. Approval, cost, security, or role changes need explicit consent.

## Hard boundaries

- Spell `jcode` in prose, workflows, and labels; preserve source capitalization.
- Never access the real `~/.jcode/config.toml` or its parent without express
  direction. Run validation and tests with disposable `HOME` and XDG via
  `tools/scripts/isolate_config.ts`. New workflows never inherit `JCODE_HOME`,
  XDG, personal PATH configuration, or provider routing.
- Patch intent lives only in its `.patched-jcode` source commit and derived
  patch. Never hand-edit `patches/*.patch`.
- `VERSION.txt` is the only pin. Use canonical stable tags, never `master`;
  upgrades go through `/plan-upstream-sync`.
- Learn upstream failures only on clean upstream with
  `just learn-upstream-exclusions`; never add `--skip` to patch metadata or
  learn or hide patched failures.
- Patch-workflow commits and amends inside `.patched-jcode` are authorized.
  Catalog commits, pushes, PRs, and deleting local state, including worktrees,
  plans, and sync reports, require an explicit ask. Sync planning and execution
  each require separate approval.
- Paid `just eval-workers` runs require explicit user intent, never run in
  `test-catalog`, and are not repeated to chase a clean result.
- Normal tests never compile jcode or incur provider cost.

## Patch design

- Each patch delivers one end-user feature or fix; respect dependencies and
  lexicographic order.
- Prefer small patches cheap to carry. Gate upstream features off rather than
  deleting code; gating patches pass the full suites of touched crates.
- Blueprint changes go to the blueprint patch unless there is a good reason;
  finishing faster is not a reason.
- Commit-message length tracks blast radius.
- Clean application or overlap is not adoption. Dropping a patch for upstream
  requires equivalent behavior and test evidence for its full intent.
- Fixes amend the owning commit, then regenerate the patch.

```text
feat: short imperative subject

X-Jcode-Patch-Intent: short user-visible outcome
X-Jcode-Patch-Depends-On: none
X-Jcode-Patch-File: 0001-short-imperative-subject.patch

Patch intent:
State change in one wrapped paragraph.

Why it exists:
State problem solved and intentional non-goals.

Upstream integration points:
Name upstream files, symbols, and behavior this patch relies on.

Update guidance:
State what future updates must preserve.

Validation:
cargo test -p package focused_test --lib
```

Full workflow: /patch-workflow.

## Delegation and shared state

- Give each worker one objective, allowed files, one acceptance command, and an
  attempt cap.
- Workers keep notes and never reset or discard work. Save `git diff` before
  taking over a crashed worker; leave am or rebase state alone unless told.
- Do not rebase, reset, or rebuild a tree used by a running build, test, or
  evaluation. Give each shared cache and result ledger one active runner.
- Decide trivial conflicts yourself; delegate their mechanical application.

## Workers and evaluation

- Aim for correct results at minimum total cost, latency, and context.
- Route by capability, without forced pipelines, counts, or quotas. Tiers are
  roles, not a discovery chain.
- The coordinator is planning-only and delegates all execution.
- Do not summarize source for workers; name inputs and let them inspect.
- `skill_manage` is planning support, never execution authority.
- Worker-to-child delegation is bounded read-only extraction only.
- Worker reports preserve all material facts; never impose length caps.
- Keep blueprint prompts concise, without repeating native jcode instructions.
  Drop obsolete text on role changes; ask when unsure.
- Blueprints are installed globally and used in every project; keep catalog
  paths and procedures out of them.
- Scenarios state outcomes and checks, never delegation hints.
- Evaluate blind and uncoached.
- Independent `accept` is the sole correctness gate. Judge findings are cited
  hypotheses; reproduce them before any patch or edit.
- Report only provider-reported cost; unknown stays unknown.
- Never weaken a verifier or fixture to get a pass.
- Evaluations serve workflow goals, not model benchmarking.
- Collect minimum sufficient evidence; the findings report is primary. Record
  gaps as unknown, never as a clean review.
- Inventory inputs, schemas, policies, and traces before diagnosing.
- `tools/evaluator/` is generic: only `coordinator` is reserved, workers are
  discovered per run, and worker assets live in `customizations/workers/`.
  Preserve harness client contracts; citations use native IDs.

## Tooling conventions

- Developer workflows live in `tools/catalog/src/`; `just` recipes are thin
  aliases. Discover commands with `just help` or
  `bun tools/catalog/src/cli.ts --help`.
- Asset setup uses Bun. Plugins use vanilla Python 3.14+.
- Tests use `<stem>.test.ts`, `<stem>_test.py`, or `<stem>_test.sh`; retain
  component-specific conventions.
- Keep each plugin in its own directory and document its upstream source.
- Shipped plugin and skill directories need allowlist entries in their own
  `.gitignore` files.

## Where things live

- /patch-workflow: change and validate catalog patches.
- /plan-upstream-sync: plan and execute approved upstream syncs.
- /evaluate-worker-blueprints: run and review worker evaluations.
- /author-plan: author plans for substantial work.
- `docs/`: feature and user documentation; `README.md`: catalog overview.
