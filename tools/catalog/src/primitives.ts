import { dlopen, FFIType } from "bun:ffi";
import { randomUUID } from "node:crypto";
import { mkdir, open, rename, unlink } from "node:fs/promises";
import { basename, dirname, join } from "node:path";

// Advisory locks remain attached to the open file and are released on process
// death. Never unlink a lock file: doing so can create two independent locks.
const unix = process.platform === "win32" ? undefined : dlopen(
  process.platform === "darwin" ? "/usr/lib/libSystem.B.dylib" : "libc.so.6",
  { flock: { args: [FFIType.i32, FFIType.i32], returns: FFIType.i32 } },
);
const windows = process.platform !== "win32" ? undefined : dlopen("msvcrt.dll", {
  _locking: { args: [FFIType.i32, FFIType.i32, FFIType.i32], returns: FFIType.i32 },
  _lseek: { args: [FFIType.i32, FFIType.i32, FFIType.i32], returns: FFIType.i32 },
});

export async function withFileLock<T>(path: string, action: (fd: number) => Promise<T>): Promise<T> {
  await mkdir(dirname(path), { recursive: true });
  const file = await open(path, "a+", 0o600);
  let locked = false;
  try {
    if (windows) {
      if ((await file.stat()).size === 0) await file.write(" ", 0);
      if (windows.symbols._lseek(file.fd, 0, 0) < 0 || windows.symbols._locking(file.fd, 2, 1) !== 0) {
        throw new Error(`Lock already held or unavailable: ${path}`);
      }
    } else if (!unix || unix.symbols.flock(file.fd, 2 | 4) !== 0) {
      throw new Error(`Lock already held or unavailable: ${path}`);
    }
    locked = true;
    return await action(file.fd);
  } finally {
    if (locked && windows) {
      windows.symbols._lseek(file.fd, 0, 0);
      windows.symbols._locking(file.fd, 0, 1);
    }
    await file.close();
  }
}

export async function atomicWrite(path: string, data: string | Uint8Array, mode = 0o600): Promise<void> {
  const dir = dirname(path);
  await mkdir(dir, { recursive: true });
  const temporary = join(dir, `.${basename(path)}.${randomUUID()}.tmp`);
  let exists = false;
  try {
    const file = await open(temporary, "wx", mode);
    exists = true;
    try {
      await file.writeFile(data);
      await file.sync();
    } finally {
      await file.close();
    }
    await rename(temporary, path);
    exists = false;
  } finally {
    if (exists) await unlink(temporary);
  }
}
