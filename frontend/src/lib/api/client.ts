import type { ReferenceTemplate, ReferenceReviewPage } from './reference-types';
/**
 * InsightFlow — typed API client
 *
 * Single entry point for talking to the FastAPI backend through the Next.js
 * rewrite (/api/* -> ${NEXT_PUBLIC_API_BASE_URL}/*). All HTTP calls happen
 * via fetch() and surface a discriminated { data, error, requestId } tuple so
 * callers can branch on success without try/catch noise.
 *
 * Conventions:
 *   - List responses are returned as paginated typed objects (see types.ts).
 *   - 2xx with empty body is normalised to {} on data=null to avoid confusion.
 *   - Non-2xx responses are parsed into ApiError; raw body is preserved on
 *     ApiError.body for diagnostics.
 *   - requestId is supplied by the server when present (X-Request-ID) and
 *     generated client-side otherwise, so every error has a stable identifier
 *     for cross-checking server logs.
 */

import type {
  AnalysisResultsResponse,
  AnalysisRun,
  AnalysisRunId,
  AnalysisRunListQuery,
  ApiError,
  ApiResult,
  ClusterRun,
  ClusterRunId,
  DecisionHistoryEntry,
  DecisionHistoryPage,
  DecisionPatchRequest,
  EvaluationCasePage,
  EvaluationCreateRequest,
  EvaluationMetricPage,
  EvaluationRunQuery,
  EvaluationRunRead,
  Feedback,
  FeedbackAnalysisEnvelope,
  FeedbackId,
  FeedbackImportSummary,
  FeedbackListResponse,
  FeedbackQuery,
  Insight,
  InsightDecision,
  InsightEvidenceResponse,
  InsightId,
  InsightRun,
  InsightRunId,
  OpportunityScore,
  OpportunityScoreId,
  OpportunityScorePage,
  PageResponse,
  Pagination,
  PrdCreateRequest,
  RequirementCreateRequest,
  PrdId,
  PrdPage,
  PrdPatchRequest,
  PrdRead,
  Project,
  ProjectAnalysisRunPage,
  ProjectCreate,
  ProjectId,
  ProjectEvaluationRunPage,
  ProjectInsightRunPage,
  ProjectPrdPage,
  Requirement,
  RequirementId,
  RequirementPage,
  RequirementPatchRequest,
  ResearchFindingListPage,
  ResearchFindingRead,
  ResearchRunListPage,
  ResearchRunRead,
  ResearchRunRequest,
  ResearchSourceListPage,
  ResearchSourceRead,
  ToolApproveRequest,
  ToolCancelRequest,
  ToolPrepareRequest,
  ToolRunPage,
  ToolRunRead,
} from './types';

let requestCounter = 0;
function nextRequestId(): string {
  requestCounter += 1;
  return `req_${Date.now().toString(36)}_${requestCounter}`;
}

export interface InsightFlowClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

