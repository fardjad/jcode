# jcode patch catalog

Always spell the project name `jcode` in human-facing prose, workflow names,
and labels. Preserve existing capitalization in code identifiers, environment
variables, protocol values, patch metadata headers, and upstream source text.

## Configuration isolation

Never access, read, write, or probe the user's real `~/.jcode/config.toml` (or
its parent directory) from repository work, tests, validation commands, or
agent workflows unless the user expressly directs that specific access. Every
validation or test command must use disposable `HOME` and
`XDG_CONFIG_HOME` values, and should use the repository's
`tools/scripts/isolate_config.py` wrapper, which also provides disposable
`JCODE_HOME` and other XDG directories. `just` validation runs declared patch
commands and compatibility tests through this isolation, and the
patched-workspace compile check is isolated as well. Do not weaken this
boundary by inheriting a user's `JCODE_HOME` or XDG settings in new workflow
commands. The wrapper may preserve or derive the standard `CARGO_HOME` and
`RUSTUP_HOME` locations so Rust toolchains and caches remain usable, but must
not preserve `JCODE_HOME` or any XDG path from the caller. This safeguard does
not authorize access to any personal path.

`personalized` is an orphan catalog branch. It contains catalog content,
including documentation, installers, workflow scripts, `patches/`,
`customizations/plugins/`, `customizations/workers/`, and project-local skills
under `.jcode/skills/`, not an upstream source checkout. `VERSION.txt` pins a
stable upstream release; ordered mail patches describe changes without
requiring original source commits.

Validator discovers every `patches/*.patch`, checks intent, kind, dependency,
and body-section metadata, then applies all patches in lexicographic order with
`git am --3way` to a disposable worktree based on the release pinned by
`VERSION.txt`.

Commands:

```text
just help
just test-catalog               # run catalog script and plugin tests
just eval-workers [scenario]    # paid live blueprint evaluation; never in test-catalog
just validate-patch-files
just create-patched-copy
just snapshot-patches            # regenerate patches and normalize personal hashes
just snapshot-patches --prune    # also delete patches no commit claims
just list-patches                # print commit-to-patch mapping
just install-patched-version
just test-patch-file <patch-file>
just check-patch-isolation       # each patch applies with only its dependencies
just test-all-patches            # resumable test-patch-file for every patch
just create-upstream-candidate-branch-from <patch-file>
just set-version vX.Y.Z [reviewed-hash]   # pin a canonical stable release
just learn-upstream-exclusions
just validate-patched-copy
```

`just create-patched-copy` creates or resets persistent `.patched-jcode` from
the stable release pinned in `VERSION.txt`, fetching its tag from canonical
jcode when needed. Patch validation, patch tests, and candidate workflows use
the same pinned release. Use `just test-patch-file <patch-file>` for
declared validation commands and compatibility tests. Failures retain worktrees
and print cleanup commands. `just test-all-patches` runs it for every patch in
the background, keeps logs under `.tools/patch-tests/<base>/`, and skips
unchanged patches that already passed.

`just eval-workers [scenario]` runs paid live real-code coordinator/worker,
simulated-user, and judge calls outside normal catalog tests. Its build gate uses the exact pinned
base plus every current patch and never resets or implicitly uses
`.patched-jcode`. Follow `customizations/workers/AGENTS.md` and
`tools/evaluator/AGENTS.md`, and
use the project-local `/evaluate-worker-blueprints` skill for run/review
procedures.

Tests that already fail on clean upstream are learned, never hand-coded. `just
learn-upstream-exclusions` records them for the compatibility suite and for
every patch's `cargo test` Validation commands, and later runs skip them. Never
add a `--skip` to patch metadata for an upstream failure. Run the learning step
once per new pin before testing patches.

Compatibility failures are learned only from clean upstream worktrees. Ignored
state lives at `.tools/nextest/exclusions/<base-commit>.json`, keyed by base
commit and pinned nextest version. Patched failures are not learned or hidden:
they remain test failures. Delete that directory to reset learned state.

