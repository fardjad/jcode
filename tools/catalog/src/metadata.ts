export interface PatchMetadata {
  intent: string;
  dependencies: string[];
  filename: string;
  commands: string[];
}

const sections = ["Patch intent:", "Why it exists:", "Upstream integration points:", "Update guidance:", "Validation:"] as const;
const headers = ["Intent", "Depends-On", "File"] as const;

/** Interpret commit messages and mail patches identically, stopping before the diff. */
export function parseMetadata(text: string): PatchMetadata {
  const message = text.split(/\r?\n---\r?\n/, 1)[0]!.replace(/\r\n/g, "\n");
  const values = new Map<string, string[]>();
  const positions: number[][] = sections.map(() => []);
  let offset = 0;
  for (const line of message.split("\n")) {
    const match = /^X-Jcode-Patch-(Intent|Depends-On|File):[ \t]*(.*)$/.exec(line);
    if (match) {
      const entries = values.get(match[1]!) ?? [];
      entries.push(match[2]!.trim());
      values.set(match[1]!, entries);
    }
    for (let index = 0; index < sections.length; index++) {
      let start = 0;
      while (true) {
        const found = line.indexOf(sections[index]!, start);
        if (found < 0) break;
        positions[index]!.push(offset + found);
        start = found + sections[index]!.length;
      }
    }
    offset += line.length + 1;
  }
  for (const name of headers) {
    const entries = values.get(name) ?? [];
    if (entries.length !== 1 || !entries[0]) throw new Error(`must have exactly one non-empty X-Jcode-Patch-${name}: header; found ${entries.length}`);
  }
  let previous = -1;
  for (let index = 0; index < sections.length; index++) {
    const matches = positions[index]!;
    if (matches.length !== 1) throw new Error(`must have exactly one ${sections[index]}`);
    if (matches[0]! <= previous) throw new Error("body sections out of order");
    previous = matches[0]!;
  }
  for (let index = 0; index < sections.length; index++) {
    const start = positions[index]![0]! + sections[index]!.length;
    const end = index + 1 < sections.length ? positions[index + 1]![0]! : message.length;
    if (!message.slice(start, end).trim()) throw new Error(`empty ${sections[index]}`);
  }
  const commands = message.slice(positions[4]![0]! + sections[4].length).split("\n").filter(line => line.trim());
  if (commands.some(line => line !== line.trimStart() || /^[\-*`]/.test(line))) throw new Error("invalid validation command");
  const dependencyText = values.get("Depends-On")![0]!;
  const dependencies = dependencyText === "none" ? [] : dependencyText.split(",").map(item => item.trim());
  if (dependencies.some(item => !item) || new Set(dependencies).size !== dependencies.length) throw new Error("invalid or duplicate dependency");
  return { intent: values.get("Intent")![0]!, dependencies, filename: values.get("File")![0]!, commands };
}

/** Recognize the supported direct cargo launchers, including shell quoting. */
export function isRustTest(command: string): boolean {
  const words: string[] = [];
  let word = "", quote = "", started = false;
  for (let i = 0; i < command.length; i++) {
    const char = command[i]!;
    if (quote === "'") {
      if (char === "'") quote = ""; else word += char;
    } else if (quote === '"') {
      if (char === '"') quote = "";
      else if (char === "\\" && i + 1 < command.length && ['"', "\\"].includes(command[i + 1]!)) word += command[++i];
      else word += char;
    } else if (char === "'" || char === '"') {
      quote = char; started = true;
    } else if (char === "\\") {
      if (++i >= command.length) return false;
      word += command[i]; started = true;
    } else if (/\s/.test(char)) {
      if (started) { words.push(word); word = ""; started = false; }
    } else {
      word += char; started = true;
    }
  }
  if (quote) return false;
  if (started) words.push(word);
  return (words[0] === "cargo" || words[0] === "scripts/cargo_exec.sh") && words[1] === "test";
}
