/**
 * InsightFlow — TypeScript API contract.
 *
 * Mirror of the FastAPI Pydantic schemas in
 *   backend/app/schemas/{project,feedback,analysis,cluster,insight,opportunity,requirement_prd}.py
 *
 * Field names are kept identical to the backend JSON responses — never
 * invent convenience names. Nullable fields are explicit. Optional fields
 * that the backend may omit in older builds should still be marked as
 * optional here, but the documented response shape is the source of truth.
 */

export type ProjectId = string;
export type FeedbackId = string;
export type AnalysisRunId = string;
export type ClusterRunId = string;
export type InsightId = string;
export type InsightRunId = string;
export type OpportunityScoreId = string;
export type RequirementId = string;
export type PrdId = string;

export interface Project {
  id: ProjectId;
  name: string;
  product_name: string;
  description?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ProjectCreate {
  name: string;
  product_name: string;
  description?: string | null;
}

/* ---------- Feedback ---------- */

export type Sentiment = 'positive' | 'neutral' | 'negative';
export type FeedbackUserType = 'customer' | 'internal' | 'partner' | 'unknown';

export interface Feedback {
  id: FeedbackId;
  project_id: ProjectId;
  external_id: string;
  content: string;
  source?: string | null;
  user_id?: string | null;
  user_type?: string | null;
  feedback_created_at?: string | null;
  created_at?: string;
}

export interface FeedbackListResponse {
  items: Feedback[];
  total: number;
  limit: number;
  offset: number;
}

export interface FeedbackImportSummary {
  total: number;
  imported: number;
  skipped: number;
}

/* ---------- Classification (Analysis) ---------- */

// Mirrors backend/app/models/analysis.py:RunStatus exactly:
//   pending / running / completed / partial_failed / failed
export type AnalysisStatus = 'pending' | 'running' | 'completed' | 'partial_failed' | 'failed';
export type FeedbackType =
  | 'praise'
  | 'feature_request'
  | 'bug_report'
  | 'pain_point'
  | 'question'
  | 'other';

export interface ClassificationResult {
  feedback_id: FeedbackId;
  feedback_type: FeedbackType;
  sentiment: Sentiment;
  confidence: number;
}

export interface AnalysisRun {
  id: AnalysisRunId;
  project_id: ProjectId;
  run_type: string;
  model?: string | null;
  prompt_version: string;
  status: AnalysisStatus;
  input_count: number;
  processed_count: number;
  success_count: number;
  failed_count: number;
  input_tokens?: number;
  output_tokens?: number;
  unknown_token_calls?: number;
  retry_count?: number;
  latency_ms?: number;
  attempt_count?: number;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string;
  error_message?: string | null;
  input_feedback_external_ids?: string[] | null;
}

/* Phase 7 Analysis history (D package) — only the two ``run_type``
 * values the runner actually emits are accepted on the read-side
 * filter. The open-string ``AnalysisRun.run_type`` is preserved
 * for forward-compat with rows the runner may grow into. */
export type AnalysisRunType = 'classification' | 'clustering';

export interface AnalysisRunListQuery extends Pagination {
  run_type?: AnalysisRunType;
}

/* ``ProjectAnalysisRunPage`` — project-scoped AnalysisRun index
 * (Phase 7 D endpoint ``GET
 * /api/projects/{project_id}/analysis-runs``). The list endpoint
 * is the source of truth for the Analysis history tab; the
 * frontend never falls back to local storage. Optional
 * ``run_type`` filter is restricted to the two values the runner
 * emits so a stale or invented value cannot reach the SQL
 * ``WHERE`` clause. Sort is ``created_at DESC, id DESC`` so
 * pagination stays stable when two rows share a timestamp. */
export interface ProjectAnalysisRunPage {
  total: number;
  items: AnalysisRun[];
  limit: number;
  offset: number;
}

/* ``ProjectInsightRunPage`` — project-scoped InsightRun index
 * (Phase 7 D endpoint ``GET
 * /api/projects/{project_id}/insight-runs``). Read-only
 * companion of the per-id ``GET /insight-runs/{run_id}`` for the
 * Analysis history tab. Same stable sort contract. */
export interface ProjectInsightRunPage {
  total: number;
  items: InsightRun[];
  limit: number;
  offset: number;
}

export interface AnalysisResultsResponse {
  run_id: AnalysisRunId;
  items: ClassificationResult[];
  total: number;
}

/* Phase 7 Feedback Detail (E package) — per-feedback AI
 * classification read DTO. Mirrors
 * backend/app/schemas/analysis.py:FeedbackAnalysisRead. The
 * fields are intentionally open strings (not the strict
 * FeedbackType / Sentiment enums) so the read shape stays
 * forward-compatible if the runner grows new categories — the
 * UI normalises at render time. */
export interface FeedbackAnalysisRead {
  id: string;
  feedback_id: FeedbackId;
  run_id: AnalysisRunId;
  feedback_type: string;
  module: string;
  intent: string;
  pain_point: string;
  sentiment: string;
  severity: number;
  confidence: number;
  semantic_text: string | null;
  created_at: string;
}

/* ``FeedbackAnalysisSelection`` — describes how the analysis
 * row was picked. ``mode`` is the only discriminator the UI
 * really needs; ``selection_rule`` is the human-readable
 * deterministic ordering the backend applied (no fallback to
 * clustering or other projects' runs, ever). */
export type FeedbackAnalysisSelectionMode = 'latest' | 'explicit';

export interface FeedbackAnalysisSelection {
  mode: FeedbackAnalysisSelectionMode;
  selected_run_id: AnalysisRunId | null;
  selection_rule: string;
}

/* Read envelope for
 * ``GET /api/projects/{project_id}/feedback/{feedback_id}/analysis``.
 * ``analysis`` is the full ``FeedbackAnalysisRead`` or
 * ``null`` when no analysis row exists for the chosen run
 * (the page renders "AI Analysis N/A" rather than guess a row
 * from a different run or project). ``feedback_id`` echoes the
 * path id so the page can verify the response is for the
 * currently open drawer. */
export interface FeedbackAnalysisEnvelope {
  feedback_id: FeedbackId;
  analysis: FeedbackAnalysisRead | null;
  selection: FeedbackAnalysisSelection;
}

/* ---------- Clustering ---------- */

export interface ClusterFeedbackRef {
  feedback_id: FeedbackId;
}

/**
 * The cluster endpoint reuses the AnalysisRun row for clustering runs; the
 * shape that comes back from the backend is the same AnalysisRunRead schema.
 */
export type ClusterRun = AnalysisRun;

export interface Cluster {
  id: string;
  run_id: ClusterRunId;
  project_id: ProjectId;
  source: string;
  parent_cluster_id?: string | null;
  name?: string | null;
  summary?: string | null;
  feedback_count: number;
  unique_user_count: number;
  average_severity?: number | null;
  negative_ratio?: number | null;
  confidence?: number | null;
  embedding_model: string;
  distance_threshold: number;
  linkage: string;
  created_at?: string;
}

/* ---------- Insights ---------- */

export type InsightStatus =
  | 'draft'
  | 'pending_review'
  | 'needs_validation'
  | 'accepted'
  | 'overridden'
  | 'deferred'
  | 'rejected';

export interface Insight {
  id: InsightId;
  run_id: InsightRunId;
  project_id: ProjectId;
  cluster_id: string;
  clustering_run_id: string;
  title?: string | null;
  pain_point?: string | null;
  affected_segment?: string | null;
  insight?: string | null;
  recommendation?: string | null;
  confidence?: number | null;
  status: InsightStatus | string;
  feedback_count: number;
  evidence_count: number;
  source_cluster_external_id: string;
  source_member_external_ids: string[];
  model?: string | null;
  prompt_version: string;
  created_at?: string;
}

export interface InsightRun {
  id: InsightRunId;
  project_id: ProjectId;
  clustering_run_id: string;
  classification_run_id: string;
  status: AnalysisStatus | string;
  model?: string | null;
  prompt_version: string;
  input_count: number;
  processed_count: number;
  success_count: number;
  needs_validation_count: number;
  failed_count: number;
  input_tokens?: number;
  output_tokens?: number;
  unknown_token_calls?: number;
  retry_count?: number;
  latency_ms?: number;
  attempt_count?: number;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string;
  error_message?: string | null;
}

export interface FeedbackAnalysisEntry {
  id: string;
  feedback_id: FeedbackId;
  run_id: string;
  feedback_type: string;
  module: string;
  intent: string;
  pain_point: string;
  sentiment: 'positive' | 'neutral' | 'negative' | string;
  severity: number;
  confidence: number;
  created_at?: string;
}

export interface InsightEvidenceEntry {
  id: string;
  insight_id: InsightId;
  feedback_id: FeedbackId;
  feedback_external_id: string;
  feedback_content: string;
  evidence_type: string;
  relevance_score: number;
  created_at?: string;
  feedback_created_at?: string | null;
  feedback_user_id?: string | null;
  /**
   * Phase 7 work package B1: raw ``feedbacks.source`` value surfaced here
   * so the Insight Detail page can show provenance alongside the raw
   * feedback content without a second round-trip. Older imports may
   * leave the column unset, in which case the field is null.
   */
  feedback_source?: string | null;
  /**
   * Phase 7 work package B1: deterministic AI classification for this
   * evidence row, sourced from
   * FeedbackAnalysis(feedback_id, run_id=insight.run.classification_run_id).
   * `null` means the backend had no analysis for that exact pair — the
   * page must render "AI Analysis N/A" rather than guess from another
   * run. We never silently fall back to a foreign run.
   */
  feedback_analysis?: FeedbackAnalysisEntry | null;
}

export interface InsightEvidenceResponse {
  insight_id: InsightId;
  items: InsightEvidenceEntry[];
  total: number;
  limit: number;
  offset: number;
}

/* ---------- Opportunity score / decision ---------- */

/**
 * OpportunityScore — Phase 4 snapshot shape.
 *
 * Mirrors backend/app/schemas/opportunity.py:OpportunityScoreRead.
 *
 * Five deterministic dimensions:
 *   user_coverage / severity / growth / user_value / business_value.
 *
 * Each dimension row carries:
 *   - raw_value / score: decimal strings (the backend ships ``str`` for
 *     ``Decimal``). The frontend never recomputes — it surfaces the
 *     snapshot verbatim so a later audit can match the LLM call.
 *   - available: false ⇒ the dimension was not measurable (e.g.
 *     coverage denominator empty) and ``score`` is ``null``. The UI
 *     must show "Not Available" with the snapshot's ``note`` rather
 *     than guess a value.
 *
 * weights.configured / weights.effective are two parallel ``Record<string,string>``
 * maps — the same five keys. The effective weights are renormalised
 * after unavailable dimensions are dropped; the frontend reads them
 * both but never recomputes.
 *
 * thresholds is another ``Record<string,string>`` keyed by priority
 * bucket (e.g. ``"ai_priority_p0_below"`` -> ``"7.5"``).
 *
 * ai_priority: free-text "P0"/"P1"/"P2" (or null for previews) — the
 * server's AI call decides; the frontend renders but never overrides.
 */
export interface OpportunityDimension {
  raw_value: string | null;
  score: string | null;
  available: boolean;
  affected_count?: number | null;
  project_count?: number | null;
  feedback_count?: number | null;
  average_severity?: string | null;
  recent_count?: number | null;
  previous_count?: number | null;
  note?: string | null;
}

export interface OpportunityScoreBreakdown {
  user_coverage: OpportunityDimension;
  severity: OpportunityDimension;
  growth: OpportunityDimension;
  user_value: OpportunityDimension;
  business_value: OpportunityDimension;
}

export interface OpportunityScoreWeights {
  configured: Record<string, string>;
  effective: Record<string, string>;
}

export interface OpportunityScore {
  id: OpportunityScoreId;
  insight_id: InsightId;
  project_id: ProjectId;
  classification_run_id: string;
  source_insight_status: string;
  source_snapshot_hash: string;
  breakdown: OpportunityScoreBreakdown;
  weights: OpportunityScoreWeights;
  thresholds: Record<string, string>;
  business_value: string | null;
  total_score: string | null;
  ai_priority: string | null;
  ai_reasoning: string;
  calculation_version: string;
  model?: string | null;
  prompt_version: string;
  input_tokens: number;
  output_tokens: number;
  unknown_token_calls: number;
  latency_ms: number;
  attempt_count: number;
  retry_count: number;
  created_at?: string;
}

export type DecisionAction =
  | 'set_business_value'
  | 'accept'
  | 'override'
  | 'defer'
  | 'reject';

/**
 * Override reason enum (Phase 4 backend literal).
 * Server-rejected as a 422 if any other string is sent.
 */
export type DecisionOverrideReason =
  | 'strategic_priority'
  | 'core_user_experience'
  | 'revenue_impact'
  | 'development_cost'
  | 'management_decision'
  | 'data_quality'
  | 'other';

/**
 * PM priority enum (Phase 4). The frontend has no P3 bucket.
 */
export type DecisionPriority = 'P0' | 'P1' | 'P2';

/**
 * DecisionPatchRequest — POST/PATCH body for /insights/{id}/decision.
 *
 * request_id: client-generated UUID used as an idempotency key. The
 *   server replays the same response on retry; mismatched
 *   expected_version returns 409 with a "please refresh" payload.
 *
 * The action enum drives state transitions: ``accept`` / ``override``
 * / ``defer`` / ``reject`` mutate status, while ``set_business_value``
 * updates BV without touching status.
 *
 * override_reason / override_note are required for ``override``; for
 * other actions they may be null.
 */
export interface DecisionPatchRequest {
  request_id: string;
  expected_version: number;
  action: DecisionAction;
  business_value?: number | null;
  pm_priority?: DecisionPriority | null;
  override_reason?: DecisionOverrideReason | null;
  override_note?: string | null;
}

/**
 * InsightDecision — current snapshot persisted on the insight row.
 *
 * Both ``ai_priority`` and ``pm_priority`` survive here: the AI
 * recommendation is preserved even after a PM override (so the UI can
 * render the "before/after" comparison without re-querying history).
 *
 * ``version`` starts at 0 and increments on every accepted patch; the
 * next PATCH must echo it back as ``expected_version``. Mismatches
 * surface as 409 and the page must refetch before retrying.
 */
export interface InsightDecision {
  id: string;
  insight_id: InsightId;
  project_id: ProjectId;
  current_score_id: OpportunityScoreId | null;
  business_value: string | null;
  ai_priority: string | null;
  pm_priority: string | null;
  /**
   * Decision status literal — the backend (Phase 4) uses:
   *   pending_review / accepted / overridden / deferred / rejected.
   * Older code may have used "pending"; the UI normalises this in
   * format helpers.
   */
  status:
    | 'pending_review'
    | 'pending'
    | 'accepted'
    | 'overridden'
    | 'deferred'
    | 'rejected'
    | string;
  override_reason: string | null;
  override_note: string | null;
  version: number;
  created_at?: string;
  updated_at?: string;
}

/**
 * DecisionHistoryEntry — append-only event row (Phase 4).
 *
 * event_type is the discriminator ("set_business_value" / "accept" /
 * "override" / "defer" / "reject"). The UI renders these as a
 * chronological audit list; before/after fields are read for inline
 * diff labels.
 */
export interface DecisionHistoryEntry {
  id: string;
  insight_id: InsightId;
  decision_id: string;
  score_id: OpportunityScoreId | null;
  request_id: string;
  event_type: DecisionAction | string;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  business_value: string | null;
  ai_priority: string | null;
  pm_priority: string | null;
  override_reason: string | null;
  override_note: string | null;
  version_after: number;
  created_at?: string;
}

/**
 * Paginated envelope used by every list endpoint that may grow over
 * time. Mirrors backend/app/schemas/* ``*Page`` schemas.
 *
 * ``total`` reflects the server-side count after any server-side filter
 * (e.g. ``?run_id=``) but BEFORE pagination. The frontend MUST NOT
 * rely on it as a "matched all rows" number — the list endpoint may
 * still be paginated and out-of-date if the user paginates back.
 */
export interface PageResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type DecisionHistoryPage = PageResponse<DecisionHistoryEntry>;
export type OpportunityScorePage = PageResponse<OpportunityScore>;

/* ---------- Requirement / PRD ----------
 *
 * Phase 7 work package B2 — these shapes mirror the real backend contract
 * from backend/app/schemas/requirement_prd.py. The previous frontend
 * types (title/description/acceptance_criteria/priority) were a B1-era
 * placeholder that did not match the persisted schema; any consumer
 * that still writes to those fields would silently send a 422.
 *
 * The frontend is read-mostly: PMs edit content via PATCH and may
 * transition status. They do NOT mutate the AI-authored snapshot
 * (model, prompt_version, source_snapshot_hash, etc.) — those fields
 * are surfaced verbatim so a later audit can match the LLM call.
 */

export type RequirementStatus =
  | 'draft'
  | 'reviewing'
  | 'approved'
  | 'rejected'
  | 'archived';

/* ---------- PMRequirementContent enum literals ----------
 *
 * These mirror backend/app/schemas/requirement_prd.py:
 *   MetricTypeLiteral = Literal["metric_definition", "baseline_only"]
 *   RiskCategoryLiteral = Literal["known", "speculative"]
 *
 * The READ DTOs (``SuccessMetricRead.type``, ``RiskRead.category``) keep
 * ``| string`` for forward-compatibility — the backend may add a new
 * enum member without a coordinated frontend release. The write DTO
 * (``RequirementContent``) intentionally stays strict so a typo'd
 * literal fails the form validator instead of leaking a 422 from the
 * backend on PATCH.
 *
 * The boundary lives in the requirement detail page:
 *   - On READ (buildContent), an unknown value falls back to a safe
 *     default (``metric_definition`` / ``speculative``) via a runtime
 *     guard, NEVER a blind ``as`` cast.
 *   - On WRITE, the select option lists are the only source of values,
 *     so PATCH bodies are guaranteed to be valid.
 */
export type MetricType = 'metric_definition' | 'baseline_only';
export type RiskCategory = 'known' | 'speculative';

export interface RequirementEvidenceRefRead {
  external_id: string;
  usage: string;
}

export interface RequirementSuccessMetricRead {
  name: string;
  // READ-side: backend may emit any string here for forward compat.
  // Frontend must map through a runtime guard before assigning to the
  // strict write-side ``MetricType``.
  type: string;
  sources: string[];
  note: string;
}

export interface RequirementRiskRead {
  category: string;
  description: string;
  evidence_ids: string[];
  mitigation: string;
}

export interface Requirement {
  id: RequirementId;
  project_id: ProjectId;
  insight_id: InsightId;
  decision_id: string;
  score_id: OpportunityScoreId | null;

