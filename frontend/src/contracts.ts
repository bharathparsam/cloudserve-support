/* Generated from docs/contracts.schema.json. Do not edit. */

export type SchemaVersion = number;
export type RecordId = string;
export type RunId = string;
export type InputIndex = number;
export type Action = "auto_respond" | "escalate";
export type Blocked = boolean;
export type Reason = string;
export type Answer = string | null;
export type Summary = string;
export type Reason1 = string;
export type OriginalReference = string;
export type Status = "ok" | "unavailable";
export type Label = string;
export type RawProbability = number;
export type CalibratedProbability = number;
export type Alternatives = {
  [k: string]: unknown;
}[];
export type ProviderConfidence = null;
export type ArtifactId = string | null;
export type CalibrationStatus = string;
export type Reason2 = string | null;
export type DocId = string;
export type ChunkId = string;
export type Title = string;
export type Content = string;
export type Start = number;
export type End = number;
export type CorpusHash = string;
export type Score = number;
export type ScoreKind = string;
export type Sources = SourcePassage[];
export type Troubleshooting = string;
export type MissingContext = string[];
export type UnresolvedQuestions = string[];
export type Provenance = string;
export type Sources1 = SourcePassage[];
export type Action1 = "auto_candidate" | "escalate";
export type ReasonCodes = string[];
export type Reason3 = string;
export type PolicyHash = string;
export type Name = string;
export type Version = string;
export type Status1 = "pass" | "fail" | "unknown" | "not_applicable";
export type Reasons = string[];
export type ClaimIds = string[];
export type Checks = GuardrailResult[];
export type Provenance1 = string;
export type Closed = null;

export interface Outcome {
  schema_version?: SchemaVersion;
  record_id: RecordId;
  run_id: RunId;
  input_index: InputIndex;
  action: Action;
  blocked: Blocked;
  reason: Reason;
  answer?: Answer;
  handoff?: Handoff | null;
  classification: Classification;
  sources?: Sources1;
  route: RouteDecision;
  checks?: Checks;
  provenance?: Provenance1;
  timings?: Timings;
  versions?: Versions;
  closed?: Closed;
}
export interface Handoff {
  summary: Summary;
  reason: Reason1;
  original_reference: OriginalReference;
  classification: Classification;
  sources?: Sources;
  troubleshooting?: Troubleshooting;
  missing_context?: MissingContext;
  unresolved_questions?: UnresolvedQuestions;
  provenance?: Provenance;
}
export interface Classification {
  status: Status;
  intent?: Dimension | null;
  urgency?: Dimension | null;
  artifact_id?: ArtifactId;
  calibration_status?: CalibrationStatus;
  reason?: Reason2;
}
export interface Dimension {
  label: Label;
  raw_probabilities: RawProbabilities;
  raw_probability: RawProbability;
  calibrated_probability: CalibratedProbability;
  alternatives?: Alternatives;
  provider_confidence?: ProviderConfidence;
}
export interface RawProbabilities {
  [k: string]: number;
}
export interface SourcePassage {
  doc_id: DocId;
  chunk_id: ChunkId;
  title: Title;
  content: Content;
  start: Start;
  end: End;
  corpus_hash: CorpusHash;
  score: Score;
  score_kind?: ScoreKind;
}
export interface RouteDecision {
  action: Action1;
  reason_codes: ReasonCodes;
  reason: Reason3;
  policy_hash: PolicyHash;
  thresholds?: Thresholds;
}
export interface Thresholds {
  [k: string]: unknown;
}
export interface GuardrailResult {
  name: Name;
  version?: Version;
  status: Status1;
  reasons?: Reasons;
  claim_ids?: ClaimIds;
}
export interface Timings {
  [k: string]: number;
}
export interface Versions {
  [k: string]: unknown;
}
