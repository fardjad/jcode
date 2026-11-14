import { describe, expect, test } from "bun:test";
import { isRustTest, parseMetadata } from "../src/metadata";

const message = `Subject: example

X-Jcode-Patch-Intent: keep a feature
X-Jcode-Patch-Depends-On: 0001-first.patch, 0002-second.patch
X-Jcode-Patch-File: 0003-feature.patch

Patch intent:
intent

Why it exists:
reason

Upstream integration points:
point

Update guidance:
guidance

Validation:
cargo test -p app
scripts/cargo_exec.sh test --lib
`;

describe("patch metadata", () => {
  test("reads headers, ordered sections and commands before the diff", () => {
    expect(parseMetadata(message + "---\ndiff --git a/file b/file\n+Validation:\n+X-Jcode-Patch-File: wrong\n")).toEqual({
      intent: "keep a feature",
      dependencies: ["0001-first.patch", "0002-second.patch"],
      filename: "0003-feature.patch",
      commands: ["cargo test -p app", "scripts/cargo_exec.sh test --lib"],
    });
    expect(parseMetadata(message.replace("0001-first.patch, 0002-second.patch", "none")).dependencies).toEqual([]);
    expect(parseMetadata(message.replaceAll("\n", "\r\n")).commands).toHaveLength(2);
  });

  test("rejects missing, empty or repeated headers and bad dependencies", () => {
    for (const broken of [
      message.replace("X-Jcode-Patch-File: 0003-feature.patch\n", ""),
      message.replace("X-Jcode-Patch-Intent: keep a feature", "X-Jcode-Patch-Intent:   "),
      message.replace("X-Jcode-Patch-File: 0003-feature.patch", "X-Jcode-Patch-File: 0003-feature.patch\nX-Jcode-Patch-File: other"),
      message.replace("0001-first.patch, 0002-second.patch", "0001-first.patch, 0001-first.patch"),
      message.replace("0001-first.patch, 0002-second.patch", "0001-first.patch, "),
    ]) expect(() => parseMetadata(broken)).toThrow();
  });

  test("rejects missing, repeated, empty or reordered sections and indented commands", () => {
    for (const broken of [
      message.replace("Why it exists:", "Reason:"),
      message.replace("Why it exists:", "Why it exists:\nWhy it exists:"),
      message.replace("Why it exists:\nreason", "Why it exists:\n"),
      message.replace("Why it exists:", "TEMP:").replace("Update guidance:", "Why it exists:").replace("TEMP:", "Update guidance:"),
      message.replace("cargo test -p app", "  cargo test -p app"),
      message.replace("cargo test -p app", "- cargo test -p app"),
    ]) expect(() => parseMetadata(broken)).toThrow();
  });

  test("recognizes Rust test launchers without accepting incidental prefixes", () => {
    for (const command of ["cargo test", "cargo 'test' -p app", "scripts/cargo_exec.sh test --lib", "'cargo' test -- --skip a"]) expect(isRustTest(command)).toBe(true);
    for (const command of ["cargo nextest run", "cargo testing", "echo cargo test", "other/cargo test", "cargo 'test", "cargo test \\"]) expect(isRustTest(command)).toBe(false);
  });
});
