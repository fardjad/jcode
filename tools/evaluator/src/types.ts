export type FindingSeverity = "low" | "medium" | "high" | "critical";
export type FindingCategory = "blueprint" | "plugin" | "patch" | "upstream" | "evaluator";

export interface Finding {
  severity: FindingSeverity;
  category: FindingCategory;
  text: string;
  fix_target: string;
  citations: string[];
}

export interface SessionEvidence {
  id: string;
  history: unknown[];
  tools: unknown[];
  persisted?: unknown;
}

export interface CostSummary {
  totalUsd: number | null;
  bySession: Record<string, number | null>;
  inputTokens: number;
  outputTokens: number;
  cacheReadInputTokens: number;
}

export interface EvidenceBundle {
  sessions: SessionEvidence[];
  records: Record<string, unknown>[];
  citations: Record<string, unknown>;
  gaps: string[];
  costs: CostSummary;
}

export interface Review {
  findings: Finding[];
}
