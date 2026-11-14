# Show this help
help:
    @just --list 

# Validate patch metadata and apply the complete catalog to the pinned release.
validate-patch-files:
    bun tools/catalog/src/cli.ts patches check

# Check each patch applies with only its declared dependencies.
check-patch-isolation:
    bun tools/catalog/src/cli.ts patches isolation

# Create or refresh the clean .patched-jcode worktree from the pinned release.
create-patched-copy:
    bun tools/catalog/src/cli.ts patches create

# Refresh .patched-jcode, check the workspace, run all Validation commands and compatibility.
validate-patched-copy:
    bun tools/catalog/src/cli.ts patches validate

# Regenerate patches from source commits and normalize patch hashes.
snapshot-patches *args:
    bun tools/catalog/src/cli.ts patches snapshot {{args}}

# Show the source-commit-to-patch mapping for .patched-jcode.
list-patches:
    bun tools/catalog/src/cli.ts patches list

# Run catalog, asset, plugin, worker, and evaluator tests without paid calls.
test-catalog:
    bun tools/catalog/src/cli.ts test

# Check one patch's dependency closure and run only its own Validation commands.
test-patch-file patch:
    bun tools/catalog/src/cli.ts patches test "{{patch}}"

# Gate the full stack once, then check closures without current passes; --compat-each for syncs.
test-all-patches *args:
    bun tools/catalog/src/cli.ts patches test --all {{args}}

# Learn baseline test failures from clean upstream, never from patched source.
learn-upstream-exclusions:
    bun tools/catalog/src/cli.ts patches learn

# Build exact catalog binaries and run paid worker evaluations, optionally by scenario.
eval-workers scenario="":
    bun tools/catalog/src/cli.ts workers evaluate {{scenario}}

# Refresh .patched-jcode, build a release binary, and install it locally.
install-patched-version:
    bun tools/catalog/src/cli.ts install

# Pin a canonical stable release, optionally checking its reviewed commit hash.
set-version release expected="":
    bun tools/catalog/src/cli.ts version set "{{release}}" {{expected}}

# Push personalized branch to origin.
push:
    bun tools/catalog/src/cli.ts push
