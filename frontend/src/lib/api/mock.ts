/**
 * In-test helper: a stub InsightFlowClient that stores every call in-memory and
 * returns whatever the test pre-loads. Used only under vitest, never shipped to
 * production pages (it deliberately duplicates the public surface to keep the
 * type checker honest).
 */

import type {
  AnalysisResultsResponse,
  AnalysisRun,
  ApiResult,
  ClusterRun,
  DecisionHistoryPage,
  DecisionPatchRequest,
  EvaluationCasePage,
  EvaluationCaseRead,
  EvaluationMetricPage,
  EvaluationMetricRead,
  EvaluationRunRead,
  Feedback,
  FeedbackAnalysisEnvelope,
  FeedbackAnalysisRead,
  FeedbackImportSummary,
  FeedbackListResponse,
  Insight,
  InsightDecision,
  InsightEvidenceResponse,
  InsightRun,
  OpportunityScore,
  OpportunityScorePage,
  Pagination,
  PrdCreateRequest,
  PrdPage,
  PrdPatchRequest,
  PrdRead,
  Project,
  ProjectAnalysisRunPage,
  ProjectCreate,
  ProjectEvaluationRunPage,
  ProjectInsightRunPage,
  ProjectPrdPage,
  Requirement,
  RequirementPage,
  RequirementPatchRequest,
} from './types';

export class MockInsightFlowClient {
  projects: Project[] = [];
  feedbackByProject = new Map<string, Feedback[]>();
  analysisRuns: AnalysisRun[] = [];
  clusterRuns: ClusterRun[] = [];
  /* Phase 7 Feedback Detail (E package) — per-feedback AI
   * classification rows. Tests seed this directly with
   * ``client.feedbackAnalyses.push({ ... })`` and the
   * ``getFeedbackAnalysis`` mock joins here with the same
   * deterministic ordering as the real backend. */
  feedbackAnalyses: FeedbackAnalysisRead[] = [];
  insightRuns: InsightRun[] = [];
  insights: Insight[] = [];
  scores: OpportunityScore[] = [];
  decisions: InsightDecision[] = [];
  requirements: Requirement[] = [];
  prdReadList: PrdRead[] = [];
  evaluationRuns: EvaluationRunRead[] = [];
  evaluationMetrics: EvaluationMetricRead[] = [];
  evaluationCases: EvaluationCaseRead[] = [];
  callLog: Array<{ method: string; path: string; body?: unknown }> = [];

  forceError: { path?: string; status: number; message: string } | null = null;

  log<T>(method: string, path: string, body?: T): void {
    this.callLog.push({ method, path, body: body as unknown });
  }

  applyError<T>(): ApiResult<T> {
    // Only valid when ``forceError`` has been set explicitly by the
    // test harness (``client.forceError = { ... }``). Calling this
    // helper without that pre-condition used to throw a TypeError on
    // the non-null assertion, surfacing as an "unhandled rejection"
    // in vitest because product code awaited the returned promise
    // without ever expecting it to reject. The two cases — forced
    // test errors vs missing-fixture reads — are now split between
    // ``applyError`` and ``notFound`` so the contract shape matches
    // what the real backend would emit.
    if (!this.forceError) {
      // Fall back to a generic 500 — this branch is only reachable when
      // a test forgot to set ``forceError`` before ``applyError`` was
      // called, which is itself a bug in the harness. Keep behaviour
      // predictable so the rejection never escapes as TypeError.
      return this.notFound<T>('__uninitialised_forceError__');
    }
    const e = this.forceError;
    return {
      data: null,
      error: {
        status: e.status,
        statusText: 'ForcedError',
        url: e.path ?? '',
        method: 'MOCK',
        body: { detail: e.message },
        message: e.message,
        requestId: `mock_${this.callLog.length}`,
      },
      requestId: `mock_${this.callLog.length}`,
    };
  }

  /**
   * Real "resource not found" shape mirroring backend 404 responses.
   *
   * Used when the test harness has no fixture pre-loaded for a given
   * identifier (``client.getOpportunityScore(insightId)`` with no
   * matching ``scores`` row, etc.). The InsightFlowClient returns 404
   * for these cases so the page can distinguish "AI snapshot missing"
   * from "server error" — making this an unhandled-rejection in tests
   * would silently regress that distinction.
   */
  notFound<T>(path: string): ApiResult<T> {
    return {
      data: null,
      error: {
        status: 404,
        statusText: 'Not Found',
        url: path,
        method: 'MOCK',
        body: { detail: 'resource not found' },
        message: '资源不存在。',
        requestId: `mock_${this.callLog.length}`,
      },
      requestId: `mock_${this.callLog.length}`,
    };
  }

  shouldError(path: string): boolean {
    if (!this.forceError) return false;
    return !this.forceError.path || this.forceError.path === path;
  }

  /* Projects */
  async listProjects(): Promise<ApiResult<Project[]>> {
    this.log('GET', '/api/projects');
    if (this.shouldError('/api/projects')) return this.applyError();
    return { data: [...this.projects], error: null, requestId: 'mock' };
  }

  async createProject(body: ProjectCreate): Promise<ApiResult<Project>> {
    this.log('POST', '/api/projects', body);
    const project: Project = {
      id: `proj_${this.projects.length + 1}`,
      name: body.name,
      product_name: body.product_name,
      description: body.description ?? null,
    };
    this.projects.push(project);
    return { data: project, error: null, requestId: 'mock' };
  }

  async getProject(id: string): Promise<ApiResult<Project>> {
    this.log('GET', `/api/projects/${id}`);
    const project = this.projects.find((p) => p.id === id);
    if (!project) {
      return this.notFound(`/api/projects/${id}`);
    }
    return { data: project, error: null, requestId: 'mock' };
  }