Compatibility tests use the cargo-nextest version declared in
`tools/catalog/src/jcode_catalog/bootstrap_nextest.py`, bootstrapped on demand
and cached under ignored `.tools/nextest/<version>/<target>/`.
JUnit reports live at `target/nextest/compat/junit.xml` inside each materialized
worktree. Learned exclusions are sorted exact `test(=...)` filters and apply
only when base commit and nextest pin match.

`just set-version vX.Y.Z` fetches that stable release tag from the canonical
jcode repository, refuses the change when an optional reviewed commit hash does
not match, and writes `VERSION.txt`. It requires a clean `personalized`
checkout and does not validate patches, rebase, or push. Every other recipe
then works on the new release. The pin and patch changes are committed
together; until then `git checkout -- VERSION.txt` restores the previous pin.

`VERSION.txt` is the only version pin. The moving upstream `master` branch is
never used, catalog workflows do not use a `master` branch, and upgrades go
through `/plan-upstream-sync`. GitHub's default branch is `personalized`.

Patch application order is lexicographic. The metadata format is described in
"Commit message format" below.

## Upstream syncs

Every upstream sync goes through the project-local `/plan-upstream-sync` skill,
which holds the full procedure: preflight, the four temporary reports under
`/sync-scratch/`, the decision table, execution steps, and recovery. This is an
agent workflow requirement, not a check implemented by any recipe. The rules
below apply to every sync and are not repeated in full there.

- Review first. Planning and execution each need separate explicit user
  approval. Answering one decision approves neither the other decisions nor
  execution. Planning must not edit patches or source, reset `.patched-jcode`,
  or update the release pin.
- Start from a clean, current `personalized` catalog. Never stash, discard, or
  commit automatically to make it clean. Stop on divergence from the tracking
  branch.
- Use `VERSION.txt`, not the running binary, as the baseline, and only stable
  tags from canonical jcode as targets.
- Similar wording, file overlap, or a patch applying cleanly is not adoption.
  Removing a patch because upstream covers it requires equivalent behavior and
  test evidence for its full intent, with residual differences made explicit.
  Drop only the parts upstream covers when the rest still has value.
- Sync reports are disposable working material, never a second source of patch
  intent. Delete them only with user permission after a validated sync.

## Patch design

Patch intent has one source of truth: the source commit message in
`.patched-jcode` and the catalog patch derived from it. Docs, reports, and
notes reference that intent and never redefine it.

Each patch must deliver one end-user feature or fix. A coherent series of
changes supporting workers with blueprints may belong together; unrelated
session deletion with Ctrl-D must not be mixed into it. Removals, splits,
consolidations, and ordering must respect declared dependencies and the
lexicographic application order.

Prefer the smallest patch that is cheap to carry across releases. To hide an
upstream feature, gate it off rather than deleting its code, and keep upstream
tests compiling. A gating patch must pass the full test suites of every crate it
touches, compared with clean upstream, because gating breaks upstream tests of
the gated surface that a focused Validation command would never run.

## Working in .patched-jcode (commit-first workflow)

Never edit `.patch` files directly. Patches are persistence artifacts derived
from commits. The commit message in `.patched-jcode` is the single source of
truth for both the diff and the `X-Jcode-Patch-*` metadata.

`.patched-jcode` is a disposable working copy, kept only to reuse build caches.
Its commits are not protected: `just create-patched-copy`,
`just validate-patched-copy`, and `just install-patched-version`
reset a clean copy to the pinned release and reapply the catalog. Run
`just snapshot-patches` before any of these whenever `.patched-jcode` has
commits not yet persisted as patches. The reset prints the previous HEAD so an
accidental reset can be recovered.

The ideal workflow for any change:

1. `just create-patched-copy` ensures `.patched-jcode` exists, is clean, and
   matches the current catalog.
2. Edit source files directly in `.patched-jcode/`.
3. Commit or amend the relevant commit there (see below for which commit).
4. Review intent vs code vs tests (see "Patch review" below).
5. `just snapshot-patches` regenerates every `.patch` file from the commits
   above the release pinned by `VERSION.txt` and normalizes every personal
   patch's `From` hash to zeros.
   Run `just validate-patch-files` to confirm.
