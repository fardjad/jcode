import { afterEach, expect, test } from "bun:test";
import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, rmSync, statSync, writeFileSync } from "node:fs";
import type { symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { installAssets } from "./setup_assets";

const roots: string[] = [];
function fixture() {
  const root = mkdtempSync(join(tmpdir(), "jcode-assets-"));
  roots.push(root);
  const repo = join(root, "repo"), custom = join(repo, "customizations"), home = join(root, "home");
  for (const dir of ["plugins/rtk/nested", "plugins/local", "workers/blueprints", "skills/author-plan", "skills/local"]) mkdirSync(join(custom, dir), { recursive: true });
  writeFileSync(join(custom, "plugins/.gitignore"), "/*/\n!/rtk/\n");
  writeFileSync(join(custom, "skills/.gitignore"), "/*\n!/author-plan/\n");
  writeFileSync(join(custom, "plugins/rtk/rtk-transform"), "#!/bin/sh\nexit 0\n");
  chmodSync(join(custom, "plugins/rtk/rtk-transform"), 0o755);
  writeFileSync(join(custom, "plugins/rtk/test_rtk_transform.py"), "tests");
  writeFileSync(join(custom, "plugins/rtk/nested/content"), "included");
  writeFileSync(join(custom, "plugins/local/secret"), "not shipped");
  writeFileSync(join(custom, "skills/author-plan/SKILL.md"), "skill");
  writeFileSync(join(custom, "skills/local/SKILL.md"), "not shipped");
  for (const name of ["coordinator", "routine", "advanced", "expert"]) writeFileSync(join(custom, "workers/blueprints", `${name}.md`), name);
  writeFileSync(join(custom, "config.toml"), "[features]\n");
  return { repo, custom, home };
}
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

test("default links catalog assets and leaves unmanaged configuration untouched", () => {
  const { repo, custom, home } = fixture();
  mkdirSync(home);
  writeFileSync(join(home, "config.toml"), "user config");
  writeFileSync(join(home, "config.private.toml"), "private");
  expect(installAssets(repo, home)).toContain("config.toml: skipped existing unmanaged file");
  expect(readlinkSync(join(home, "plugins"))).toBe(join(custom, "plugins"));
  expect(readlinkSync(join(home, "worker-blueprints"))).toBe(join(custom, "workers/blueprints"));
  expect(readlinkSync(join(home, "skills"))).toBe(join(custom, "skills"));
  expect(readFileSync(join(home, "config.toml"), "utf8")).toBe("user config");
  expect(readFileSync(join(home, "config.private.toml"), "utf8")).toBe("private");
  expect(installAssets(repo, home)).toContain("plugins: already linked");
});

test("copy mode filters unallowlisted assets and test files while preserving executable permissions", () => {
  const { repo, home } = fixture();
  installAssets(repo, home, { copy: true });
  expect(readFileSync(join(home, "plugins/rtk/nested/content"), "utf8")).toBe("included");
  expect(statSync(join(home, "plugins/rtk/rtk-transform")).mode & 0o111).not.toBe(0);
  expect(existsSync(join(home, "plugins/rtk/test_rtk_transform.py"))).toBe(false);
  expect(existsSync(join(home, "plugins/local"))).toBe(false);
  expect(existsSync(join(home, "skills/local"))).toBe(false);
  expect(readFileSync(join(home, "skills/author-plan/SKILL.md"), "utf8")).toBe("skill");
  expect(lstatSync(join(home, "config.toml")).isFile()).toBe(true);
});

test("copy mode converts owned links and refuses unmanaged directories before changing anything", () => {
  const { repo, home } = fixture();
  installAssets(repo, home);
  installAssets(repo, home, { copy: true });
  expect(lstatSync(join(home, "plugins")).isDirectory()).toBe(true);
  expect(lstatSync(join(home, "config.toml")).isFile()).toBe(true);
  expect(() => installAssets(repo, home, { copy: true })).toThrow("refusing to overwrite unmanaged asset");
  expect(readFileSync(join(home, "plugins/rtk/nested/content"), "utf8")).toBe("included");
});

test("unsupported symlink creation falls back to filtered copies", () => {
  const { repo, home } = fixture();
  const link = (() => { const error = new Error("not supported") as NodeJS.ErrnoException; error.code = "ENOTSUP"; throw error; }) as typeof symlinkSync;
  expect(installAssets(repo, home, { link })).toContain("plugins: copied (symlinks unavailable)");
  expect(lstatSync(join(home, "plugins")).isDirectory()).toBe(true);
  expect(existsSync(join(home, "plugins/local"))).toBe(false);
  expect(readFileSync(join(home, "config.toml"), "utf8")).toBe("[features]\n");
});

test("command installs the real catalog into the explicitly supplied disposable JCODE_HOME", async () => {
  const { home } = fixture();
  const script = resolve(import.meta.dir, "setup_assets.ts");
  const child = Bun.spawn([process.execPath, script, "--copy"], { env: { ...process.env, JCODE_HOME: home }, stdout: "pipe", stderr: "pipe" });
  const [out, err, status] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  expect({ status, err }).toEqual({ status: 0, err: "" });
  expect(out).toContain("plugins: copied");
  expect(existsSync(join(home, "plugins/rtk/rtk-transform"))).toBe(true);
  expect(existsSync(join(home, "skills/author-plan/SKILL.md"))).toBe(true);
});
