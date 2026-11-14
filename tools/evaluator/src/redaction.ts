import { atomicWrite } from "../../catalog/src/primitives";

export class Redactor {
  private readonly variants: string[];

  constructor(secrets: readonly string[]) {
    const variants = new Set<string>();
    for (const secret of secrets) {
      if (!secret) continue;
      variants.add(secret);
      for (const encoding of ["base64", "base64url"] as const) {
        const encoded = Buffer.from(secret).toString(encoding);
        variants.add(encoded);
        variants.add(encoded.replace(/=+$/, ""));
      }
    }
    this.variants = [...variants].sort((a, b) => b.length - a.length);
  }

  text(value: string): string {
    for (const variant of this.variants) value = value.split(variant).join("[REDACTED]");
    return value;
  }

  value(value: unknown): unknown {
    if (typeof value === "string") return this.text(value);
    if (Array.isArray(value)) return value.map(item => this.value(item));
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [this.text(key), this.value(item)]));
    return value;
  }
}

export async function writeRedacted(path: string, text: string, redactor: Redactor): Promise<void> {
  await atomicWrite(path, redactor.text(text), 0o600);
}