  insight_status_snapshot: string;
  decision_status_snapshot: string;
  ai_priority_snapshot: string | null;
  pm_priority: 'P0' | 'P1' | 'P2' | string;
  pm_override_reason_snapshot: string | null;
  source_snapshot_hash: string;

  name: string;
  problem: string;
  target_user: string;
  user_story: string;
  solution_hypothesis: string;
  success_metrics: RequirementSuccessMetricRead[];
  risks: RequirementRiskRead[];
  evidence_references: RequirementEvidenceRefRead[];

  status: RequirementStatus | string;
  model: string | null;
  prompt_version: string;
  input_tokens: number;
  output_tokens: number;
  unknown_token_calls: number;
  latency_ms: number;
  attempt_count: number;
  retry_count: number;
  next_version: number;

  created_at?: string;
  updated_at?: string;
}

export interface RequirementPage {
  items: Requirement[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * PM-editable content block (mirrors backend
 * PMRequirementContent). Used as the ``content`` field of
 * RequirementPatchRequest. All fields are required by the backend
 * schema (min_length=1) so the form must surface a non-empty value
 * for each before allowing a PATCH.
 */
export interface RequirementContent {
  name: string;
  problem: string;
  target_user: string;
  user_story: string;
  solution_hypothesis: string;
  success_metrics: Array<{
    name: string;
    type: MetricType;
    sources?: string[];
    note?: string;
  }>;
  risks: Array<{
    category: RiskCategory;
    description: string;
    evidence_ids?: string[];
    mitigation?: string;
  }>;
  evidence_references: Array<{
    external_id: string;
    usage: string;
  }>;
}

export interface RequirementPatchRequest {
  status?: RequirementStatus;
  content?: RequirementContent;
}

/* ---------- PRD ----------
 *
 * Phase 7 work package C1 — full PRD contract that mirrors
 * backend/app/schemas/requirement_prd.py:PRDRead and friends. The
 * previous frontend `Prd` stub (id/requirement_id/version/status)
 * did NOT match the persisted 13-section PRD shape; consumers that
 * still write ``body: string`` to the create endpoint would silently
 * 422 because the backend reads ``content: PRDContent`` instead.
 *
 * The PRD body is structured, not free text. Every claim-shaped
 * section (``EvidenceBackedSection``, ``ProposedSolution``,
 * ``FunctionalRequirement``, ``EdgeCase``, ``AcceptanceCriterion``,
 * ``PRDRisk``, ``MVPScope``) carries an ``evidence_ids`` provenance
 * list so the AI client / validator cannot drop grounding silently.
 *
 * Read-side enum literals (``EdgeCaseCategoryLiteral``,
 * ``MetricTypeLiteral``, ``RiskCategoryLiteral``) are typed as the
 * strict union on the wire — the backend persists a literal and the
 * Read DTO mirrors it. New enum members added by the backend would
 * surface here as a TS compile error, which is the contract
 * intended by the schema authors (see ``RequirementContent`` for
 * the same pattern).
 */

export type PrdStatus = 'draft' | 'reviewing' | 'accepted' | 'archived';

export type EdgeCaseCategory =
  | 'empty_state'
  | 'failure'
  | 'retry'
  | 'partial_success'
  | 'invalid_input'
  | 'limit_exceeded';

/* ---- Inner structured items (PRD body) ---- */

export interface PrdEvidenceBackedSection {
  text: string;
  evidence_ids: string[];
}

export interface PrdProposedSolution {
  text: string;
  is_proposed: boolean;
  evidence_ids: string[];
}

export interface PrdFunctionalRequirement {
  text: string;
  evidence_ids: string[];
}

export interface PrdEdgeCase {
  category: EdgeCaseCategory;
  description: string;
  evidence_ids: string[];
}

export interface PrdAcceptanceCriterion {
  text: string;
  verifiable: boolean;
  note: string;
  evidence_ids: string[];
}

export interface PrdSuccessMetricBlock {
  name: string;
  type: MetricType;
  note: string;
  sources: string[];
}

export interface PrdRisk {
  category: RiskCategory;
  description: string;
  evidence_ids: string[];
  mitigation: string;
}

export interface PrdMvpScope {
  in_scope: string;
  out_of_scope: string;
}

/** Full 13-section PRD payload — mirrors backend ``PRDContent`` /
 *  ``PRDContentRead``. ``extra=forbid`` on the backend so unknown
 *  keys would 422 a POST. */
export interface PrdContent {
  background: PrdEvidenceBackedSection;
  problem: PrdEvidenceBackedSection;
  target_user: PrdEvidenceBackedSection;
  user_story: PrdEvidenceBackedSection;
  product_goal: PrdEvidenceBackedSection;
  proposed_solution: PrdProposedSolution;
  functional_requirements: PrdFunctionalRequirement[];
  user_flow: PrdEvidenceBackedSection;
  edge_cases: PrdEdgeCase[];
  acceptance_criteria: PrdAcceptanceCriterion[];
  success_metrics: PrdSuccessMetricBlock[];
  risks: PrdRisk[];
  mvp_scope: PrdMvpScope;
  evidence_sources: string[];
  evidence_ids: string[];
}

/* ---- Grounding warning ---- */

export interface PrdGroundingWarning {
  code: string;
  section: string | null;
  message: string;
  evidence_id: string | null;
}

/* ---- Read shape ---- */

/**
 * Read shape for one PRD version — mirrors backend ``PRDRead``.
 *
 * Includes AI/PM authoring lineage (``is_ai_generated_draft``,
 * ``model``, ``prompt_version``, ``input_tokens``, etc.) so the
 * page can render "AI generated" vs "PM edited" without a
 * follow-up round-trip. The frontend never recomputes these.
 */
export interface PrdRead {
  id: PrdId;
  requirement_id: RequirementId;
  project_id: ProjectId;
  insight_id: InsightId;

  version: number;
  status: PrdStatus | string;
  content: PrdContent;
  content_hash: string;

  is_ai_generated_draft: boolean;
  grounding_warnings: PrdGroundingWarning[];

  model: string | null;
  prompt_version: string;
  input_tokens: number;
  output_tokens: number;
  unknown_token_calls: number;
  latency_ms: number;
  attempt_count: number;
  retry_count: number;

  accepted_at: string | null;
  accepted_by: string | null;

  created_at?: string;
  updated_at?: string;
}

/* ---- Page envelopes ---- */

/**
 * Per-requirement PRD history. ``active_prd_id`` /
 * ``active_version`` are stable pointers to the highest-versioned
 * row, computed by the backend via a separate ``ORDER BY version
 * DESC LIMIT 1`` lookup so the field stays correct regardless of
 * pagination. Consumers MUST NOT derive the active PRD from the
 * current ``items`` slice — when ``offset`` skips past the newest
 * row, the page would otherwise treat an older draft as active.
 */
export interface PrdPage {
  total: number;
  items: PrdRead[];
  limit: number;
  offset: number;
  active_prd_id: PrdId | null;
  active_version: number | null;
}

/**
 * Project-scoped PRD index — Phase 7 D1 endpoint
 * ``GET /api/projects/{project_id}/prds``. Returns EVERY PRD
 * version from every requirement in the project (no latest-per-
 * requirement projection), sorted ``version DESC, id DESC``. The
 * frontend groups by ``requirement_id`` to render the version
 * timeline; collapsing to one row per requirement would silently
 * drop earlier drafts / accepted versions.
 */
export interface ProjectPrdPage {
  total: number;
  items: PrdRead[];
  limit: number;
  offset: number;
}

/* ---- Request bodies ---- */

/**
 * Body for ``POST /api/requirements/{requirement_id}/prds``.
 * ``content`` is optional — when absent, the AI client generates the
 * PRD; when present, the row is marked PM-authored.
 */
export interface PrdCreateRequest {
  content?: PrdContent;
}

/**
 * Body for ``PATCH /api/prds/{prd_id}``. PM may transition
 * ``draft`` → ``reviewing`` → ``archived`` here; ``accept`` lives on
 * the dedicated ``POST /prds/{id}/accept`` endpoint. Content edits
 * are only allowed on ``status ∈ {draft, reviewing}`` — accepted
 * versions are immutable on the backend.
 */
export interface PrdPatchRequest {
  status?: PrdStatus;
  content?: PrdContent;
}

/**
 * Body for ``POST /api/prds/{prd_id}/accept``. Empty by design —
 * ``extra=forbid`` on the backend rejects any field the client tries
 * to claim (Phase 5 has no user system, so ``accepted_by`` stays
 * ``NULL`` until a real auth layer exists).
 */
export type PrdAcceptRequest = Record<string, never>;

/* ---------- Evaluation (Phase 6) ----------
 *
 * Phase 7 D2a — read-only contract for the demo-dataset evaluation
 * surface. The frontend never POSTs an evaluation from this layer
 * (the runner is invoked from a Phase 1 / 2 detail page that owns
 * the upstream linkage); the shapes below mirror
 * backend/app/schemas/evaluation.py verbatim so a UI can render
 * the AI lineage and the dataset provenance marker
 * (``dataset_label_status``) without an extra round-trip.
 *
 * Distinguishing ``value: 0`` from ``value: null``:
 *
 *   The backend persists ``value`` as ``float | None``. A metric
 *   that was not measurable (empty denominator, label drift, …)
 *   ships ``value: null`` AND ``value_display: 'NOT MEASURED'``
 *   (NOT_MEASURED_DISPLAY). A metric that measured exactly zero
 *   ships ``value: 0`` AND ``value_display: '0.000000'``. The two
 *   states are NOT interchangeable — the UI MUST branch on
 *   ``value === null`` (or on ``value_display``) rather than
 *   falsy-check the number, otherwise a real zero would be
 *   rendered as "Not Measured" and the audit trail would silently
 *   drift from the database.
 *
 * ``failed_stage`` is a backend-derived convenience field on each
 * case row (``row.passed === false ? row.stage : null``); the UI
 * uses it to filter "failed at stage X" cases without recomputing
 * the predicate client-side.
 *
 * ``dataset_label_status`` MUST surface verbatim — the brief
 * requires the demo dataset provenance to be visible so a caller
 * can never silently treat it as ground-truth.
 */

/**
 * Evaluation type literal — mirrors backend
 * ``EvaluationTypeLiteral``. Marked ``| string`` for forward-compat
 * (same pattern as ``InsightStatus`` / ``PrdStatus``); the backend
 * still rejects unknown values at POST time.
 */
export type EvaluationType = 'classification_metrics' | 'cluster_metrics' | string;

/**
 * ``EvaluationRunRead`` — mirrors backend
 * :class:`EvaluationRunRead`. Every AI-authored field is preserved
 * verbatim (``model`` / ``prompt_version`` / ``pipeline_version``
 * / ``calculation_version`` / ``input_tokens`` / ``output_tokens``
 * / ``latency_ms`` / ``total_cost_usd`` / ``attempt_count`` /
 * ``retry_count``) so the UI can render "AI generated, version
 * v1.2.3, $0.0004 cost" without a follow-up round-trip.
 */
export interface EvaluationRunRead {
  id: string;
  project_id: ProjectId;

  evaluation_type: EvaluationType;
  stage: string;
  status: string;

  dataset_name: string;
  dataset_version: string;
  dataset_content_hash: string;
  dataset_provenance: string;
  dataset_split: string;
  /**
   * Provenance marker for the labels used by the demo dataset. The
   * backend always ships the literal
   * ``synthetic_reference_labels_unreviewed_no_human_review``
   * for the demo snapshot — the field is surfaced verbatim so the
   * UI can render a "Not Reviewed" badge without parsing
   * ``notes``.
   */
  dataset_label_status: string;

  model: string | null;
  prompt_version: string | null;
  pipeline_version: string | null;
  calculation_version: string;

  input_count: number;
  processed_count: number;
  success_count: number;
  failed_count: number;

  input_tokens: number | null;
  output_tokens: number | null;
  unknown_token_calls: number;
  latency_ms: number | null;
  total_cost_usd: number | null;

  attempt_count: number;
  retry_count: number;

  started_at: string | null;
  completed_at: string | null;
  created_at: string;

  error_message: string | null;
  notes: string | null;
}

/**
 * ``EvaluationMetricRead`` — one row from the per-run metric list.
 *
 * ``value`` is REQUIRED (not optional) because the backend always
 * ships a value field. ``value: null`` means "not measurable" —
 * the UI MUST render ``value_display`` verbatim to avoid collapsing
 * a real zero into "Not Measured".
 */
export interface EvaluationMetricRead {
  id: string;
  run_id: string;
  project_id: ProjectId;

  stage: string;
  metric_name: string;
  metric_kind: string;

  sample_count: number;
  value: number | null;
  value_display: string;
  details: Record<string, unknown>;

  created_at: string;
}

export interface EvaluationMetricPage {
  total: number;
  items: EvaluationMetricRead[];
  limit: number;
  offset: number;
}

/**
 * ``EvaluationCaseRead`` — one row from the per-run case list.
 *
 * Raw feedback text is deliberately omitted by the backend; only
 * SHA-256 fingerprints of ``input`` / ``expected`` / ``actual`` are
 * returned so a caller reading the audit trail never sees raw
 * content. ``failed_stage`` is the backend-derived convenience
 * field for the failure dashboard (``null`` for passed / unknown).
 */
export interface EvaluationCaseRead {
  id: string;
  run_id: string;
  project_id: ProjectId;

  case_id: string;
  stage: string;

  input_hash: string | null;
  expected_hash: string | null;
  actual_hash: string | null;

  passed: boolean | null;
  error_type: string | null;
  eval_source: string;
  notes: string | null;
  created_at: string;

  failed_stage: string | null;
}

export interface EvaluationCasePage {
  total: number;
  items: EvaluationCaseRead[];
  limit: number;
  offset: number;
}

/**
 * ``ProjectEvaluationRunPage`` — project-scoped EvaluationRun
 * index (Phase 7 D1 endpoint ``GET
 * /api/projects/{project_id}/evaluations``). Returns every
 * EvaluationRun owned by the project, sorted ``created_at DESC,
 * id DESC`` to match the backend route. The frontend does NOT
 * derive a stable pointer from this slice — pagination can
 * otherwise hide the latest row when the user paginates back.
 */
export interface ProjectEvaluationRunPage {
  total: number;
  items: EvaluationRunRead[];
  limit: number;
  offset: number;
}

/**
 * Query options shared by the per-run metric / case endpoints.
 *
 * ``project_id`` is OPTIONAL: when present, the backend enforces
 * cross-project isolation (a run that does not belong to the
 * project → 404, not 403). When absent, the run row is returned
 * unconditionally (single-user mode). The frontend ALWAYS passes
 * ``project_id`` when the active project context is known so a
 * multi-tenant deployment never lets the page render a foreign
 * project's run id.
 */
export interface EvaluationRunQuery extends Pagination {
  project_id?: ProjectId;
}

/* ---------- Error ---------- */

export interface ApiErrorBody {
  detail?: unknown;
  message?: string;
  code?: string;
}

export interface ApiError {
  status: number;
  statusText: string;
  url: string;
  method: string;
  body: ApiErrorBody | null;
  message: string;
  requestId: string;
}

export interface ApiResult<T> {
  data: T | null;
  error: ApiError | null;
  requestId: string;
}

/* ---------- Query option types ---------- */

export interface FeedbackQuery {
  limit?: number;
  offset?: number;
  // search/sort/user_type/source are NOT forwarded to the backend today; the
  // page layer is responsible for client-side filtering on the current page
  // and for surfacing that fact in the UI.
  search?: string;
  source?: string;
  user_type?: FeedbackUserType;
  sort?: 'created_at' | '-created_at' | 'external_id' | '-external_id';
}

export interface Pagination {
  limit: number;
  offset: number;
}