  /* Feedback */
  async listFeedback(
    projectId: string,
    query: { limit?: number; offset?: number } = {},
  ): Promise<ApiResult<FeedbackListResponse>> {
    this.log('GET', `/api/projects/${projectId}/feedback`, query);
    const all = this.feedbackByProject.get(projectId) ?? [];
    const limit = query.limit ?? 25;
    const offset = query.offset ?? 0;
    const items = all.slice(offset, offset + limit);
    const response: FeedbackListResponse = {
      items,
      total: all.length,
      limit,
      offset,
    };
    return { data: response, error: null, requestId: 'mock' };
  }

  async importFeedback(
    projectId: string,
    file: File,
  ): Promise<ApiResult<FeedbackImportSummary>> {
    this.log('POST', `/api/projects/${projectId}/feedback/import`, { name: file.name });
    const existing = this.feedbackByProject.get(projectId) ?? [];
    this.feedbackByProject.set(projectId, [
      ...existing,
      {
        id: `fb_${existing.length + 1}`,
        project_id: projectId,
        external_id: `imp_${existing.length + 1}`,
        content: `imported from ${file.name}`,
      },
    ]);
    return {
      data: { total: existing.length + 1, imported: 1, skipped: 0 },
      error: null,
      requestId: 'mock',
    };
  }

  /* Phase 7 Feedback Detail (E package) — read-only per-feedback
   * AI classification selection. Mirrors
   * ``backend/app/api/feedback.py:get_feedback_analysis``:
   *
   *   1. Strict project filter — a missing project returns 404
   *      before we leak the existence of any feedback.
   *   2. Strict feedback filter — a feedback id that does not
   *      belong to ``projectId`` returns 404, never a partial
   *      body. (The brief requires the same 404 for cross-project
   *      and unknown id so the page cannot tell them apart.)
   *   3. ``run_type == 'classification'`` is enforced at the SQL
   *      layer on the real backend; the mock mirrors that filter
   *      so a clustering row in ``analysisRuns`` never seeds a
   *      selection.
   *   4. Latest mode ordering:
   *        AnalysisRun.created_at DESC, id DESC,
   *        then FeedbackAnalysis.id DESC.
   *      The id tiebreakers keep pagination-like reads stable
   *      when two runs share a timestamp (single-transaction
   *      bulk insert / clock skew).
   *   5. When no analysis row exists the envelope carries
   *      ``analysis: null`` AND ``selection.selected_run_id:
   *      null`` — the page must render "AI Analysis N/A" rather
   *      than guess.
   *
   * ``callLog`` is updated through ``this.log`` so the test
   * harness can verify call ordering (per the
   * [[feedback-mock-test-logging]] rule). */
  async getFeedbackAnalysis(
    projectId: string,
    feedbackId: string,
    query: { run_id?: string } = {},
    _signal?: AbortSignal,
  ): Promise<ApiResult<FeedbackAnalysisEnvelope>> {
    this.log(
      'GET',
      `/api/projects/${projectId}/feedback/${feedbackId}/analysis`,
      query,
    );
    const path = `/api/projects/${projectId}/feedback/${feedbackId}/analysis`;

    if (!this.projects.some((p) => p.id === projectId)) {
      return this.notFound(path);
    }
    const feedbackList = this.feedbackByProject.get(projectId) ?? [];
    if (!feedbackList.some((f) => f.id === feedbackId)) {
      return this.notFound(path);
    }

    const ruleExplicit =
      'explicit run_id query parameter — only the given run is consulted.';
    const ruleLatest =
      "latest classification run owned by this project that has a FeedbackAnalysis for this feedback, ordered by AnalysisRun.created_at DESC, id DESC, then FeedbackAnalysis.id DESC. Clustering runs and other projects' runs are ignored.";

    if (query.run_id !== undefined) {
      const run = this.analysisRuns.find((r) => r.id === query.run_id);
      if (
        !run ||
        run.project_id !== projectId ||
        run.run_type !== 'classification'
      ) {
        return this.notFound(path);
      }
      const analysis = this.feedbackAnalyses.find(
        (a) => a.feedback_id === feedbackId && a.run_id === query.run_id,
      );
      return {
        data: {
          feedback_id: feedbackId,
          analysis: analysis ?? null,
          selection: {
            mode: 'explicit',
            selected_run_id: analysis ? analysis.run_id : null,
            selection_rule: ruleExplicit,
          },
        },
        error: null,
        requestId: 'mock',
      };
    }

    /* Latest: join analysis rows with their classification
     * runs, drop any that don't belong to the project or that
     * are not classification, then apply the three-level stable
     * sort. Top 1 is the deterministic pick; if no candidate
     * exists, return null with selected_run_id null. */
    const candidates = this.feedbackAnalyses
      .filter((a) => a.feedback_id === feedbackId)
      .map((a) => {
        const run = this.analysisRuns.find((r) => r.id === a.run_id);
        return { analysis: a, run };
      })
      .filter(
        (c) =>
          c.run !== undefined &&
          c.run.project_id === projectId &&
          c.run.run_type === 'classification',
      )
      .sort((a, b) => {
        const ra = a.run!;
        const rb = b.run!;
        // Three-level stable order:
        //   AnalysisRun.created_at DESC, id DESC,
        //   then FeedbackAnalysis.id DESC.
        // ``AnalysisRun.created_at`` is optional (legacy
        // fixtures may lack it), so we explicitly handle the
        // four cases instead of relying on direct equality /
        // lexicographic compare on possibly-undefined values.
        // Rows missing ``created_at`` sort LAST in DESC so a
        // fresh dated row never hides behind an undated one
        // (same convention as ``listProjectAnalysisRuns``).
        const ac = ra.created_at;
        const bc = rb.created_at;
        if (ac && bc) {
          if (bc !== ac) return bc < ac ? -1 : 1;
          const idCmp = rb.id.localeCompare(ra.id);
          if (idCmp !== 0) return idCmp;
          return b.analysis.id.localeCompare(a.analysis.id);
        }
        if (ac && !bc) return -1;
        if (!ac && bc) return 1;
        const idCmp = rb.id.localeCompare(ra.id);
        if (idCmp !== 0) return idCmp;
        return b.analysis.id.localeCompare(a.analysis.id);
      });
    const top = candidates[0]?.analysis ?? null;
    return {
      data: {
        feedback_id: feedbackId,
        analysis: top,
        selection: {
          mode: 'latest',
          selected_run_id: top ? top.run_id : null,
          selection_rule: ruleLatest,
        },
      },
      error: null,
      requestId: 'mock',
    };
  }