6. `just test-patch-file patches/<name>.patch` to run the patch's validation.

### Mapping commits to patches

Each commit names its own patch file with an `X-Jcode-Patch-File` header, and
`just snapshot-patches` writes that file. Mapping never depends on position.
Run `just list-patches` to see the mapping and any stale files. To change an
existing patch, amend the corresponding commit.

Snapshot validates every commit before writing anything, then writes files
atomically. It rejects a run when a header is missing, names repeat, filenames
do not increase in commit order, a `Depends-On` entry does not name an earlier
commit's file, or the number does not match the kind. Nothing in `patches/`
changes on rejection.

- **Add:** commit on top. If the header is missing, snapshot stops and suggests
  the next free name; add it to the message and rerun.
- **Remove:** drop the commit, then run `just snapshot-patches --prune`. Without
  `--prune`, files no commit claims are reported and kept.
- **Reorder or split:** reorder or add commits, then renumber their
  `X-Jcode-Patch-File` headers so they increase in the new order. Update any
  `X-Jcode-Patch-Depends-On` entries that name renumbered files. Use
  `--prune` when old filenames go away.

### Patch review

Before running `just snapshot-patches`, review every patch the change
touched. The review compares the full stated intent (commit message body
sections and `X-Jcode-Patch-*` headers) against the actual code diff and
the named validation tests. The goal is to catch gaps where the code or
tests are narrower than the intent claims, or where the code is broader
than the intent describes.

For each patch:

1. Read the commit message (subject, headers, and all body sections).
   Understand the complete scope of what the patch claims to do.

2. Read the actual diff. Note every behavioral change the code makes,
   including ones the prose does not explicitly call out. The code is the
   ground truth for what the patch does.

3. Read the `Validation:` section and find the actual test functions it
   names. Read those tests.

4. Compare: do the code and tests together prove the general behavior the
   intent describes, or are they narrowly scoped to the specific bug
   scenario that motivated the patch? For example:

   - If the code adds a policy check but only for one provider path, does
     it also cover the other paths the intent names (dispatch, failover,
     sidecar, model switching)?
   - If the intent says "all session sources," do the tests cover external
     transcripts and missing files, or just one local fixture?
   - If the code hardcodes a check for one provider when the policy should
     apply to all, flag that as a bug even if tests pass.

5. For any gap where the code or tests are narrower than the intent, fix
   it before snapshotting: add a test that expresses the general
   invariant, broaden the code, or update the intent prose to match what
   the code actually does.

6. Also check for the reverse: code changes that are broader than the
   stated intent. These may be intentional but the intent should be
   updated so future maintainers know the broader scope is deliberate.

When fixing a gap, follow the same commit-first workflow: edit in
`.patched-jcode`, amend the corresponding commit, add the new test name to
the `Validation:` section, then snapshot.

### Commit message format

Every patch commit message must contain the four `X-Jcode-Patch-*` headers and
the five body sections. `git format-patch` turns the commit message into the
patch file, so the message is the patch. Use this format:

```text
feat: short imperative subject

X-Jcode-Patch-Intent: short user-visible outcome
X-Jcode-Patch-Kind: personal-feature
X-Jcode-Patch-Depends-On: none
X-Jcode-Patch-File: 1016-personal-feature-short-imperative-subject.patch

Patch intent:
State change in one wrapped paragraph.

Why it exists:
State problem solved and intentional non-goals.

Upstream integration points:
Name upstream files, symbols, and behavior this patch relies on.

Update guidance:
State what future updates must preserve.

Validation:
scripts/cargo_exec.sh test -p package focused_test --lib
```

Wrap every prose line at 80 columns or fewer. Do not wrap validation commands:
each command is one unindented line under `Validation:`. Each patch's
Validation commands must run at least one test and should cover the patch's
full intent. The `X-Jcode-Patch-*`
headers go in the commit message body, after the subject and its blank line.
Dependencies must be declared with `X-Jcode-Patch-Depends-On` as `none` or
comma-separated earlier patch names. Required metadata headers are
`X-Jcode-Patch-Intent`, `X-Jcode-Patch-Kind`, `X-Jcode-Patch-Depends-On`, and
`X-Jcode-Patch-File`, whose value must equal the patch's own filename.
Required ordered body sections are `Patch intent:`,
`Why it exists:`, `Upstream integration points:`, `Update guidance:`, and
`Validation:`.

