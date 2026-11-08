# Show this help
help:
    @just --list 

# Validate patch metadata and apply the complete catalog to the pinned release.
validate-patch-files:
    uv run --project tools/catalog jcode-catalog patches check

# Check each patch applies with only its declared dependencies.
check-patch-isolation:
    uv run --project tools/catalog jcode-catalog patches isolation

# Create or refresh the clean .patched-jcode worktree from the pinned release.
create-patched-copy:
    uv run --project tools/catalog jcode-catalog patches create

# Refresh .patched-jcode, check the workspace, run all Validation commands and compatibility.
validate-patched-copy:
    uv run --project tools/catalog jcode-catalog patches validate

# Regenerate patches from source commits and normalize personal patch hashes.
snapshot-patches *args:
    uv run --project tools/catalog jcode-catalog patches snapshot {{args}}

# Show the source-commit-to-patch mapping for .patched-jcode.
list-patches:
    uv run --project tools/catalog jcode-catalog patches list

# Run catalog, asset, plugin, worker, and evaluator tests without paid calls.
test-catalog:
    uv run --project tools/catalog jcode-catalog test

# Check one patch's dependency closure and run only its own Validation commands.
test-patch-file patch:
    uv run --project tools/catalog jcode-catalog patches test "{{patch}}"

# Gate the full stack once, then check closures without current passes; --compat-each for syncs.
test-all-patches *args:
    uv run --project tools/catalog jcode-catalog patches test --all {{args}}

# Validate a candidate patch and create its branch without overwriting divergence.
create-upstream-candidate-branch-from patch:
    uv run --project tools/catalog jcode-catalog patches candidate "{{patch}}"

# Learn baseline test failures from clean upstream, never from patched source.
learn-upstream-exclusions:
    uv run --project tools/catalog jcode-catalog patches learn

# Build exact catalog binaries and run paid worker evaluations, optionally by scenario.
eval-workers scenario="":
    uv run --project tools/catalog jcode-catalog workers evaluate {{scenario}}

# Refresh .patched-jcode, build a release binary, and install it locally.
install-patched-version:
    uv run --project tools/catalog jcode-catalog install

# Pin a canonical stable release, optionally checking its reviewed commit hash.
set-version release expected="":
    uv run --project tools/catalog jcode-catalog version set "{{release}}" {{expected}}

# Push personalized and upstream-candidate branches to origin.
push:
    uv run --project tools/catalog jcode-catalog push
