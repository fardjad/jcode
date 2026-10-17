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
`scripts/isolate_config.py` wrapper, which also provides disposable
`JCODE_HOME` and other XDG directories. `just` validation runs declared patch
commands and compatibility tests through this isolation, and the
patched-workspace compile check is isolated as well. Do not weaken this
boundary by inheriting a user's `JCODE_HOME` or XDG settings in new workflow
commands. The wrapper may preserve or derive the standard `CARGO_HOME` and
`RUSTUP_HOME` locations so Rust toolchains and caches remain usable, but must
not preserve `JCODE_HOME` or any XDG path from the caller. This safeguard does
not authorize access to any personal path.

`personalized` is an orphan catalog branch. It contains catalog content,
including documentation, installers, workflow scripts, `patches/`, `plugins/`,
`workers/`, and project-local skills under `.jcode/skills/`, not an upstream
source checkout. `VERSION.txt` pins a stable upstream release; ordered mail
patches describe changes without requiring original source commits.

Validator discovers every `patches/*.patch`, checks intent, kind, dependency,
and body-section metadata, then applies all patches in lexicographic order with
`git am` to a disposable worktree based on the release pinned by `VERSION.txt`.

Commands:

```text
just help
just test-catalog               # run catalog script and plugin tests
just validate-patch-files
just create-patched-copy
just snapshot-patches            # regenerate patches and normalize personal hashes
just snapshot-patches --prune    # also delete patches no commit claims
just list-patches                # print commit-to-patch mapping
just install-patched-version
just test-patch-file <patch-file>
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
and print cleanup commands.

Compatibility failures are learned only from clean upstream worktrees. Ignored
state lives at `.tools/nextest/exclusions/<base-commit>.json`, keyed by base
commit and pinned nextest version. Patched failures are not learned or hidden:
they remain test failures. Delete that directory to reset learned state.

Compatibility tests pin cargo-nextest `0.9.143`, bootstrapped on demand and
cached under ignored `.tools/nextest/<version>/<target>/`.
JUnit reports live at `target/nextest/compat/junit.xml` inside each materialized
worktree. Learned exclusions are sorted exact `test(=...)` filters and apply
only when base commit and nextest pin match.

`just set-version vX.Y.Z` fetches that stable release tag from the canonical
jcode repository, refuses the change when an optional reviewed commit hash does
not match, and writes `VERSION.txt`. It requires a clean `personalized`
checkout and does not validate patches, rebase, or push. Every other recipe
then works on the new release: create the patched copy, fix patches, snapshot,
and run `just learn-upstream-exclusions` and `just validate-patched-copy` as
the final check. The pin and patch changes are committed together; until then
`git checkout -- VERSION.txt` restores the previous pin. The moving upstream
`master` branch is never used.

`VERSION.txt` is the only version pin. Catalog workflows do not use a `master`
branch, and upgrades go through `/plan-upstream-sync`, which reviews and then,
with separate approval, executes the upgrade. GitHub's default branch is
`personalized`.

Patch application order is lexicographic. Dependencies must be declared with
`X-Jcode-Patch-Depends-On` as `none` or comma-separated earlier patch names.
Required metadata headers are `X-Jcode-Patch-Intent`,
`X-Jcode-Patch-Kind`, `X-Jcode-Patch-Depends-On`, and `X-Jcode-Patch-File`.
Each patch must contain
non-empty ordered sections: `Patch intent:`, `Why it exists:`, `Upstream
integration points:`, `Update guidance:`, and `Validation:`.

## Decision-first upstream review

Before an upstream sync, use the project-local `/plan-upstream-sync` skill.
This is an agent workflow requirement, not a check implemented by any recipe.
Planning and execution require separate explicit user approval.

Start with a clean `personalized` catalog, including no untracked nonignored
files. Never automatically stash, discard, or commit changes to make it clean.
Fetch the configured catalog tracking remote and compare with its configured
tracking branch. This is distinct from the canonical upstream release source.
If strictly behind, fast-forward only after checking it is safe. Stop on
divergence, missing tracking configuration, or fetch failure. Report local-only
commits without discarding them. Recheck cleanliness and tracking status after
updating before collecting evidence.

Use `VERSION.txt`, not the running binary version, as the baseline. Verify an
explicit stable target tag from canonical jcode. Record catalog HEAD, baseline
and target commit hashes, and the ordered patch filenames and content hashes.
Review baseline-exclusive, target-inclusive full commit messages, all available
intervening release notes and changelog entries, and relevant actual diffs and
tests. Record source references, missing notes, and incomplete evidence. History
and release-note retrieval for review is allowed without changing `VERSION.txt`
or any branch; it does not change the stable-tag-only sync policy.

Ensure `/sync-scratch/` is ignored before creating it. Produce these temporary
reports in dependency order:

1. `1-commits-and-release-notes-context.md`: reviewed input identities, complete
   commit inventory and full messages, release-note/changelog context, sources,
   and coverage limitations.
2. `2-upstream-intent.md`: upstream goals with stated intent distinguished from
   inference, linked commits, symbols, behavior changes, tests, and uncertainties
   sufficient for patch-by-patch comparison.
3. `3-fork-vs-upstream-intent.md`: compare every catalog patch's authoritative
   intent with upstream goals and implementation. Identify conflicting
   directions, overlapping changes, adoption evidence, and residual gaps.
4. `4-sync-plan.md`: temporary decision checklist with IDs, options,
   recommendations, consequences, user answers, and affected patches. Include
   trivial updates, justified removal candidates, dependency-safe ordering,
   feature docs under `docs/` affected by changed or removed patches,
   `upstream-candidate/*` branches and upstream PRs to retire for adopted
   candidates, expected cost of the exclusion-learning and validation builds,
   validation checks, and a concrete recovery procedure.

Patch intent has one source of truth: source commit metadata and its derived
catalog patch, following the commit-first workflow below. Reports reference
that intent, not a separately maintained definition. Similar wording or file
overlap is not adoption. Removal requires equivalent behavior and test evidence
for the full intent, with residual differences made explicit.

Each patch must deliver one end-user feature or fix. A coherent series of changes
supporting workers with blueprints may belong together; unrelated session
deletion with Ctrl-D must not be mixed into it. Proposed removals, splits,
consolidations, and ordering must respect declared dependencies and the
lexicographic application order.

A plan is ready when decisions are resolved, uncertainties are addressed or
explicitly accepted, and checks map to the proposed outcomes. Include declared
patch validations, clean upstream and patched compatibility checks, and the
combined patched-workspace check through existing isolated tooling. Record a
recovery procedure that preserves work and does not assume a destructive reset.
Immediately before execution, recheck cleanliness, catalog tracking status,
catalog HEAD, pin, patch identities, and canonical target hash. Changed inputs
invalidate affected analysis and require review before proceeding.

Planning must not edit patches or source, reset `.patched-jcode`, or update the
release pin. Execution approval is separate from answering decisions
or declaring the plan ready. Approved implementation uses the existing
commit-first workflow; catalog commits and pushes still need explicit requests.

Execution, once approved, follows the skill's execute section: recheck inputs,
`just set-version vX.Y.Z <reviewed-hash>`, recreate `.patched-jcode`, carry each
patch forward per the plan, snapshot, run each changed patch's validation, then
`just learn-upstream-exclusions` and `just validate-patched-copy`. Update
affected docs. The pin, patches, and docs land together in one catalog commit,
made only on request.

Reports and decisions are disposable working material, not a permanent archive
or alternate intent source. Reuse existing reports only for matching inputs;
ask before replacing existing scratch work. After a successful validated sync,
they may be deleted with user permission, limited to the assigned scratch
contents. Preserve evidence on failure until recovery and cleanup are agreed.

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

1. `just create-patched-copy` — ensures `.patched-jcode` exists, clean, and
   matches the current catalog.
2. Edit source files directly in `.patched-jcode/`.
3. Commit or amend the relevant commit there (see below for which commit).
4. Review intent vs code vs tests (see "Patch review" below).
5. `just snapshot-patches` — regenerates every `.patch` file from the commits
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
each command is one unindented line under `Validation:`. The `X-Jcode-Patch-*`
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

## Plugins

Plugin source is external catalog content under `plugins/`. Keep each plugin in
its own directory, document its upstream source and update procedure, and avoid
mixing plugin implementation changes with mail-patch changes unless required.