### Personal vs candidate patches

Personal patches use `X-Jcode-Patch-Kind: personal-*` and a `1000`-series numeric
prefix. `just snapshot-patches` always normalizes their `From` commit hash to
zeros for reproducibility. Candidate patches use `X-Jcode-Patch-Kind:
upstream-candidate` and retain their real hash.
They use a `0000`-series prefix. Keep candidate patches focused and independent
from personal patches.

## Patch update and commit discipline

When a change fixes behavior owned by an existing catalog patch, make the fix in
`.patched-jcode`, amend the corresponding commit, then run
`just snapshot-patches` to regenerate the patch. Never leave source-only fixes
in `.patched-jcode` while its catalog patch is stale.

Commits and amends inside `.patched-jcode` are authorized as part of the
commit-first patch workflow: create or amend the source commit, then regenerate
and validate the catalog patch. No separate user confirmation is needed for
those `.patched-jcode` commits when they are required to keep the source and
patch catalog in sync.

Do not commit or amend in the catalog git repository (`personalized`), push, or
create a PR unless the user explicitly asks. Leave updated catalog files and
generated worktree changes uncommitted for user review. Do not create temporary
commits merely to regenerate a patch.

When applying a stack before full testing, preserve follow-up fixes with
explicit owning-patch attribution until the corresponding source commits can
be amended. Never temporarily commit an earlier feature's correction into a
later patch. Preserve recovery refs or bundles before rewriting the stack,
and verify exact final tree equality after ownership repair. Do not reset a
copy with unpersisted commits or edits to simplify bookkeeping.

## Delegating patch work

These rules apply whenever workers edit `.patched-jcode`, during a sync or not.

- Give each worker one objective, the allowed files, one acceptance command,
  and an attempt cap. Open-ended tasks such as "adapt this patch" stall.
- Workers keep progress notes in a scratch file, never reset or discard work,
  and leave any `git am` or rebase state alone unless told otherwise. Save
  `git diff` before taking over a crashed worker's tree.
- Do not rebase or reset `.patched-jcode` while a background build or test run
  uses it.
- Resolve trivial conflicts directly, such as both sides adding tests or
  methods, or a test moved into a split module.

## Worker blueprint boundaries

The blueprints under `customizations/workers/blueprints/` are reusable harness
roles, used in other projects as well as this catalog. Keep catalog paths,
patch ownership, release pins, sync procedures, isolation commands, and
installer recipes out of those blueprints. Supply this repository's context
through project instructions and scoped assignments. Runtime tool guidance
and transferable execution discipline belong in blueprints, not
catalog-specific procedures.

Before changing workers or delegation rules, check the blueprints, their
README, tool permissions, and these project instructions for contradictions.
Fix the authoritative instruction that is wrong. Do not add generic
"be project-independent" boilerplate, conflicting exceptions, or workarounds
that bypass permissions. Preserve established role responsibilities unless
the user explicitly approves changing them. Static contract tests check known
regressions, not whether a model will follow the instructions in practice.

## Validation execution discipline

Give each result ledger and shared mutable build cache one active runner.
Do not overlap per-patch runners or modify their inputs during validation.
Before rerunning after a timeout, inspect the original process, task state,
and log. A monitoring timeout is not command failure or proof of termination.
Long runs need persistent logs, process/task identity, and an explicit exit
status that can be inspected independently of the monitoring call.

Do not accept compiler diagnostics that contradict the actual worktree as a
source defect without checking dependency resolution and cached artifacts.
After affected builds stop, invalidate only the implicated disposable cache
entries and rerun the failing check. Never widen patch dependencies merely to
mask stale artifacts, hide patched failures, or accept unexplained retries.