export class InsightFlowClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: InsightFlowClientOptions = {}) {
    // The browser MUST hit the same-origin `/api/*` path. The Next.js rewrite
    // (next.config.mjs) is what forwards those calls to the FastAPI backend
    // — going direct to the backend from the browser would skip the rewrite
    // and trip the backend's missing CORS configuration.
    //
    // `NEXT_PUBLIC_API_BASE_URL` is therefore NOT read by the client at
    // runtime; it is a build-time target for the rewrite only.
    //
    // URL construction:
    //   - Every per-method `path` already starts with `/api/...`.
    //   - Default baseUrl is `''` so the browser request resolves to
    //     `https://<host>/api/projects` (same-origin). Using `/api` here
    //     would produce `/api/api/projects` — a real bug we just fixed.
    //   - An explicit absolute baseUrl (e.g. `http://api.local`) is used as
    //     the origin; the per-method `/api/...` is appended.
    //   - An explicit baseUrl that already ends in `/api` (e.g.
    //     `http://api.local/api`) has the trailing `/api` stripped so the
    //     per-method path is not duplicated.
    const explicit = options.baseUrl;
    if (explicit) {
      this.baseUrl = explicit.replace(/\/$/, '').replace(/\/api$/, '');
    } else {
      this.baseUrl = '';
    }
    if (options.fetchImpl) {
      this.fetchImpl = options.fetchImpl;
    } else if (typeof fetch !== 'undefined') {
      // The native ``fetch`` is a host method that requires its
      // receiver to be ``Window`` (or ``WorkerGlobalScope``).
      // Storing the unbound global here and later calling
      // ``this.fetchImpl(...)`` re-binds ``this`` to the client
      // instance, which the browser refuses with "Illegal
      // invocation" and bricks every API request.
      //
      // Wrap the global in an arrow so ``fetch`` is invoked as a
      // free function. Property-access on ``globalThis`` keeps the
      // receiver ``globalThis`` itself, which matches the native
      // contract regardless of how the caller invokes the wrapper.
      // The arrow introduces NO new behaviour for URL / headers /
      // body / error parsing — ``request`` keeps doing
      // ``this.fetchImpl(fullUrl, { method, headers, body, signal })``
      // exactly as before.
      this.fetchImpl = ((
        url: RequestInfo | URL,
        init?: RequestInit,
      ) => fetch(url, init)) as unknown as typeof fetch;
    } else {
      this.fetchImpl = (() => {
        throw new Error('fetch is not available in this environment');
      }) as unknown as typeof fetch;
    }
  }

  /* ---------- private helpers ---------- */

  private async request<T>(
    method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
    path: string,
    init?: { query?: Record<string, unknown>; body?: unknown; signal?: AbortSignal },
  ): Promise<ApiResult<T>> {
    const requestId = nextRequestId();
    const url = new URL(this.baseUrl + path, 'http://placeholder.local');
    if (init?.query) {
      for (const [key, value] of Object.entries(init.query)) {
        if (value === undefined || value === null) continue;
        url.searchParams.set(key, String(value));
      }
    }
    const fullUrl = url.toString().replace('http://placeholder.local', '');
    const headers: Record<string, string> = {
      'X-Request-ID': requestId,
      Accept: 'application/json',
    };
    let body: BodyInit | undefined;
    if (init?.body instanceof FormData) {
      body = init.body;
    } else if (init?.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(init.body);
    }
    try {
      const resp = await this.fetchImpl(fullUrl, {
        method,
        headers,
        body,
        signal: init?.signal,
      });
      const headerRequestId = resp.headers.get('X-Request-ID') || requestId;
      if (!resp.ok) {
        const errBody = await safeParseJson(resp);
        const error: ApiError = {
          status: resp.status,
          statusText: resp.statusText,
          url: fullUrl,
          method,
          body: errBody,
          message: deriveErrorMessage(resp.status, errBody),
          requestId: headerRequestId,
        };
        return { data: null, error, requestId: headerRequestId };
      }
      if (resp.status === 204) {
        return { data: null, error: null, requestId: headerRequestId };
      }
      const data = (await resp.json()) as T;
      return { data, error: null, requestId: headerRequestId };
    } catch (e) {
      const error: ApiError = {
        status: 0,
        statusText: 'NetworkError',
        url: fullUrl,
        method,
        body: null,
        message: e instanceof Error ? e.message : String(e),
        requestId,
      };
      return { data: null, error, requestId };
    }
  }

  /* ---------- Projects ---------- */

  listProjects(signal?: AbortSignal): Promise<ApiResult<Project[]>> {
    return this.request<Project[]>('GET', '/api/projects', { signal });
  }

  createProject(
    body: ProjectCreate,
    signal?: AbortSignal,
  ): Promise<ApiResult<Project>> {
    return this.request<Project>('POST', '/api/projects', { body, signal });
  }

  getProject(
    id: ProjectId,
    signal?: AbortSignal,
  ): Promise<ApiResult<Project>> {
    return this.request<Project>('GET', `/api/projects/${id}`, { signal });
  }

  /* ---------- Feedback ---------- */

  listFeedback(
    projectId: ProjectId,
    query: FeedbackQuery = {},
    signal?: AbortSignal,
  ): Promise<ApiResult<FeedbackListResponse>> {
    const page = normalisePage(query);
    // Backend's list endpoint only supports limit/offset today. Search / sort /
    // user_type / source are intentionally NOT forwarded — the page layer is
    // responsible for surfacing that fact in the UI (we keep the param shape
    // here so the page can still express "user wants to filter" without
    // pretending the server is doing it).
    return this.request<FeedbackListResponse>(
      'GET',
      `/api/projects/${projectId}/feedback`,
      {
        query: {
          limit: page.limit,
          offset: page.offset,
        },
        signal,
      },
    );
  }

  importFeedback(
    projectId: ProjectId,
    file: File,
    signal?: AbortSignal,
  ): Promise<ApiResult<FeedbackImportSummary>> {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return this.request<FeedbackImportSummary>(
      'POST',
      `/api/projects/${projectId}/feedback/import`,
      { body: fd, signal },
    );
  }

  // GET /api/projects/{project_id}/feedback/{feedback_id}/analysis?run_id=
  // — Phase 7 E read-only per-feedback AI classification selection.
  // ``run_id`` is optional; when supplied, the backend restricts the
  // lookup to that single classification run. The response envelope
  // carries ``feedback_id`` (echoes the path id) and
  // ``analysis: FeedbackAnalysisRead | null`` so the page can verify
  // the response is for the currently open drawer and render
  // "AI Analysis N/A" when no analysis row exists.
  getFeedbackAnalysis(
    projectId: ProjectId,
    feedbackId: FeedbackId,
    query: { run_id?: AnalysisRunId } = {},
    signal?: AbortSignal,
  ): Promise<ApiResult<FeedbackAnalysisEnvelope>> {
    const q: Record<string, unknown> = {};
    if (query.run_id !== undefined) q.run_id = query.run_id;
    return this.request<FeedbackAnalysisEnvelope>(
      'GET',
      `/api/projects/${projectId}/feedback/${feedbackId}/analysis`,
      { query: q, signal },
    );
  }

  /* ---------- Classification ---------- */

  createAnalysis(
    projectId: ProjectId,
    signal?: AbortSignal,
  ): Promise<ApiResult<AnalysisRun>> {
    return this.request<AnalysisRun>(
      'POST',
      `/api/projects/${projectId}/analysis`,
      { signal },
    );
  }

  getAnalysis(
    runId: AnalysisRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<AnalysisRun>> {
    return this.request<AnalysisRun>('GET', `/api/analysis/${runId}`, { signal });
  }

  retryAnalysis(
    runId: AnalysisRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<AnalysisRun>> {
    return this.request<AnalysisRun>(
      'POST',
      `/api/analysis/${runId}/retry`,
      { signal },
    );
  }

  // GET /api/projects/{project_id}/analysis-runs?run_type=&limit=&offset=
  // — Phase 7 D project-scoped AnalysisRun history. The list
  // endpoint encodes the project in the path, so no
  // ``project_id`` query is required; ``run_type`` is optional
  // and only accepts the two values the runner emits
  // (``classification`` / ``clustering``) per the backend
  // Literal type.
  listProjectAnalysisRuns(
    projectId: ProjectId,
    query: AnalysisRunListQuery = { limit: 50, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<ProjectAnalysisRunPage>> {
    const q: Record<string, unknown> = {
      limit: query.limit,
      offset: query.offset,
    };
    if (query.run_type !== undefined) q.run_type = query.run_type;
    return this.request<ProjectAnalysisRunPage>(
      'GET',
      `/api/projects/${projectId}/analysis-runs`,
      { query: q, signal },
    );
  }

  getAnalysisResults(
    runId: AnalysisRunId,
    query: Pagination,
    signal?: AbortSignal,
  ): Promise<ApiResult<AnalysisResultsResponse>> {
    return this.request<AnalysisResultsResponse>(
      'GET',
      `/api/analysis/${runId}/results`,
      {
        query: { limit: query.limit, offset: query.offset },
        signal,
      },
    );
  }

  /* ---------- Clustering ---------- */

  // POST /projects/{id}/clustering does not accept a body in the current
  // backend contract — the runner picks the latest classification run for the
  // project. We keep the analysisRunId argument only so the page can record
  // the upstream linkage locally; it is intentionally NOT sent to the server.
  createClustering(
    projectId: ProjectId,
    _analysisRunId: AnalysisRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<ClusterRun>> {
    return this.request<ClusterRun>(
      'POST',
      `/api/projects/${projectId}/clustering`,
      { signal },
    );
  }

  getClustering(
    runId: ClusterRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<ClusterRun>> {
    return this.request<ClusterRun>(
      'GET',
      `/api/clustering/${runId}`,
      { signal },
    );
  }

  retryClustering(
    runId: ClusterRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<ClusterRun>> {
    return this.request<ClusterRun>(
      'POST',
      `/api/clustering/${runId}/retry`,
      { signal },
    );
  }

  /* ---------- Insights ---------- */

  generateInsights(
    projectId: ProjectId,
    clusteringRunId: ClusterRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<InsightRun>> {
    return this.request<InsightRun>(
      'POST',
      `/api/projects/${projectId}/insights/generate`,
      { body: { clustering_run_id: clusteringRunId }, signal },
    );
  }

  getInsightRun(
    runId: InsightRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<InsightRun>> {
    return this.request<InsightRun>(
      'GET',
      `/api/insight-runs/${runId}`,
      { signal },
    );
  }

  retryInsightRun(
    runId: InsightRunId,
    signal?: AbortSignal,
  ): Promise<ApiResult<InsightRun>> {
    return this.request<InsightRun>(
      'POST',
      `/api/insight-runs/${runId}/retry`,
      { signal },
    );
  }

  // GET /api/projects/{project_id}/insight-runs?limit=&offset=
  // — Phase 7 D project-scoped InsightRun history. The list
  // endpoint is the read-only companion of
  // ``GET /insight-runs/{run_id}``; the project is encoded in
  // the path so no ``project_id`` query is needed.
  listProjectInsightRuns(
    projectId: ProjectId,
    query: Pagination = { limit: 50, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<ProjectInsightRunPage>> {
    return this.request<ProjectInsightRunPage>(
      'GET',
      `/api/projects/${projectId}/insight-runs`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  listProjectInsights(
    projectId: ProjectId,
    query: Pagination = { limit: 25, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<{ items: Insight[]; total: number; limit: number; offset: number }>> {
    return this.request<{ items: Insight[]; total: number; limit: number; offset: number }>(
      'GET',
      `/api/projects/${projectId}/insights`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  getInsight(
    insightId: InsightId,
    signal?: AbortSignal,
  ): Promise<ApiResult<Insight>> {
    return this.request<Insight>(
      'GET',
      `/api/insights/${insightId}`,
      { signal },
    );
  }

  getInsightEvidence(
    insightId: InsightId,
    query: Pagination = { limit: 25, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<InsightEvidenceResponse>> {
    return this.request<InsightEvidenceResponse>(
      'GET',
      `/api/insights/${insightId}/evidence`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  /* ---------- Opportunity score / decision ---------- */

  getOpportunityScore(
    insightId: InsightId,
    signal?: AbortSignal,
  ): Promise<ApiResult<OpportunityScore>> {
    return this.request<OpportunityScore>(
      'GET',
      `/api/insights/${insightId}/opportunity-score`,
      { signal },
    );
  }

  createOpportunityScore(
    insightId: InsightId,
    signal?: AbortSignal,
  ): Promise<ApiResult<OpportunityScore>> {
    return this.request<OpportunityScore>(
      'POST',
      `/api/insights/${insightId}/opportunity-score`,
      { signal },
    );
  }

  listInsightOpportunityScores(
    insightId: InsightId,
    query: Pagination,
    signal?: AbortSignal,
  ): Promise<ApiResult<OpportunityScorePage>> {
    return this.request<OpportunityScorePage>(
      'GET',
      `/api/insights/${insightId}/opportunity-scores`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  getDecision(
    insightId: InsightId,
    signal?: AbortSignal,
  ): Promise<ApiResult<InsightDecision>> {
    return this.request<InsightDecision>(
      'GET',
      `/api/insights/${insightId}/decision`,
      { signal },
    );
  }

  patchDecision(
    insightId: InsightId,
    body: DecisionPatchRequest,
    signal?: AbortSignal,
  ): Promise<ApiResult<InsightDecision>> {
    return this.request<InsightDecision>(
      'PATCH',
      `/api/insights/${insightId}/decision`,
      { body, signal },
    );
  }

  getDecisionHistory(
    insightId: InsightId,
    query: Pagination = { limit: 50, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<DecisionHistoryPage>> {
    return this.request<DecisionHistoryPage>(
      'GET',
      `/api/insights/${insightId}/decision/history`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  /* ---------- Requirement / PRD ---------- */

  // Phase 7 B2: project-level list — the backend exposes
  // GET /api/projects/{project_id}/requirements?limit&offset. The previous
  // /api/insights/{insight_id}/requirements endpoint was the B1 placeholder
  // and is intentionally NOT in the client anymore. Pages that need
  // requirements for the active project should call
  // `listProjectRequirements(activeProjectId, { limit, offset })`.
  listProjectRequirements(
    projectId: ProjectId,
    query: Pagination = { limit: 25, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<RequirementPage>> {
    return this.request<RequirementPage>(
      'GET',
      `/api/projects/${projectId}/requirements`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  // POST /api/insights/{insight_id}/requirement — the backend accepts an
  // empty body (RequirementCreateRequest has no required fields) and returns
  // 201 with the freshly-generated RequirementRead. The client intentionally
  // does NOT take content fields here: PM editable fields are applied later
  // via `patchRequirement`.
  //
  // The optional ``promptVersion`` is forwarded verbatim — the page passes
  // ``requirement-generation-v2`` to opt into the revised PM-facing
  // prompt. Older callers (no third arg) still send ``{}`` so the empty-
  // body behaviour is preserved for the legacy test path.
  createRequirement(
    insightId: InsightId,
    signal?: AbortSignal,
    promptVersion?: string | null,
    projectId?: ProjectId,
  ): Promise<ApiResult<Requirement>> {
    const body: RequirementCreateRequest =
      promptVersion && promptVersion.length > 0
        ? { prompt_version: promptVersion }
        : {};
    return this.request<Requirement>(
      'POST',
      `/api/insights/${insightId}/requirement`,
      { body, signal, query: projectId ? { project_id: projectId } : undefined },
    );
  }

  getRequirement(
    requirementId: RequirementId,
    signal?: AbortSignal,
    projectId?: ProjectId,
  ): Promise<ApiResult<Requirement>> {
    return this.request<Requirement>(
      'GET',
      `/api/requirements/${requirementId}`,
      { signal, query: projectId ? { project_id: projectId } : undefined },
    );
  }

  // PATCH /api/requirements/{requirement_id} — both `status` and `content`
  // are optional; the backend applies whichever set is present. The PM
  // should send at least one of them or the request is a 422.
  patchRequirement(
    requirementId: RequirementId,
    body: RequirementPatchRequest,
    signal?: AbortSignal,
    projectId?: ProjectId,
  ): Promise<ApiResult<Requirement>> {
    return this.request<Requirement>(
      'PATCH',
      `/api/requirements/${requirementId}`,
      { body, signal, query: projectId ? { project_id: projectId } : undefined },
    );
  }

  /* ---------- PRD ----------
   *
   * Phase 7 C1 — real contract. The previous ``listPrds`` /
   * ``createPrd`` used a fake ``Prd { id, requirement_id, version,
   * status }`` stub and ``PrdCreate { body: string }`` request —
   * the backend reads ``content: PRDContent`` (a 13-section
   * structured payload) instead, so any caller that still wrote
   * ``body: string`` would have 422'd. ``listProjectPrds`` is the
   * Phase 7 D1 endpoint that survives a page refresh on ``/prds``.
   */

  // Per-requirement PRD history. Returns a PrdPage envelope so the
  // caller gets ``active_prd_id`` / ``active_version`` alongside the
  // paginated slice — the page MUST NOT derive the active PRD from
  // ``items`` because pagination can skip past the newest row.
  listPrds(
    requirementId: RequirementId,
    signal?: AbortSignal,
    projectId?: ProjectId,
  ): Promise<ApiResult<PrdPage>> {
    return this.request<PrdPage>(
      'GET',
      `/api/requirements/${requirementId}/prds`,
      { signal, query: projectId ? { project_id: projectId } : undefined },
    );
  }

  // Phase 7 D1 — project-scoped PRD index. Returns EVERY PRD
  // version across the project (no latest-per-requirement
  // projection) so the page can group by ``requirement_id`` and
  // render the version timeline without dropping history.
  listProjectPrds(
    projectId: ProjectId,
    query: Pagination = { limit: 25, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<ProjectPrdPage>> {
    return this.request<ProjectPrdPage>(
      'GET',
      `/api/projects/${projectId}/prds`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  // POST /api/requirements/{requirement_id}/prds — body
  // ``PRDCreateRequest`` allows an empty body (the AI client
  // generates the PRD), or a fully-formed ``content: PRDContent``
  // for PM-authored PRDs. Sending the old ``{ body: string }``
  // would 422 because the schema reads ``content``, not ``body``.
  createPrd(
    requirementId: RequirementId,
    body: PrdCreateRequest = {},
    signal?: AbortSignal,
    projectId?: ProjectId,
  ): Promise<ApiResult<PrdRead>> {
    return this.request<PrdRead>(
      'POST',
      `/api/requirements/${requirementId}/prds`,
      { body, signal, query: projectId ? { project_id: projectId } : undefined },
    );
  }

  // GET /api/prds/{prd_id} — single PRD version.
  getPrd(
    prdId: PrdId,
    signal?: AbortSignal,
    projectId?: ProjectId,
  ): Promise<ApiResult<PrdRead>> {
    return this.request<PrdRead>('GET', `/api/prds/${prdId}`, { signal, query: projectId ? { project_id: projectId } : undefined });
  }

  // PATCH /api/prds/{prd_id} — both ``status`` and ``content``
  // are optional; the backend applies whichever set is present.
  patchPrd(
    prdId: PrdId,
    body: PrdPatchRequest,
    signal?: AbortSignal,
    projectId?: ProjectId,
  ): Promise<ApiResult<PrdRead>> {
    return this.request<PrdRead>(
      'PATCH',
      `/api/prds/${prdId}`,
      { body, signal, query: projectId ? { project_id: projectId } : undefined },
    );
  }

  // POST /api/prds/{prd_id}/accept — body MUST be empty
  // (``extra=forbid``); the backend refuses any client-claimed
  // identity until a real auth layer exists. We pass an explicit
  // ``{}`` so the body is sent and the request shape is pinned in
  // tests (rather than relying on the JSON-serialiser skipping
  // ``undefined``).
  acceptPrd(
    prdId: PrdId,
    signal?: AbortSignal,
    projectId?: ProjectId,
  ): Promise<ApiResult<PrdRead>> {
    return this.request<PrdRead>(
      'POST',
      `/api/prds/${prdId}/accept`,
      { body: {}, signal, query: projectId ? { project_id: projectId } : undefined },
    );
  }

  /* ---------- Evaluation (Phase 6) ----------
   *
   * Create requests use explicit project/source linkage and server-side validation.
   *
   * Cross-project isolation: every per-run endpoint accepts an
   * OPTIONAL ``project_id`` query parameter. When present, the
   * backend enforces ``run.project_id == project_id`` and returns
   * ``404`` (not 403) on mismatch — the same shape used by
   * clusters and insights. The frontend always passes the active
   * project id so a multi-tenant deployment can never render a
   * foreign project's run.
   *
   * The list endpoint ``/api/projects/{project_id}/evaluations``
   * is the Phase 7 D1 project index and does NOT accept
   * ``project_id`` (the route already encodes it).
   */

  getReferenceTemplate(body: EvaluationCreateRequest, signal?: AbortSignal): Promise<ApiResult<ReferenceTemplate>> {
    return this.request('GET', '/api/evaluations/reference-template', { query: { ...body }, signal });
  }

  createReferenceReview(body: EvaluationCreateRequest & { csv_text: string }, signal?: AbortSignal): Promise<ApiResult<EvaluationRunRead>> {
    return this.request('POST', '/api/evaluations/reference-review', { body, signal });
  }

  getReferenceReview(runId: string, projectId: string, offset = 0, signal?: AbortSignal): Promise<ApiResult<ReferenceReviewPage>> {
    return this.request('GET', `/api/evaluations/${runId}/reference-review`, { query: { project_id: projectId, limit: 25, offset }, signal });
  }

  createEvaluation(
    body: EvaluationCreateRequest,
    mode: 'run-check' | 'reference',
    signal?: AbortSignal,
  ): Promise<ApiResult<Pick<EvaluationRunRead, 'id' | 'project_id'>>> {
    return this.request('POST', mode === 'run-check' ? '/api/evaluations/run-check' : '/api/evaluations', { body, signal });
  }

  // GET /api/projects/{project_id}/evaluations?limit=&offset=
  // — Phase 7 D1 project-scoped EvaluationRun index.
  listProjectEvaluations(
    projectId: ProjectId,
    query: Pagination = { limit: 25, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<ProjectEvaluationRunPage>> {
    return this.request<ProjectEvaluationRunPage>(
      'GET',
      `/api/projects/${projectId}/evaluations`,
      { query: { limit: query.limit, offset: query.offset }, signal },
    );
  }

  // GET /api/evaluations/{run_id}?project_id=… — single run row.
  // ``project_id`` is OPTIONAL; the backend enforces it when
  // supplied (mismatch → 404). The frontend always supplies it
  // when an active project is known so a multi-tenant deployment
  // cannot render a foreign run.
  getEvaluation(
    runId: string,
    query: { project_id?: ProjectId } = {},
    signal?: AbortSignal,
  ): Promise<ApiResult<EvaluationRunRead>> {
    const q: Record<string, unknown> = {};
    if (query.project_id !== undefined) q.project_id = query.project_id;
    return this.request<EvaluationRunRead>(
      'GET',
      `/api/evaluations/${runId}`,
      { query: q, signal },
    );
  }

  // GET /api/evaluations/{run_id}/metrics?project_id=&limit=&offset=
  listEvaluationMetrics(
    runId: string,
    query: EvaluationRunQuery = { limit: 50, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<EvaluationMetricPage>> {
    const q: Record<string, unknown> = {
      limit: query.limit,
      offset: query.offset,
    };
    if (query.project_id !== undefined) q.project_id = query.project_id;
    return this.request<EvaluationMetricPage>(
      'GET',
      `/api/evaluations/${runId}/metrics`,
      { query: q, signal },
    );
  }

  // GET /api/evaluations/{run_id}/cases?project_id=&limit=&offset=
  listEvaluationCases(
    runId: string,
    query: EvaluationRunQuery = { limit: 50, offset: 0 },
    signal?: AbortSignal,
  ): Promise<ApiResult<EvaluationCasePage>> {
    const q: Record<string, unknown> = {
      limit: query.limit,
      offset: query.offset,
    };
    if (query.project_id !== undefined) q.project_id = query.project_id;
    return this.request<EvaluationCasePage>(
      'GET',
      `/api/evaluations/${runId}/cases`,
      { query: q, signal },
    );
  }

  /* ---------- Research (Phase 8) ----------
   *
   * Front-end surface for the external market intelligence pipeline.
   * Mirrors backend/app/api/research.py:
   *
   *   POST /api/insights/{insight_id}/research               — synchronous run.
   *   GET  /api/insights/{insight_id}/research?project_id=   — paginated run
   *                                                              history for
   *                                                              one insight.
   *   GET  /api/research/{run_id}?project_id=                — single run row.
   *   GET  /api/research/{run_id}/sources?project_id=
   *   GET  /api/research/{run_id}/findings?project_id=
   *
   * Every Research endpoint takes a REQUIRED ``project_id`` query
   * parameter (the backend ``Query(...)`` decorator without a default
   * rejects a missing value with 422). The page always supplies the
   * active project id so a multi-tenant deployment never lets the
   * page render a foreign project's research surface, and so a real
   * call never trips the 422 on a bare request.
   *
   * Concurrency: the POST is synchronous on the backend (200 once
   * the run row is finalised); the page optimistically surfaces a
   * "Running" badge during the request window and refetches the
   * history when the response lands. Cross-project switching clears
   * every in-flight UI state via ``signal.aborted`` (the per-project
   * AbortController in useProjectScopedFetch).
   */

  startResearchRun(
    insightId: InsightId,
    body: ResearchRunRequest,
    signal?: AbortSignal,
  ): Promise<ApiResult<ResearchRunRead>> {
    return this.request<ResearchRunRead>(
      'POST',
      `/api/insights/${insightId}/research`,
      { body, signal },
    );
  }

  listInsightResearchRuns(
    insightId: InsightId,
    query: { project_id: ProjectId; limit?: number; offset?: number },
    signal?: AbortSignal,
  ): Promise<ApiResult<ResearchRunListPage>> {
    return this.request<ResearchRunListPage>(
      'GET',
      `/api/insights/${insightId}/research`,
      {
        query: {
          project_id: query.project_id,
          limit: query.limit ?? 50,
          offset: query.offset ?? 0,
        },
        signal,
      },
    );
  }

  getResearchRun(
    runId: string,
    query: { project_id: ProjectId },
    signal?: AbortSignal,
  ): Promise<ApiResult<ResearchRunRead>> {
    return this.request<ResearchRunRead>(
      'GET',
      `/api/research/${runId}`,
      { query: { project_id: query.project_id }, signal },
    );
  }

  listResearchSources(
    runId: string,
    query: { project_id: ProjectId } & Pagination,
    signal?: AbortSignal,
  ): Promise<ApiResult<ResearchSourceListPage>> {
    return this.request<ResearchSourceListPage>(
      'GET',
      `/api/research/${runId}/sources`,
      {
        query: {
          project_id: query.project_id,
          limit: query.limit,
          offset: query.offset,
        },
        signal,
      },
    );
  }

  listResearchFindings(
    runId: string,
    query: { project_id: ProjectId } & Pagination,
    signal?: AbortSignal,
  ): Promise<ApiResult<ResearchFindingListPage>> {
    return this.request<ResearchFindingListPage>(
      'GET',
      `/api/research/${runId}/findings`,
      {
        query: {
          project_id: query.project_id,
          limit: query.limit,
          offset: query.offset,
        },
        signal,
      },
    );
  }

  /* ---------- Tool Layer (Phase 8) ----------
   *
   * Front-end surface for the simulated external-tool surface.
   * Mirrors backend/app/api/tools.py:
   *
   *   POST /api/requirements/{requirement_id}/tool-runs?project_id=
   *         — Prepare. Idempotent on (project_id, request_id);
   *           first call returns 201, repeat returns 200 with the
   *           existing awaiting_approval row. The frontend does
   *           not read ``X-Tool-Run-Created`` — a 201/200 either
   *           way is a successful Prepare.
   *   GET  /api/requirements/{requirement_id}/tool-runs?project_id=
   *         — paginated history with ``active_run_id`` pointer.
   *   GET  /api/tool-runs/{tool_run_id}?project_id=
   *         — single run row.
   *   POST /api/tool-runs/{tool_run_id}/cancel?project_id=
   *         — awaiting_approval → cancelled (empty body).
   *   POST /api/tool-runs/{tool_run_id}/approve?project_id=
   *         — drives the simulator (empty body). On success the
   *           row is updated to ``completed`` and
   *           ``external_id`` carries a ``SIM-...`` id.
   *
   * Every endpoint takes a REQUIRED ``project_id`` query
   * parameter; the backend ``Query(...)`` decorator without a
   * default rejects a missing value with 422. The page always
   * supplies the active project id so a multi-tenant deployment
   * never lets the page render a foreign project's tool run.
   *
   * Errors:
   *   404 — unknown / cross-project.
   *   409 — state-machine violation (e.g. cancel a completed run).
   *   422 — schema rejection (bad request_id, unknown tool name,
   *         injected payload field).
   *   503 — simulator adapter config error.
   */

  prepareToolRun(
    requirementId: RequirementId,
    query: { project_id: ProjectId },
    body: ToolPrepareRequest,
    signal?: AbortSignal,
  ): Promise<ApiResult<ToolRunRead>> {
    return this.request<ToolRunRead>(
      'POST',
      `/api/requirements/${requirementId}/tool-runs`,
      { query: { project_id: query.project_id }, body, signal },
    );
  }

  listToolRuns(
    requirementId: RequirementId,
    query: { project_id: ProjectId; limit?: number; offset?: number },
    signal?: AbortSignal,
  ): Promise<ApiResult<ToolRunPage>> {
    return this.request<ToolRunPage>(
      'GET',
      `/api/requirements/${requirementId}/tool-runs`,
      {
        query: {
          project_id: query.project_id,
          limit: query.limit ?? 50,
          offset: query.offset ?? 0,
        },
        signal,
      },
    );
  }

  getToolRun(
    toolRunId: string,
    query: { project_id: ProjectId },
    signal?: AbortSignal,
  ): Promise<ApiResult<ToolRunRead>> {
    return this.request<ToolRunRead>(
      'GET',
      `/api/tool-runs/${toolRunId}`,
      { query: { project_id: query.project_id }, signal },
    );
  }

  cancelToolRun(
    toolRunId: string,
    query: { project_id: ProjectId },
    signal?: AbortSignal,
  ): Promise<ApiResult<ToolRunRead>> {
    return this.request<ToolRunRead>(
      'POST',
      `/api/tool-runs/${toolRunId}/cancel`,
      {
        query: { project_id: query.project_id },
        body: {} as ToolCancelRequest,
        signal,
      },
    );
  }

  approveToolRun(
    toolRunId: string,
    query: { project_id: ProjectId },
    signal?: AbortSignal,
  ): Promise<ApiResult<ToolRunRead>> {
    return this.request<ToolRunRead>(
      'POST',
      `/api/tool-runs/${toolRunId}/approve`,
      {
        query: { project_id: query.project_id },
        body: {} as ToolApproveRequest,
        signal,
      },
    );
  }
}

/* ---------- helpers ---------- */

async function safeParseJson(resp: Response): Promise<ApiError['body']> {
  try {
    const data = (await resp.json()) as unknown;
    if (data && typeof data === 'object') {
      return data as ApiError['body'];
    }
    return null;
  } catch {
    return null;
  }
}

function deriveErrorMessage(status: number, body: ApiError['body']): string {
  if (body && typeof body === 'object') {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
      const arr = detail as { msg?: string; loc?: unknown[] };
      if (typeof arr.msg === 'string') {
        return arr.msg;
      }
    }
    const msg = (body as { message?: unknown }).message;
    if (typeof msg === 'string') return msg;
  }
  if (status >= 500) return '服务暂不可用，请稍后再试。';
  if (status === 404) return '资源不存在。';
  if (status === 409) return '操作冲突，请刷新后重试。';
  if (status === 413) return '请求体过大。';
  if (status === 415) return '不支持的请求格式。';
  if (status === 422) return '请求参数不合法。';
  if (status === 429) return '请求过于频繁，请稍候。';
  return '请求失败。';
}

function normalisePage(query: FeedbackQuery): Pagination {
  const limit = clamp(query.limit ?? 25, 1, 200);
  const offset = Math.max(0, query.offset ?? 0);
  return { limit, offset };
}

function clamp(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return Math.min(Math.max(value, min), max);
}

/* ---------- default singleton ---------- */

let defaultClient: InsightFlowClient | null = null;

export function getApiClient(): InsightFlowClient {
  if (!defaultClient) {
    defaultClient = new InsightFlowClient();
  }
  return defaultClient;
}

export function setApiClient(client: InsightFlowClient | null): void {
  defaultClient = client;
}