  /* Classification */
  async createAnalysis(projectId: string): Promise<ApiResult<AnalysisRun>> {
    this.log('POST', `/api/projects/${projectId}/analysis`);
    const run: AnalysisRun = {
      id: `arun_${this.analysisRuns.length + 1}`,
      project_id: projectId,
      run_type: 'classification',
      model: null,
      prompt_version: 'v1',
      status: 'completed',
      input_count: this.feedbackByProject.get(projectId)?.length ?? 0,
      processed_count: this.feedbackByProject.get(projectId)?.length ?? 0,
      success_count: this.feedbackByProject.get(projectId)?.length ?? 0,
      failed_count: 0,
      /* Phase 7 D — list endpoint sorts ``created_at DESC, id DESC``;
       * the mock mirrors the backend contract by stamping a
       * monotonic timestamp on create so the history list test
       * surface can verify the stable order without injecting
       * fixtures. */
      created_at: new Date().toISOString(),
    };
    this.analysisRuns.push(run);
    return { data: run, error: null, requestId: 'mock' };
  }

  async getAnalysis(runId: string): Promise<ApiResult<AnalysisRun>> {
    this.log('GET', `/api/analysis/${runId}`);
    const run = this.analysisRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/analysis/${runId}`);
    return { data: run, error: null, requestId: 'mock' };
  }

  async retryAnalysis(runId: string): Promise<ApiResult<AnalysisRun>> {
    this.log('POST', `/api/analysis/${runId}/retry`);
    const run = this.analysisRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/analysis/${runId}/retry`);
    run.status = 'completed';
    return { data: run, error: null, requestId: 'mock' };
  }

  async getAnalysisResults(
    runId: string,
    _query: { limit: number; offset: number },
  ): Promise<ApiResult<AnalysisResultsResponse>> {
    this.log('GET', `/api/analysis/${runId}/results`);
    const run = this.analysisRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/analysis/${runId}/results`);
    return {
      data: { run_id: runId, items: [], total: 0 },
      error: null,
      requestId: 'mock',
    };
  }

  /* Phase 7 D — project-scoped AnalysisRun history.
   *
   * Strict project filter: the page MUST only see runs owned by
   * the active project, never rows leaked from a different
   * project. ``run_type`` is restricted to ``classification`` /
   * ``clustering`` so the mock mirrors the backend Literal
   * gate; an out-of-set value returns an empty page rather than
   * 422 (the test surface prefers round-trip success on bad
   * input so the page-level "refuse foreign" defense is the
   * single source of truth).
   *
   * Merge contract: the real backend stores classification and
   * clustering rows in the same ``analysis_runs`` table (both
   * ``run_type`` literals share the read schema); the mock keeps
   * ``analysisRuns`` and ``clusterRuns`` as separate arrays so
   * ``createClustering`` / ``getClustering`` / ``retryClustering``
   * keep their existing behaviour, then UNIONS them here by
   * ``project_id``. De-duplication by ``id`` is defensive — the
   * two arrays use independent counters so a collision would only
   * ever come from an injected fixture, but the page must never
   * surface a duplicate row. Sorting and pagination remain on the
   * ``created_at DESC, id DESC`` contract. */
  async listProjectAnalysisRuns(
    projectId: string,
    query: { run_type?: 'classification' | 'clustering'; limit?: number; offset?: number } = {
      limit: 50,
      offset: 0,
    },
  ): Promise<ApiResult<ProjectAnalysisRunPage>> {
    this.log('GET', `/api/projects/${projectId}/analysis-runs`, query);
    const limit = query.limit ?? 50;
    const offset = query.offset ?? 0;
    /* Union of classification + clustering runs owned by this
     * project. ``ClusterRun`` is structurally a subset of
     * ``AnalysisRun`` (``types.ts: ClusterRun = AnalysisRun``) so
     * the union type is honest without a cast. */
    const sameProject = (r: AnalysisRun) => r.project_id === projectId;
    const merged: AnalysisRun[] = [
      ...this.analysisRuns.filter(sameProject),
      ...this.clusterRuns.filter(sameProject),
    ];
    const seen = new Set<string>();
    const unique = merged.filter((r) => {
      if (seen.has(r.id)) return false;
      seen.add(r.id);
      return true;
    });
    const filtered =
      query.run_type !== undefined
        ? unique.filter((r) => r.run_type === query.run_type)
        : unique;
    /* Stable sort — created_at DESC, id DESC. The id tiebreaker
     * matches the backend route so pagination stays consistent
     * when two runs share a created_at (single-transaction bulk
     * insert, or clock skew on DateTime(timezone=True)).
     * Rows missing a created_at (legacy fixtures) sort LAST in
     * DESC so a fresh row never hides behind an undated one. */
    const sorted = [...filtered].sort((a, b) => {
      const ac = a.created_at;
      const bc = b.created_at;
      if (ac && bc) {
        if (bc !== ac) return bc < ac ? -1 : 1;
        return b.id.localeCompare(a.id);
      }
      if (ac && !bc) return -1;
      if (!ac && bc) return 1;
      return b.id.localeCompare(a.id);
    });
    return {
      data: {
        items: sorted.slice(offset, offset + limit),
        total: sorted.length,
        limit,
        offset,
      },
      error: null,
      requestId: 'mock',
    };
  }

  /* Clustering */
  async createClustering(
    projectId: string,
    _analysisRunId: string,
  ): Promise<ApiResult<ClusterRun>> {
    this.log('POST', `/api/projects/${projectId}/clustering`);
    const run: ClusterRun = {
      id: `crun_${this.clusterRuns.length + 1}`,
      project_id: projectId,
      run_type: 'clustering',
      model: null,
      prompt_version: 'v1',
      status: 'completed',
      input_count: 0,
      processed_count: 0,
      success_count: 0,
      failed_count: 0,
      created_at: new Date().toISOString(),
    };
    this.clusterRuns.push(run);
    return { data: run, error: null, requestId: 'mock' };
  }

  async getClustering(runId: string): Promise<ApiResult<ClusterRun>> {
    this.log('GET', `/api/clustering/${runId}`);
    const run = this.clusterRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/clustering/${runId}`);
    return { data: run, error: null, requestId: 'mock' };
  }

  async retryClustering(runId: string): Promise<ApiResult<ClusterRun>> {
    this.log('POST', `/api/clustering/${runId}/retry`);
    const run = this.clusterRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/clustering/${runId}/retry`);
    run.status = 'completed';
    return { data: run, error: null, requestId: 'mock' };
  }

  /* Insights */
  async generateInsights(
    projectId: string,
    clusteringRunId: string,
  ): Promise<ApiResult<InsightRun>> {
    this.log('POST', `/api/projects/${projectId}/insights/generate`, {
      clusteringRunId,
    });
    const run: InsightRun = {
      id: `irun_${this.insightRuns.length + 1}`,
      project_id: projectId,
      clustering_run_id: clusteringRunId,
      classification_run_id: clusteringRunId,
      status: 'completed',
      model: null,
      prompt_version: 'v1',
      input_count: 0,
      processed_count: 0,
      success_count: 0,
      needs_validation_count: 0,
      failed_count: 0,
      created_at: new Date().toISOString(),
    };
    this.insightRuns.push(run);
    return { data: run, error: null, requestId: 'mock' };
  }

  async getInsightRun(runId: string): Promise<ApiResult<InsightRun>> {
    this.log('GET', `/api/insight-runs/${runId}`);
    const run = this.insightRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/insight-runs/${runId}`);
    return { data: run, error: null, requestId: 'mock' };
  }

  async retryInsightRun(runId: string): Promise<ApiResult<InsightRun>> {
    this.log('POST', `/api/insight-runs/${runId}/retry`);
    const run = this.insightRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/insight-runs/${runId}/retry`);
    run.status = 'completed';
    return { data: run, error: null, requestId: 'mock' };
  }

  /* Phase 7 D — project-scoped InsightRun history.
   *
   * Strict project filter — a row owned by a different project
   * never leaks into the page's view. Sort is
   * ``created_at DESC, id DESC`` (id tiebreaker keeps pagination
   * stable when two rows share a timestamp). Rows missing a
   * ``created_at`` (legacy fixtures) sort LAST so a fresh row
   * never hides behind an undated one. */
  async listProjectInsightRuns(
    projectId: string,
    query: { limit?: number; offset?: number } = { limit: 50, offset: 0 },
  ): Promise<ApiResult<ProjectInsightRunPage>> {
    this.log('GET', `/api/projects/${projectId}/insight-runs`, query);
    const limit = query.limit ?? 50;
    const offset = query.offset ?? 0;
    const all = this.insightRuns.filter((r) => r.project_id === projectId);
    const sorted = [...all].sort((a, b) => {
      const ac = a.created_at;
      const bc = b.created_at;
      if (ac && bc) {
        if (bc !== ac) return bc < ac ? -1 : 1;
        return b.id.localeCompare(a.id);
      }
      if (ac && !bc) return -1;
      if (!ac && bc) return 1;
      return b.id.localeCompare(a.id);
    });
    return {
      data: {
        items: sorted.slice(offset, offset + limit),
        total: sorted.length,
        limit,
        offset,
      },
      error: null,
      requestId: 'mock',
    };
  }

  async listProjectInsights(
    projectId: string,
    query: { limit: number; offset: number } = { limit: 25, offset: 0 },
  ): Promise<ApiResult<{ items: Insight[]; total: number; limit: number; offset: number }>> {
    this.log('GET', `/api/projects/${projectId}/insights`, query);
    const all = [...this.insights];
    return {
      data: {
        items: all.slice(query.offset, query.offset + query.limit),
        total: all.length,
        limit: query.limit,
        offset: query.offset,
      },
      error: null,
      requestId: 'mock',
    };
  }

  async getInsight(insightId: string): Promise<ApiResult<Insight>> {
    this.log('GET', `/api/insights/${insightId}`);
    const insight = this.insights.find((i) => i.id === insightId);
    if (!insight) return this.notFound(`/api/insights/${insightId}`);
    return { data: insight, error: null, requestId: 'mock' };
  }

  async getInsightEvidence(
    insightId: string,
    query: Pagination = { limit: 25, offset: 0 },
    _signal?: AbortSignal,
  ): Promise<ApiResult<InsightEvidenceResponse>> {
    this.log('GET', `/api/insights/${insightId}/evidence`, query);
    return {
      data: {
        insight_id: insightId,
        items: [],
        total: 0,
        limit: query.limit,
        offset: query.offset,
      },
      error: null,
      requestId: 'mock',
    };
  }

  /* Opportunity scores / decisions */
  async getOpportunityScore(insightId: string): Promise<ApiResult<OpportunityScore>> {
    this.log('GET', `/api/insights/${insightId}/opportunity-score`);
    const score = this.scores.find((s) => s.insight_id === insightId);
    if (!score) return this.notFound(`/api/insights/${insightId}/opportunity-score`);
    return { data: score, error: null, requestId: 'mock' };
  }

  async createOpportunityScore(
    insightId: string,
  ): Promise<ApiResult<OpportunityScore>> {
    this.log('POST', `/api/insights/${insightId}/opportunity-score`);
    // Phase 7 B1: mock returns a Phase 4 five-dimension snapshot with all
    // dimensions "unavailable" so tests that probe a missing dimension see
    // real gaps rather than synthesised values. The shape mirrors
    // backend/app/schemas/opportunity.py:OpportunityScoreRead exactly.
    const unavailable = {
      raw_value: null,
      score: null,
      available: false,
      affected_count: null,
      project_count: null,
      feedback_count: null,
      average_severity: null,
      recent_count: null,
      previous_count: null,
      note: 'mock dimension unavailable',
    };
    const halfWeights = {
      user_coverage: '0.30',
      severity: '0.30',
      growth: '0.10',
      user_value: '0.20',
      business_value: '0.10',
    };
    const score: OpportunityScore = {
      id: `score_${this.scores.length + 1}`,
      insight_id: insightId,
      project_id: 'mock-project',
      classification_run_id: 'mock-classification',
      source_insight_status: 'draft',
      source_snapshot_hash: 'mock-hash',
      breakdown: {
        user_coverage: { ...unavailable },
        severity: { ...unavailable },
        growth: { ...unavailable },
        user_value: { ...unavailable },
        business_value: { ...unavailable },
      },
      weights: { configured: halfWeights, effective: halfWeights },
      thresholds: {
        ai_priority_p0_below: '7.5',
        ai_priority_p1_below: '5',
      },
      business_value: null,
      total_score: null,
      ai_priority: null,
      ai_reasoning: 'mock — no real AI reasoning',
      calculation_version: 'mock-v0',
      model: 'mock',
      prompt_version: 'mock-prompt-v1',
      input_tokens: 0,
      output_tokens: 0,
      unknown_token_calls: 0,
      latency_ms: 0,
      attempt_count: 0,
      retry_count: 0,
    };
    this.scores.push(score);
    return { data: score, error: null, requestId: 'mock' };
  }

  async listInsightOpportunityScores(
    insightId: string,
    query: Pagination = { limit: 25, offset: 0 },
    _signal?: AbortSignal,
  ): Promise<ApiResult<OpportunityScorePage>> {
    this.log('GET', `/api/insights/${insightId}/opportunity-scores`, query);
    const items = this.scores.filter((s) => s.insight_id === insightId);
    return {
      data: {
        items,
        total: items.length,
        limit: query.limit,
        offset: query.offset,
      },
      error: null,
      requestId: 'mock',
    };
  }

  async getDecision(insightId: string): Promise<ApiResult<InsightDecision>> {
    this.log('GET', `/api/insights/${insightId}/decision`);
    const decision = this.decisions.find((d) => d.insight_id === insightId);
    if (!decision) return this.notFound(`/api/insights/${insightId}/decision`);
    return { data: decision, error: null, requestId: 'mock' };
  }

  async patchDecision(
    insightId: string,
    body: DecisionPatchRequest,
  ): Promise<ApiResult<InsightDecision>> {
    this.log('PATCH', `/api/insights/${insightId}/decision`, body);
    // Phase 7 B1: mock shape now mirrors the Phase 4 InsightDecisionRead:
    // both AI priority and PM priority are preserved, override_reason /
    // override_note are nullable, and the row carries project_id +
    // current_score_id for audit linkage.
    return {
      data: {
        id: `decision_mock`,
        insight_id: insightId,
        project_id: 'mock-project',
        current_score_id: null,
        ai_priority: null,
        pm_priority: body.pm_priority ?? null,
        business_value:
          body.business_value !== undefined && body.business_value !== null
            ? String(body.business_value)
            : null,
        status:
          body.action === 'override'
            ? 'overridden'
            : body.action === 'defer'
              ? 'deferred'
              : body.action === 'reject'
                ? 'rejected'
                : body.action === 'set_business_value'
                  ? 'pending_review'
                  : 'accepted',
        override_reason: body.override_reason ?? null,
        override_note: body.override_note ?? null,
        version: 1,
      },
      error: null,
      requestId: 'mock',
    };
  }

  async getDecisionHistory(
    insightId: string,
    query: Pagination = { limit: 50, offset: 0 },
    _signal?: AbortSignal,
  ): Promise<ApiResult<DecisionHistoryPage>> {
    this.log('GET', `/api/insights/${insightId}/decision/history`, query);
    return {
      data: { items: [], total: 0, limit: query.limit, offset: query.offset },
      error: null,
      requestId: 'mock',
    };
  }

  /* Requirements / PRDs */
  // Phase 7 B2: project-level list. The previous
  // /api/insights/{insight_id}/requirements endpoint is a B1-era placeholder
  // and is no longer reachable through the client.
  async listProjectRequirements(
    projectId: string,
    query: { limit: number; offset: number } = { limit: 25, offset: 0 },
    _signal?: AbortSignal,
  ): Promise<ApiResult<RequirementPage>> {
    this.log('GET', `/api/projects/${projectId}/requirements`, query);
    const all = this.requirements.filter((r) => r.project_id === projectId);
    const items = all.slice(query.offset, query.offset + query.limit);
    return {
      data: { items, total: all.length, limit: query.limit, offset: query.offset },
      error: null,
      requestId: 'mock',
    };
  }

  // POST /api/insights/{insight_id}/requirement — backend ignores the body
  // (RequirementCreateRequest has no required fields) and synthesises a
  // fully-populated AI snapshot. The mock mirrors the real
  // RequirementRead shape so patched fields round-trip through PATCH
  // without dropping schema-required entries.
  async createRequirement(
    insightId: string,
    _signal?: AbortSignal,
  ): Promise<ApiResult<Requirement>> {
    this.log('POST', `/api/insights/${insightId}/requirement`);
    const req = buildMockRequirement(insightId, this.requirements.length + 1);
    this.requirements.push(req);
    return { data: req, error: null, requestId: 'mock' };
  }

  async getRequirement(
    requirementId: string,
    _signal?: AbortSignal,
  ): Promise<ApiResult<Requirement>> {
    this.log('GET', `/api/requirements/${requirementId}`);
    const req = this.requirements.find((r) => r.id === requirementId);
    if (!req) return this.notFound(`/api/requirements/${requirementId}`);
    return { data: req, error: null, requestId: 'mock' };
  }

  async patchRequirement(
    requirementId: string,
    body: RequirementPatchRequest,
    _signal?: AbortSignal,
  ): Promise<ApiResult<Requirement>> {
    this.log('PATCH', `/api/requirements/${requirementId}`, body);
    const req = this.requirements.find((r) => r.id === requirementId);
    if (!req) return this.notFound(`/api/requirements/${requirementId}`);
    if (body.status !== undefined) req.status = body.status;
    if (body.content) {
      const c = body.content;
      req.name = c.name;
      req.problem = c.problem;
      req.target_user = c.target_user;
      req.user_story = c.user_story;
      req.solution_hypothesis = c.solution_hypothesis;
      req.success_metrics = c.success_metrics.map((m) => ({
        name: m.name,
        type: m.type,
        sources: m.sources ?? [],
        note: m.note ?? '',
      }));
      req.risks = c.risks.map((r) => ({
        category: r.category,
        description: r.description,
        evidence_ids: r.evidence_ids ?? [],
        mitigation: r.mitigation ?? '',
      }));
      req.evidence_references = c.evidence_references.map((e) => ({
        external_id: e.external_id,
        usage: e.usage,
      }));
      req.next_version += 1;
    }
    return { data: req, error: null, requestId: 'mock' };
  }

  /* PRD — Phase 7 C1 real contract.
   *
   * The previous C0 stub returned a bare Prd[] from listPrds and
   * accepted ``{ body: string }`` on createPrd, both of which would
   * have 422'd against the real backend. The mock now mirrors the
   * structured-payload contract (PrdPage envelope + PrdRead 13-
   * section content + PrdCreateRequest { content?: PrdContent })
   * so any page that depends on the mock exercises the same shape
   * the backend will hand back. ``this.log()`` runs on every method
   * per the in-test harness rule so callLog stays accurate. */

  async listPrds(
    requirementId: string,
    _signal?: AbortSignal,
  ): Promise<ApiResult<PrdPage>> {
    this.log('GET', `/api/requirements/${requirementId}/prds`);
    const all = this.prdReadList.filter((p) => p.requirement_id === requirementId);
    const limit = 50;
    const offset = 0;
    const items = all.slice(offset, offset + limit);
    // ``active_prd_id`` / ``active_version`` mirror the backend's
    // separate ``ORDER BY version DESC LIMIT 1`` lookup so the
    // active pointer stays stable regardless of pagination.
    const active = all.length > 0
      ? all.reduce((acc, cur) => (cur.version > acc.version ? cur : acc))
      : null;
    return {
      data: {
        items,
        total: all.length,
        limit,
        offset,
        active_prd_id: active?.id ?? null,
        active_version: active?.version ?? null,
      },
      error: null,
      requestId: 'mock',
    };
  }

  // Phase 7 D1 — project-scoped PRD index. Returns every PRD
  // version across the project (no latest-per-requirement
  // projection), ordered ``version DESC, id DESC`` to match the
  // backend route.
  async listProjectPrds(
    projectId: string,
    query: { limit?: number; offset?: number } = { limit: 25, offset: 0 },
    _signal?: AbortSignal,
  ): Promise<ApiResult<ProjectPrdPage>> {
    this.log('GET', `/api/projects/${projectId}/prds`, query);
    const all = this.prdReadList.filter((p) => p.project_id === projectId);
    const limit = query.limit ?? 25;
    const offset = query.offset ?? 0;
    // Sort stable: version DESC, id DESC. The id tiebreaker keeps
    // pagination consistent even when two PRD rows share a
    // version (only possible under a UNIQUE(requirement_id,
    // version) violation).
    const sorted = [...all].sort((a, b) => {
      if (b.version !== a.version) return b.version - a.version;
      return b.id.localeCompare(a.id);
    });
    const items = sorted.slice(offset, offset + limit);
    return {
      data: {
        items,
        total: all.length,
        limit,
        offset,
      },
      error: null,
      requestId: 'mock',
    };
  }

  async createPrd(
    requirementId: string,
    body: PrdCreateRequest = {},
    _signal?: AbortSignal,
  ): Promise<ApiResult<PrdRead>> {
    this.log('POST', `/api/requirements/${requirementId}/prds`, body);
    const now = new Date().toISOString();
    const req = this.requirements.find((r) => r.id === requirementId);
    const prd: PrdRead = {
      id: `prd_${this.prdReadList.length + 1}`,
      requirement_id: requirementId,
      project_id: req?.project_id ?? 'mock-project',
      insight_id: req?.insight_id ?? 'mock-insight',
      version: 1,
      status: 'draft',
      content: body.content ?? buildMockPrdContent(),
      content_hash: `mock-hash-${this.prdReadList.length + 1}`,
      is_ai_generated_draft: body.content === undefined,
      grounding_warnings: [],
      model: body.content === undefined ? 'mock' : null,
      prompt_version: 'mock-prompt-v1',
      input_tokens: 0,
      output_tokens: 0,
      unknown_token_calls: 0,
      latency_ms: 0,
      attempt_count: 0,
      retry_count: 0,
      accepted_at: null,
      accepted_by: null,
      created_at: now,
      updated_at: now,
    };
    this.prdReadList.push(prd);
    return { data: prd, error: null, requestId: 'mock' };
  }

  async getPrd(
    prdId: string,
    _signal?: AbortSignal,
  ): Promise<ApiResult<PrdRead>> {
    this.log('GET', `/api/prds/${prdId}`);
    const prd = this.prdReadList.find((p) => p.id === prdId);
    if (!prd) return this.notFound(`/api/prds/${prdId}`);
    return { data: prd, error: null, requestId: 'mock' };
  }

  async patchPrd(
    prdId: string,
    body: PrdPatchRequest,
    _signal?: AbortSignal,
  ): Promise<ApiResult<PrdRead>> {
    this.log('PATCH', `/api/prds/${prdId}`, body);
    const prd = this.prdReadList.find((p) => p.id === prdId);
    if (!prd) return this.notFound(`/api/prds/${prdId}`);
    if (body.status !== undefined) prd.status = body.status;
    if (body.content !== undefined) {
      prd.content = body.content;
      prd.is_ai_generated_draft = false;
    }
    if (body.status === 'accepted') {
      prd.accepted_at = new Date().toISOString();
      prd.accepted_by = null;
    }
    prd.updated_at = new Date().toISOString();
    return { data: prd, error: null, requestId: 'mock' };
  }

  async acceptPrd(
    prdId: string,
    _signal?: AbortSignal,
  ): Promise<ApiResult<PrdRead>> {
    this.log('POST', `/api/prds/${prdId}/accept`, {});
    const prd = this.prdReadList.find((p) => p.id === prdId);
    if (!prd) return this.notFound(`/api/prds/${prdId}/accept`);
    prd.status = 'accepted';
    prd.accepted_at = new Date().toISOString();
    prd.accepted_by = null;
    prd.updated_at = new Date().toISOString();
    return { data: prd, error: null, requestId: 'mock' };
  }

  /* Evaluation (Phase 6) — Phase 7 D2a
   *
   * Mock surface mirrors ``InsightFlowClient`` 1:1 so any test
   * that uses the mock exercises the same call signature the real
   * backend will receive. State arrays are public so a test can
   * seed fixtures directly:
   *
   *   client.evaluationRuns.push({ ... })
   *
   * Cross-project isolation is enforced when ``project_id`` is
   * supplied to ``getEvaluation`` / ``listEvaluationMetrics`` /
   * ``listEvaluationCases`` — a run that does not belong to the
   * project returns ``notFound`` (404), matching the backend's
   * ``_enforce_project_match`` rule. ``listProjectEvaluations``
   * is the project-scoped D1 endpoint and does NOT accept
   * ``project_id`` (the route already encodes it).
   */

  async listProjectEvaluations(
    projectId: string,
    query: { limit?: number; offset?: number } = { limit: 25, offset: 0 },
    _signal?: AbortSignal,
  ): Promise<ApiResult<ProjectEvaluationRunPage>> {
    this.log('GET', `/api/projects/${projectId}/evaluations`, query);
    const limit = query.limit ?? 25;
    const offset = query.offset ?? 0;
    // Sort stable: created_at DESC, id DESC. The id tiebreaker
    // matches the backend route so pagination stays consistent
    // when two runs share a created_at (single-transaction bulk
    // insert, or clock skew on DateTime(timezone=True)).
    const all = this.evaluationRuns.filter((r) => r.project_id === projectId);
    const sorted = [...all].sort((a, b) => {
      if (b.created_at !== a.created_at) {
        return b.created_at < a.created_at ? -1 : 1;
      }
      return b.id.localeCompare(a.id);
    });
    const items = sorted.slice(offset, offset + limit);
    return {
      data: { items, total: all.length, limit, offset },
      error: null,
      requestId: 'mock',
    };
  }

  async getEvaluation(
    runId: string,
    query: { project_id?: string } = {},
    _signal?: AbortSignal,
  ): Promise<ApiResult<EvaluationRunRead>> {
    this.log('GET', `/api/evaluations/${runId}`, query);
    const run = this.evaluationRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/evaluations/${runId}`);
    if (query.project_id !== undefined && run.project_id !== query.project_id) {
      return this.notFound(`/api/evaluations/${runId}`);
    }
    return { data: run, error: null, requestId: 'mock' };
  }

  async listEvaluationMetrics(
    runId: string,
    query: { project_id?: string; limit?: number; offset?: number } = {
      limit: 50,
      offset: 0,
    },
    _signal?: AbortSignal,
  ): Promise<ApiResult<EvaluationMetricPage>> {
    this.log('GET', `/api/evaluations/${runId}/metrics`, query);
    const run = this.evaluationRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/evaluations/${runId}/metrics`);
    if (query.project_id !== undefined && run.project_id !== query.project_id) {
      return this.notFound(`/api/evaluations/${runId}/metrics`);
    }
    const limit = query.limit ?? 50;
    const offset = query.offset ?? 0;
    const all = this.evaluationMetrics.filter((m) => m.run_id === runId);
    const items = all.slice(offset, offset + limit);
    return {
      data: { items, total: all.length, limit, offset },
      error: null,
      requestId: 'mock',
    };
  }

  async listEvaluationCases(
    runId: string,
    query: { project_id?: string; limit?: number; offset?: number } = {
      limit: 50,
      offset: 0,
    },
    _signal?: AbortSignal,
  ): Promise<ApiResult<EvaluationCasePage>> {
    this.log('GET', `/api/evaluations/${runId}/cases`, query);
    const run = this.evaluationRuns.find((r) => r.id === runId);
    if (!run) return this.notFound(`/api/evaluations/${runId}/cases`);
    if (query.project_id !== undefined && run.project_id !== query.project_id) {
      return this.notFound(`/api/evaluations/${runId}/cases`);
    }
    const limit = query.limit ?? 50;
    const offset = query.offset ?? 0;
    const all = this.evaluationCases.filter((c) => c.run_id === runId);
    const items = all.slice(offset, offset + limit);
    return {
      data: { items, total: all.length, limit, offset },
      error: null,
      requestId: 'mock',
    };
  }
}

/* ---------- helpers ---------- */

/** Build a minimal-but-valid 13-section PRDContent so the mock
 *  exercises the structured-payload contract end-to-end. The
 *  backend ``extra=forbid`` schema rejects unknown keys, so this
 *  helper must stay aligned with
 *  backend/app/schemas/requirement_prd.py:PRDContent. */
function buildMockPrdContent(): PrdRead['content'] {
  const ev = (text: string): PrdRead['content']['background'] => ({
    text,
    evidence_ids: ['mock-ev-1'],
  });
  return {
    background: ev('Mock background for C1 smoke.'),
    problem: ev('Mock problem statement for C1 smoke.'),
    target_user: ev('Mock target user for C1 smoke.'),
    user_story: ev('Mock user story for C1 smoke.'),
    product_goal: ev('Mock product goal for C1 smoke.'),
    proposed_solution: {
      text: 'Mock proposed solution for C1 smoke.',
      is_proposed: true,
      evidence_ids: ['mock-ev-1'],
    },
    functional_requirements: [
      { text: 'Mock FR for C1 smoke.', evidence_ids: ['mock-ev-1'] },
    ],
    user_flow: ev('Mock user flow for C1 smoke.'),
    edge_cases: [
      {
        category: 'empty_state',
        description: 'Mock empty-state for C1 smoke.',
        evidence_ids: ['mock-ev-1'],
      },
    ],
    acceptance_criteria: [
      {
        text: 'Mock AC for C1 smoke.',
        verifiable: true,
        note: '',
        evidence_ids: ['mock-ev-1'],
      },
    ],
    success_metrics: [
      {
        name: 'Mock engagement',
        type: 'metric_definition',
        note: '',
        sources: ['mock-run'],
      },
    ],
    risks: [
      {
        category: 'known',
        description: 'Mock known risk for C1 smoke.',
        evidence_ids: [],
        mitigation: 'Mock mitigation.',
      },
    ],
    mvp_scope: {
      in_scope: 'Mock in-scope for C1 smoke.',
      out_of_scope: 'Mock out-of-scope for C1 smoke.',
    },
    evidence_sources: ['mock-run'],
    evidence_ids: ['mock-ev-1'],
  };
}

function buildMockRequirement(insightId: string, seq: number): Requirement {
  const now = new Date().toISOString();
  return {
    id: `req_${Date.now().toString(36)}_${seq}`,
    project_id: 'mock-project',
    insight_id: insightId,
    decision_id: `decision_mock_${seq}`,
    score_id: null,
    insight_status_snapshot: 'accepted',
    decision_status_snapshot: 'accepted',
    ai_priority_snapshot: 'P1',
    pm_priority: 'P1',
    pm_override_reason_snapshot: null,
    source_snapshot_hash: `mock-hash-${seq}`,
    name: `Mock Requirement #${seq}`,
    problem: 'Mock problem statement — derived from the upstream Insight.',
    target_user: 'Mock target user segment.',
    user_story: 'As a mock user, I want a mock outcome, so that I can verify B2.',
    solution_hypothesis: 'Hypothesis: shipping X will move metric Y.',
    success_metrics: [
      {
        name: 'Engagement rate',
        type: 'metric_definition',
        sources: ['mock-run'],
        note: 'Synthetic metric for B2 contract smoke.',
      },
    ],
    risks: [
      {
        category: 'known',
        description: 'Coverage denominator may be empty at small projects.',
        evidence_ids: [],
        mitigation: 'Surface as Not Available rather than guessing.',
      },
    ],
    evidence_references: [
      { external_id: 'mock-ev-1', usage: 'representative quote' },
    ],
    status: 'draft',
    model: 'mock-model',
    prompt_version: 'mock-prompt-v1',
    input_tokens: 0,
    output_tokens: 0,
    unknown_token_calls: 0,
    latency_ms: 0,
    attempt_count: 0,
    retry_count: 0,
    next_version: 1,
    created_at: now,
    updated_at: now,
  };
}