Final individual-test acceptance must match the current patch content hashes
and pinned base, including candidate hashes changed by regeneration. A prior
pass for an older patch is not current evidence. Verify regenerated patches
reproduce the intended source tree exactly, and map all required checks to
observed outcomes before reporting the sync validated. The resumable runner
now fingerprints dependency closure, validation tooling, exclusions, and Rust
version. Run this no-build current-result audit:

```text
uv run --project tools/catalog jcode-catalog patches test --all --audit
```

Legacy records lack the stronger fingerprint and are not upgraded to passes
automatically.
Compiler caches are keyed by clean source tree under `.tools/cargo-target/`.
Different trees cost additional disk space and cold compilation, but cannot
reuse one another's mutable compiler metadata. Preserve the documented JUnit
path under each worktree. Runner locks do not authorize resetting a worktree
or bypass manual coordination with other source users.

## Review evidence and instruction changes

Before reporting a sync complete, record a final intent/code/test review for
all retained patches under `sync-scratch/reviews/`. Use
`uv run --project tools/catalog jcode-catalog patches reviews init`
to create pending templates, then fill them from actual review. Never mark a
record reviewed solely because tests passed. Each claim needs code locations,
actual test functions, findings, and a supported outcome or explicitly
user-accepted gap. Include full-scope coverage, broader changes, and residuals.
The source commit, patch and dependency hashes, and base identify the reviewed
version. After changes, re-review stale records. Run the script's `check` action
before completion. It checks freshness and record structure, not truth or
semantic completeness, and does not redefine patch intent.

For instruction edits, name the concrete defect and authoritative location,
make the smallest correction, and state whether role responsibilities or tool
permissions change. Changes to approval, cost, security, or role semantics
require explicit approval for that change, not incidental cleanup. Review
questionable assumptions separately. Distinguish proposed rules, implemented
checks, and behavior actually verified by a live harness.

## Developer CLI and test conventions

Developer workflows live in `tools/catalog/src/jcode_catalog/`, a Python 3.14
project managed by uv. Use
`uv run --project tools/catalog jcode-catalog --help` to discover commands.
`just` recipes are thin aliases only. Isolation is enforced inside the CLI
before checks or builds, and `tools/scripts/isolate_config.py` remains the
standalone bootstrap for validation commands launched outside the CLI. Do not
add workflow logic to recipes or new standalone developer scripts.

User-facing asset setup and shipped plugins use vanilla Python 3.14 or newer,
with `python3` on PATH, and must not require uv or developer dependencies.

Keep tests beside the implementation they exercise in
`tools/catalog/src/jcode_catalog/` or `tools/scripts/`. Name Python tests
`<implementation_stem>_test.py` and shell
tests `<implementation_stem>_test.sh`, not `test_<name>`. For cross-module or
recipe tests, use a descriptive workflow name with the same suffix. Update
pytest discovery and references when adding or renaming tests. Existing
component-specific conventions in plugins, workers, and evaluator stay intact.

## Plugins

Plugin source is external catalog content under `customizations/plugins/`.
Keep each plugin in its own directory, document its upstream source and
update procedure, and avoid mixing plugin implementation changes with
mail-patch changes unless required.

`customizations/plugins/.gitignore` ignores plugin directories by default so
users can keep local plugins in their checkout without adding them to the
catalog. Whenever adding a shipped plugin under `customizations/plugins/`, add
its directory to the explicit allowlist in
`customizations/plugins/.gitignore` too, and verify asset setup copies it
while continuing to exclude unlisted local plugin directories.

## Skills

Shipped skill source lives under `customizations/skills/<name>/`. Project-local
agent workflows remain under `.jcode/skills/`.

`customizations/skills/.gitignore` ignores local additions by default. Add an
explicit `!/<name>/` entry for each shipped skill, and verify asset setup
copies it while excluding unlisted local skills.
`tools/scripts/setup_assets.py` installs
skills into the target jcode home's `skills/` directory using the same managed
asset safety rules as plugins, preserving unrelated local skills in copy mode.
Default symlink mode links the source directory, so ignore rules filter copies,
not the contents visible through a symlink.

Changes to a separate globally installed skill do not automatically update the
catalog source. A symlink installation shares the catalog source directly.
