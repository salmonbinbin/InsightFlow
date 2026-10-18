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

/**
 * Body for ``POST /api/insights/{insight_id}/requirement`` —
 * mirrors backend ``RequirementCreateRequest``. The backend
 * accepts an empty body and returns 201 with the freshly-
 * generated ``RequirementRead``; an optional
 * ``prompt_version`` pin (``requirement-generation-v2`` opts
 * into the revised PM-facing prompt) is forwarded verbatim.
 * The client intentionally does NOT take content fields
 * here: PM-editable fields are applied later via
 * ``patchRequirement``. Older callers that send ``{}`` are
 * preserved.
 */
export interface RequirementCreateRequest {
  prompt_version?: string | null;
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
 * ``prompt_version`` is an optional pin
 * (``prd-generation-v4`` opts into the revised PRD prompt);
 * the backend accepts an empty body and defaults to its
 * active prompt version, so older callers that send
 * ``{}`` or ``{ content: ... }`` continue to work.
 */
export interface PrdCreateRequest {
  content?: PrdContent;
  prompt_version?: string | null;
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

/* ---------- Research (Phase 8) ----------
 *
 * Phase 8 work package B — front-end contract for the Research Agent
 * (external market intelligence). Mirrors
 *   backend/app/schemas/research.py
 *   backend/app/models/research.py
 *
 * External Research is intentionally separated from the Internal Evidence
 * surface: the two live in different tables, the UI renders them in
 * different sections, and the API client never conflates them. An
 * Insight's Evidence list (feedback-driven) and its Research findings
 * (external search-driven) are NOT interchangeable — even when the
 * topic overlaps, a finding here is grounded (or explicitly marked
 * not-grounded) by a Tavily hit, never by raw customer feedback.
 *
 * Status invariant: ``ResearchRun.status`` always one of
 *   pending / running / completed / partial_failed / failed.
 * The ``running`` state is only observable in-process — the backend
 * commits the run row and finalises before the HTTP 200 returns, so
 * a real client never sees ``status === 'running'``. The frontend
 * surfaces a synthetic "Running" badge during the synchronous POST
 * window only, then re-reads from the server (which always reports
 * a terminal state).
 *
 * Finding status invariant: ``ResearchFinding.status`` is
 *   confirmed   — carries ≥1 ``source_refs`` whose provider_source_id
 *                 resolves to a persisted ``ResearchSource`` row.
 *   not_verified — carries no ``source_refs``. The DB CHECK
 *                 constraint rejects every combination that violates
 *                 this invariant before INSERT.
 *
 * ``summary`` category findings are always ``not_verified`` because
 * the LLM's headline ``summary`` is not tied to a single provider
 * source; the prompt pins the headline to the internal context alone.
 */

/** Run status literal — mirrors backend ``ResearchRunStatus`` enum. */
export type ResearchRunStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'partial_failed'
  | 'failed';

/** Finding category literal — mirrors backend ``ResearchFindingCategory``. */
export type ResearchFindingCategory =
  | 'summary'
  | 'competitor'
  | 'market_pattern'
  | 'opportunity'
  | 'risk';

/** Finding provenance literal — mirrors backend ``ResearchFindingStatus``. */
export type ResearchFindingStatus = 'confirmed' | 'not_verified';

/* ---- Phase 8 Research v6 — analysis support trace ----
 *
 * The v6 contract extends a finding with a ``claim_kind`` so
 * the page can distinguish a ground-truth ``fact`` (the LLM
 * cited a source) from an ``inference`` / ``recommendation``
 * the LLM authored ON TOP of the cited facts. An inference or
 * recommendation is ALWAYS treated as not-verified on the
 * page — even when every supporting fact carries citations,
 * the analytical layer is the LLM's own synthesis and the
 * audit boundary the brief mandates is "manual semantic
 * review required".
 *
 * The optional ``evidence_location`` is a positioning object
 * (url + section / paragraph / line range) so a reviewer can
 * confirm where in the cited source the fact is grounded.
 * All four v6 fields are OPTIONAL on the read shape so legacy
 * pre-v6 rows continue to type-check without forcing a
 * synchronous fixture rewrite.
 *
 * ``supporting_finding_ids`` is strictly the set of finding
 * ids the v6 analysis layer grounded on. The page never
 * follows a support id into a foreign run — the helper
 * filter is bounded to the currently-loaded findings, the
 * same run_id, ``claim_kind === 'fact'`` and
 * ``status === 'confirmed'``. Missing / unverified / foreign
 * ids are silently dropped rather than rendered as broken
 * links. */

export interface ResearchSourceRefRead {
  provider_source_id: string;
  url: string;
  title: string;
}

export interface ResearchEvidenceSummaryRead {
  external_id: string;
  summary: string;
}

/**
 * Read shape for ``research_runs.internal_context`` — the snapshot
 * the LLM saw during the run. The Insight title, pain point,
 * affected segment, recommendation, the first N evidence external_ids
 * with bounded per-entry summaries, the PM Decision snapshot and
 * the user-supplied research question / scope are echoed verbatim so
 * a reviewer can confirm what the LLM was grounded on.
 */
export interface ResearchInternalContextRead {
  insight_title: string;
  insight_pain_point: string;
  insight_affected_segment: string;
  insight_recommendation: string;
  evidence_external_ids: string[];
  evidence_summaries: ResearchEvidenceSummaryRead[];
  decision_status: string | null;
  decision_priority: string | null;
  research_question: string;
  scope: string | null;
}

/** Claim kind literal — mirrors backend v6
 *  ``ResearchFindingClaimKind`` enum. ``string & {}`` keeps
 *  the literal union open so a future backend addition
 *  compiles without a coordinated frontend release — the
 *  page renders any unknown value through a conservative
 *  fallback label rather than echoing the raw enum. */
export type ResearchClaimKind =
  | 'fact'
  | 'inference'
  | 'recommendation'
  | (string & {});

/** Exact saved-snapshot location, including matching-only section context.
 * Optional fields tolerate historical rows and future metadata additions. */
export interface ResearchFindingLocation {
  provider_source_id?: string;
  offset?: number;
  length?: number;
  text?: string;
  sha256?: string;
  first_failed_guard?: string;
  section?: string;
  context?: string;
  step_index?: number;
  step_total?: number;
  product_key?: string;
}

export interface ResearchFindingRead {
  id: string;
  run_id: string;
  category: ResearchFindingCategory | string;
  title: string;
  description: string;
  confidence: number | null;
  status: ResearchFindingStatus | string;
  /**
   * Resolved citations. Backend hydrates ``source_refs`` by
   * joining every persisted ``ResearchSource`` row referenced
   * in the raw JSON column, so the UI can render the URL + title
   * without a follow-up round-trip. ``null`` for not_verified
   * findings (the source-code invariant forbids entries on those
   * rows).
   */
  source_refs: ResearchSourceRefRead[] | null;
  created_at: string;
  /* Phase 8 Research v6 — analysis support trace. All
   * three fields are OPTIONAL so legacy pre-v6 rows
   * continue to type-check without a synchronous fixture
   * rewrite. The page only surfaces the new affordances
   * when ``claim_kind`` is present. */
  claim_kind?: ResearchClaimKind | null;
  supporting_finding_ids?: string[] | null;
  evidence_location?: ResearchFindingLocation | null;
}

export interface ResearchSourceRead {
  id: string;
  run_id: string;
  provider: string;
  provider_source_id: string;
  url: string;
  title: string | null;
  domain: string;
  snippet: string | null;
  retrieved_at: string;
  created_at: string;
}

/* ---------- Phase 8 Research v5 — task contract + coverage report ----------
 *
 * The backend's v5 ResearchRunRead carries two new bounded
 * payloads alongside the existing run metadata. Both are
 * nullable on legacy rows from before the migration; the
 * read shape MUST tolerate that so historical research runs
 * keep rendering with the existing honesty-only banner.
 *
 * The wire payload is typed ``dict`` in
 * backend/app/schemas/research.py:ResearchRunRead so the
 * page reads these as code-rule-level audit data only —
 * NOT a semantic review. The backend pins
 * ``coverage_report.semantic_review = 'NOT_MEASURED'`` to
 * mark the boundary; the page must never claim a run is
 * "fully answered" based on this report.
 *
 * The parse_status / coverage-status enums are modelled as
 * ``(string & {})`` rather than a strict literal union so
 * future backend additions land without a frontend rebuild.
 * The page renders only the documented set; anything
 * unknown falls through to a "需人工核对" boundary. */

export type ResearchTaskContractParseStatus =
  | 'parsed'
  | 'partial'
  | 'unparseable'
  | (string & {});

export interface ResearchTaskContractItem {
  /** Server-side requirement id; the coverage item of the
   *  same name references it. We never render this raw id
   *  to the user — the page maps it to ``value`` below for
   *  the human-readable label. */
  id: string;
  role: string;
  kind: string;
  /** The human-readable sub-question / scope / predicate
   *  text. This is the value the UI surfaces. */
  value: string;
  predicate_note: string | null;
}

export interface ResearchTaskContract {
  question: string;
  parse_status: ResearchTaskContractParseStatus;
  items: ResearchTaskContractItem[];
  parse_notes: string[];
}

export type ResearchCoverageItemStatus =
  | 'covered'
  | 'partial'
  | 'not_verified'
  | 'not_applicable'
  | (string & {});

export interface ResearchCoverageItem {
  requirement_id: string;
  role: string;
  kind: string;
  status: ResearchCoverageItemStatus;
  /** Internal evidence ids — not surfaced to the user. */
  evidence_ids: string[];
  /** Internal source ids — not surfaced to the user. */
  evidence_source_ids: string[];
  /** Short, human-readable reason for ``partial`` /
   *  ``not_verified``; empty for ``covered``. */
  gap: string | null;
}

export interface ResearchCoverageReport {
  task_question: string;
  parse_status: ResearchTaskContractParseStatus;
  items: ResearchCoverageItem[];
  procedure_status: string;
  /** Server-computed: a v5 ``completed`` run only lands when
   *  this is ``true``. The page reads it but does NOT
   *  collapse it into a "fully answered" verdict. */
  overall_completable: boolean;
  failure_reasons: string[];
  /** Pinned by the backend as ``'NOT_MEASURED'`` — the page
   *  must surface this honestly and never infer a real
   *  semantic audit from any other field on this report. */
  semantic_review: 'NOT_MEASURED' | string;
  /* Phase 8 v7 — discriminator fields. A v5 row leaves both
   * fields undefined. A v7 row carries
   *   version: 'research-support-v7'
   *   answer?: ResearchAnswerV7 | null
   * ``answer`` may be ``null`` on a v7 row whose planner
   * never produced a payload (planning failed, or the spec
   * never finished); the page detects the v7 contract from
   * ``version`` alone, NOT from the presence of ``answer``,
   * and falls back to a "v7 规划失败 / 未验证" honesty row
   * for the answer-less case.
   *
   * The fields are OPTIONAL so existing v5 fixtures and
   * pre-v5 mock objects can keep their current shape without
   * a synchronous rewrite. The page never reads v5 keys
   * (``items`` / ``failure_reasons`` / ``overall_completable``)
   * for a v7 row. */
  version?: string;
  answer?: ResearchAnswerV7 | null;
}

export interface ResearchRunRead {
  id: string;
  project_id: ProjectId;
  insight_id: InsightId;
  classification_run_id: string;
  clustering_run_id: string;
  insight_decision_id: string | null;
  research_question: string;
  scope: string | null;
  provider: string;
  provider_query: string;
  model: string | null;
  prompt_version: string;
  status: ResearchRunStatus | string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  error_message: string | null;
  internal_context: ResearchInternalContextRead;
  internal_context_hash: string;
  /* Phase 8 Research v5 — server-side task contract /
   * coverage report. All three are nullable on legacy rows
   * that pre-date the v5 migration; the page renders those
   * with the historical-only honesty banner. ``task_contract``
   * and ``coverage_report`` are bounded code-rule payloads —
   * they are NOT a semantic review. ``execution_trace`` is
   * opaque and is never displayed in the UI (it can carry
   * internal step ids).
   *
   * The fields are OPTIONAL (not just nullable) so existing
   * fixtures and pre-v5 mock objects can simply omit them
   * without breaking the type checker — the v5 migration
   * must not force a synchronous update of every test
   * fixture. */
  task_contract?: ResearchTaskContract | ResearchSpecV7 | null;
  coverage_report?: ResearchCoverageReport | ResearchSupportV7 | null;
  execution_trace?: unknown;
  input_count: number;
  processed_count: number;
  source_count: number;
  finding_count: number;
  input_tokens: number;
  output_tokens: number;
  unknown_token_calls: number;
  latency_ms: number;
  attempt_count: number;
  retry_count: number;
}

export interface ResearchRunListPage {
  total: number;
  items: ResearchRunRead[];
  limit: number;
  offset: number;
}

export interface ResearchSourceListPage {
  total: number;
  items: ResearchSourceRead[];
  limit: number;
  offset: number;
}

export interface ResearchFindingListPage {
  total: number;
  items: ResearchFindingRead[];
  limit: number;
  offset: number;
}

/* ---------- Tool Layer (Phase 8) ----------
 *
 * Phase 8 work package C — front-end contract for the simulated
 * external-tool surface. Mirrors
 *   backend/app/schemas/tool.py
 *   backend/app/services/tool_service.py
 *   backend/app/api/tools.py
 *
 * Wire rule: every per-run endpoint requires ``project_id`` as a
 * query parameter (the backend ``Query(...)`` decorator without a
 * default would 422 a missing value). The page always supplies the
 * active project id so a multi-tenant deployment never lets the
 * page render a foreign project's tool run.
 *
 * Frozen-snapshot rule: the page never re-derives the Title /
 * Description / Priority / Source-Insight preview. Those four
 * fields are server-derived at Prepare time and persisted on
 * ``ToolRunRead.request_payload`` (a ``ToolRunSnapshot``). Approve
 * re-uses the same snapshot — a later Requirement edit cannot
 * drift the audited content. The frontend surfaces the snapshot
 * verbatim so an audit can match what the simulator adapter saw.
 *
 * Idempotency rule: ``(project_id, request_id)`` is the unique key
 * for the Prepare call. A repeated Prepare with the same pair
 * returns the existing row (the API answers 201 first time, 200 on
 * duplicate) and the page treats both as success.
 *
 * State machine: ``awaiting_approval`` → ``cancelled`` (Cancel) or
 * → ``completed`` (Approve). Any other transition surfaces 409 and
 * the page renders a friendly "state conflict" error. A second
 * Approve on a completed run is a no-op (returns the existing row
 * without producing a duplicate audit row).
 */

/** Run status literal — mirrors backend ``ToolRunStatus`` enum. */
export type ToolRunStatus =
  | 'pending'
  | 'awaiting_approval'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** Frozen audit-visible snapshot persisted on
 *  ``ToolRunRead.request_payload`` at Prepare time. For
 *  ``simulated_issue_tracker`` the four server-derived fields
 *  describe the simulated external-issue ticket the simulator
 *  would create — the user's ``{}`` payload is intentionally
 *  ignored. The page renders this snapshot verbatim during the
 *  ``awaiting_approval`` window and again after Approve so a
 *  reviewer can confirm what the human approved. */
export interface ToolRunSnapshot {
  title: string;
  description: string;
  priority: string | null;
  source_insight_preview: string;
}

export interface ToolRunRead {
  id: string;
  project_id: ProjectId;
  requirement_id: RequirementId;
  agent_run_id: string | null;

