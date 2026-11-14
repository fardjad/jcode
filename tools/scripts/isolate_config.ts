#!/usr/bin/env bun
import { constants } from "node:os";
import { basename } from "node:path";
import { withIsolatedEnv } from "../catalog/src/isolation";

const command = process.argv.slice(2);
if (command[0] === "--") command.shift();

if (command.length === 0) {
  console.error(`usage: ${basename(process.argv[1])} [--] COMMAND [ARGUMENT ...]`);
  process.exitCode = 2;
} else {
  // Resolve the requested executable before replacing PATH; its descendants
  // see only the isolated tool path, not the caller's search configuration.
  const executable = command[0] === "bun" ? process.execPath : Bun.which(command[0]!) ?? command[0]!;
  process.exitCode = await withIsolatedEnv(async (env) => {
    const child = Bun.spawn([executable, ...command.slice(1)], { env, stdio: ["inherit", "inherit", "inherit"] });
    const status = await child.exited;
    const signal = child.signalCode;
    if (signal !== null) {
      const number = typeof signal === "number" ? signal : constants.signals[signal];
      if (number !== undefined) return 128 + number;
    }
    return status;
  });
}