  tool_name: string;
  tool_action: string;
  tool_version: string;
  request_id: string;

  status: ToolRunStatus | string;
  execution_mode: 'simulation' | 'real' | string;
  /**
   * Simulator-produced external id (``SIM-...``) surfaced on the
   * row after a successful Approve. ``null`` while the run is
   * still ``awaiting_approval`` / ``running``. The frontend never
   * constructs a real link from this — the brief mandates that
   * no real Jira / external URL is fabricated.
   */
  external_id: string | null;

  request_payload: ToolRunSnapshot;
  response_summary: string | null;
  failure_category: string | null;

  attempt_count: number;
  retry_count: number;

  created_at: string;
  updated_at: string;
  approved_at: string | null;
  completed_at: string | null;
}

/** Paginated tool-run history scoped to one Requirement. The
 *  ``active_run_id`` pointer is the awaiting_approval run if one
 *  exists, otherwise the most recent non-cancelled run. The page
 *  MUST NOT derive the active pointer from the current ``items``
 *  slice because pagination can skip past the newest row. */
export interface ToolRunPage {
  total: number;
  items: ToolRunRead[];
  limit: number;
  offset: number;
  active_run_id: string | null;
}

/** Body for ``POST /requirements/{requirement_id}/tool-runs``.
 *  ``request_id`` is REQUIRED and must be UUID-shaped; the service
 *  layer uses ``(project_id, request_id)`` as the idempotency key
 *  for the Prepare call. ``payload`` is empty for the simulator
 *  (``extra=forbid`` on the registered input schema rejects any
 *  injected field). The page sends a fresh UUID per Prepare
 *  click so each user-driven Prepare is a new row. */
export interface ToolPrepareRequest {
  tool_name: string;
  tool_action: string;
  request_id: string;
  payload: Record<string, never>;
  agent_run_id?: string | null;
}

/** Body for ``POST /tool-runs/{tool_run_id}/cancel``. Empty by
 *  design (``extra=forbid``); any field the client tries to claim
 *  is rejected with 422. */
export type ToolCancelRequest = Record<string, never>;

/** Body for ``POST /tool-runs/{tool_run_id}/approve``. Empty by
 *  design (``extra=forbid``); the Approve endpoint is a single
 *  explicit acknowledgement. */
export type ToolApproveRequest = Record<string, never>;

/**
 * Body for ``POST /api/insights/{insight_id}/research`` —
 * mirrored verbatim on the backend (``extra="forbid"``); an
 * unknown key would 422 the request.
 *
 * Length caps mirror the DB CHECK constraints and the LLM prompt
 * contract: ``research_question`` is 1..500 chars, ``scope`` is
 * optional but ≤2000. ``prompt_version`` is the explicit prompt
 * pin; the page sends ``research-agent-v7`` to opt into the
 * v7 extractive contract, while older callers may still
 * leave it unset and let the backend default to v1.
 */
export interface ResearchRunRequest {
  research_question: string;
  scope?: string | null;
  prompt_version?: string | null;
}

/* ---------- Phase 8 Research v7 — extractive answer contract ----------
 *
 * Phase 8 v7 (research-agent-v7) replaces the v5 / v6
 * "LLM-authored, post-hoc audited" model with a strict
 * extractive contract. The wire payload is rooted at
 * ``coverage_report.answer`` for a v7 run; the rest of
 * ``coverage_report`` retains the v5 / v6 keys so the
 * existing page rendering still resolves. The page
 * detects the v7 contract by reading
 * ``coverage_report.answer.version === 'research-answer-v7'``
 * and routes the panel into the v7 renderer.
 *
 * Three typed sub-contracts with strict version
 * discriminators so the page can refuse to render a row
 * whose ``version`` does not match (and so a future v8
 * migration can land without a silent fallback):
 *
 *   ResearchSpecV7     — research-spec-v7   — the planning
 *                        payload. The page surfaces only
 *                        the user-visible subset
 *                        (original_question, requirements
 *                        with question_text + kind,
 *                        unresolved clauses).
 *   ResearchSupportV7  — research-support-v7 — per-requirement
 *                        status / reason_codes /
 *                        answer_polarity / adopted_attempt.
 *                        The page reads from ``answer.items``
 *                        which already merges this in.
 *   ResearchAnswerV7   — research-answer-v7 — the user-visible
 *                        payload. Items carry
 *                        question / kind / status /
 *                        answer_polarity / quotes[] / gap /
 *                        adopted_attempt. ``analyses[]``
 *                        carries the per-facet inference /
 *                        recommendation derivation; each
 *                        row exposes ``supporting_finding_ids``
 *                        so the page can expand a row to its
 *                        cited fact premises.
 *
 * The v7 contract pins ``semantic_review = 'NOT_MEASURED'``;
 * the page MUST continue to render the manual-semantic-
 * review boundary exactly as it does for v5 / v6.
 */

/** Version discriminator — every v7 sub-contract pins this
 *  literal. ``string & {}`` keeps the literal union narrow
 *  while still letting the runtime check reject a row whose
 *  ``version`` is something else. */
export type ResearchV7Version =
  | 'research-spec-v7'
  | 'research-support-v7'
  | 'research-answer-v7'
  | 'research-analysis-v7'
  | 'research-fact-v7'
  | 'research-execution-v7'
  | (string & {});

export interface ResearchSpecRequirementV7 {
  id: string;
  anchor_ids: string[];
  kind: string;
  products: string[];
  platform: string | null;
  entry: string | null;
  action: string | null;
  object: string | null;
  mode: string;
  quantity: string;
  condition_quotes: string[];
  version: string | null;
  analysis_facets: string[];
  /** Derived from the anchors joined back to the original
   *  question; this is the user-visible question for the
   *  requirement. The page surfaces this verbatim. */
  question_text: string;
  mandatory_quantity: string;
  mandatory_conditions: string[];
}

export interface ResearchSpecAnchorV7 {
  id: string;
  start: number;
  end: number;
  text: string;
}

export interface ResearchSpecPolicySpanV7 {
  start: number;
  end: number;
  text: string;
}

export interface ResearchSpecUnresolvedV7 {
  id: string;
  text: string;
  reason: string;
}

export interface ResearchSpecV7 {
  version: 'research-spec-v7';
  original_question: string;
  original_scope: string | null;
  policy_spans: ResearchSpecPolicySpanV7[];
  anchors: ResearchSpecAnchorV7[];
  requirements: ResearchSpecRequirementV7[];
  unresolved: ResearchSpecUnresolvedV7[];
  queries: Array<{ id: string; text: string }>;
  fingerprint: string;
}

/** A single cited fact (extractive premise). The page must
 *  render this with the original source quote ``text`` and
 *  the ``provider_source_id`` / ``url`` as the citation —
 *  it MUST NOT promote the internal ``evidence_id`` to a
 *  visible title (the spec warns that the model can write
 *  arbitrary titles, so only the saved span text is honest). */
export interface ResearchAnswerQuoteV7 {
  finding_id: string;
  evidence_id: string;
  text: string;
  provider_source_id: string;
  url: string;
}

export type ResearchAnswerItemStatusV7 =
  | 'covered'
  | 'partial'
  | 'not_verified'
  | (string & {});

export type ResearchAnswerPolarityV7 =
  | 'positive'
  | 'negative'
  | 'conflicting'
  | 'unknown'
  | (string & {});

/** One per-requirement row. ``quotes`` is the set of
 *  saved fact spans the run actually grounded on; ``gap``
 *  is a server-derived code string (e.g. ``evidence_missing``)
 *  the page renders through a conservative Chinese label. */
export interface ResearchVersionConstraintV7 {
  platform: string;
  relation: 'at_least' | 'at_most' | 'required' | 'earlier_than' | 'later_than';
  version: string;
  evidence_ids: string[];
  product_key?: string;
}

export interface ResearchAnswerItemV7 {
  requirement_id: string;
  question: string;
  kind: string;
  status: ResearchAnswerItemStatusV7;
  answer_polarity: ResearchAnswerPolarityV7;
  quotes: ResearchAnswerQuoteV7[];
  gap: string;
  adopted_attempt: number | null;
  version_constraints?: ResearchVersionConstraintV7[];
  conflict_groups?: Array<{ attempt: number | null; evidence_ids: string[]; version_constraints?: ResearchVersionConstraintV7[] }>;

}

/** An analysis derivation — inference or recommendation on
 *  top of confirmed fact premises. The page distinguishes
 *  ``claim_kind: 'inference' | 'recommendation'`` so the
 *  user never mistakes the LLM's own synthesis for a
 *  cited fact. ``premises`` is the per-facet binding
 *  back to the requirements; ``supporting_finding_ids``
 *  is the matching list of fact ids so the user can
 *  expand the row and see the cited premises. */
export interface ResearchAnalysisPremiseV7 {
  requirement_id: string;
  evidence_ids: string[];
}

export interface ResearchAnalysisV7 {
  finding_id: string;
  title: string;
  description: string;
  claim_kind: 'inference' | 'recommendation' | (string & {});
  supporting_finding_ids: string[];
  /* Carried verbatim from the backend analysis meta. The
   * page never claims this row is semantically audited;
   * ``semantic_review`` is pinned to ``'NOT_MEASURED'``. */
  version?: string;
  requirement_id?: string;
  facet?: string;
  operator?: string;
  premises?: ResearchAnalysisPremiseV7[];
  scenario?: string | null;
  proposal?: string | null;
  semantic_review?: 'NOT_MEASURED' | string;
}

export interface ResearchAnswerV7 {
  version: 'research-answer-v7' | (string & {});
  items: ResearchAnswerItemV7[];
  unresolved: ResearchSpecUnresolvedV7[];
  analyses: ResearchAnalysisV7[];
  semantic_review: 'NOT_MEASURED' | string;
}

/** Type guard for the v7 answer. A coverage_report may
 *  carry a v7 answer (``coverage_report.answer``) or a v5
 *  / v6 audit payload (``coverage_report.items``,
 *  ``coverage_report.overall_completable``). The page uses
 *  this guard to pick the renderer without ever poking
 *  at the old keys for a v7 run. */
export function isResearchAnswerV7(
  answer: unknown,
): answer is ResearchAnswerV7 {
  if (!answer || typeof answer !== 'object') return false;
  const v = (answer as { version?: unknown }).version;
  return v === 'research-answer-v7';
}

/* ---------- v7 coverage envelope ----------
 *
 * Phase 8 v7 wires the ``coverage_report`` discriminator at
 * the envelope level (``coverage_report.version ===
 * 'research-support-v7'``). The envelope carries per-
 * requirement support verdicts (the ``evaluate_support``
 * ledger), a list of rejected evidence ids, an
 * ``overall_completable`` summary flag, and an OPTIONAL
 * ``answer`` payload. A planning-failed v7 run leaves
 * ``answer`` null — the page must NOT use ``answer``
 * presence to discriminate v5 vs v7; only ``version``
 * does. */

export type ResearchSupportItemStatusV7 =
  | 'covered'
  | 'partial'
  | 'not_verified'
  | (string & {});

export type ResearchSupportPolarityV7 =
  | 'positive'
  | 'negative'
  | 'conflicting'
  | 'unknown'
  | (string & {});

/** One per-requirement verdict from the v7
 *  ``evaluate_support`` ledger. ``reason_codes`` are the
 *  closed-code support rejections; ``gap`` is the
 *  ``;``-joined code string the page already maps through
 *  ``gapLabel``. ``adopted_attempt`` records which pipeline
 *  attempt the page should cite (``采用第 N 轮``); ``null``
 *  when the run never adopted a verdict for this row. */
export interface ResearchSupportV7Item {
  requirement_id: string;
  status: ResearchSupportItemStatusV7;
  evidence_ids: string[];
  reason_codes: string[];
  gap: string;
  role: string;
  answer_polarity: ResearchSupportPolarityV7;
  adopted_attempt: number | null;
  version_constraints?: ResearchVersionConstraintV7[];
  conflict_groups?: Array<{ attempt: number | null; evidence_ids: string[]; version_constraints?: ResearchVersionConstraintV7[] }>;

  /** Only present when two covered attempts disagree on
   *  polarity and the ledger demoted the row to
   *  ``conflicting``. The page surfaces it as audit
   *  context, never as a user verdict. */
  conflicting_attempts?: number[] | null;
}

/** The v7 ``coverage_report`` envelope. A row is v7 iff
 *  ``version === 'research-support-v7'``; ``answer`` is
 *  optional (a planning-failed run leaves it ``null``).
 *  The page reads ``answer.items`` / ``answer.analyses``
 *  via the ``ResearchAnswer`` renderer when ``answer`` is
 *  present, and renders the v7 honesty row otherwise. */
export interface ResearchSupportV7 {
  version: 'research-support-v7';
  items: ResearchSupportV7Item[];
  rejected_ids?: string[];
  overall_completable: boolean;
  answer?: ResearchAnswerV7 | null;
}

/** Type guard for the v7 coverage envelope. The page uses
 *  it to decide whether to render the v7 renderer (with or
 *  without an ``answer``) or fall back to the v5 / v6
 *  evidence-coverage section. */
export function isResearchSupportV7(
  report: unknown,
): report is ResearchSupportV7 {
  if (!report || typeof report !== 'object') return false;
  return (report as { version?: unknown }).version === 'research-support-v7';
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
