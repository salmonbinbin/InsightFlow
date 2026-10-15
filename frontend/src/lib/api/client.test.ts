import { describe, it, expect, vi, beforeEach } from 'vitest';
import { InsightFlowClient } from './client';
import { MockInsightFlowClient } from './mock';
import type {
  PrdContent,
  PrdPatchRequest,
  RequirementPatchRequest,
} from './types';

function jsonResponse(body: unknown, init: { status?: number; statusText?: string; requestId?: string } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    statusText: init.statusText ?? 'OK',
    headers: { 'Content-Type': 'application/json', ...(init.requestId ? { 'X-Request-ID': init.requestId } : {}) },
  });
}

describe('InsightFlowClient', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
  });

  it('defaults to same-origin /api/projects (no env var)', async () => {
    // The browser MUST go through the Next.js rewrite at /api/* so that the
    // backend's missing CORS configuration is bypassed. The previous default
    // (`http://127.0.0.1:8000`) would have hit the backend directly and
    // tripped CORS. This test pins the same-origin contract.
    fetchMock.mockResolvedValueOnce(jsonResponse([{ id: 'p1', name: 'P', product_name: 'P' }]));
    const client = new InsightFlowClient({ fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listProjects();
    expect(res.error).toBeNull();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/projects');
    expect((init as RequestInit).method).toBe('GET');
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers['X-Request-ID']).toBeTruthy();
  });

  it('serialises JSON body for POST /projects', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'p1', name: 'X', product_name: 'Y' }, { status: 201, statusText: 'Created' }));
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.createProject({ name: 'X', product_name: 'Y' });
    expect(res.data?.id).toBe('p1');
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ name: 'X', product_name: 'Y' });
    expect((init as RequestInit).headers).toMatchObject({ 'Content-Type': 'application/json' });
  });

  it('strips a trailing /api from an explicit absolute baseUrl', async () => {
    // Some deployments point the client at "https://api.example.com/api" so
    // that the per-method path "/api/projects" still resolves correctly. We
    // must not produce "/api/api/projects".
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    const client = new InsightFlowClient({
      baseUrl: 'http://api.local/api',
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    await client.listProjects();
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://api.local/api/projects');
    expect(url).not.toContain('/api/api/');
  });

  it('uses the absolute baseUrl as-is when no /api suffix is present', async () => {
    // Direct backend integration tests (no Next.js rewrite in front) point
    // the client at the backend origin. The per-method path still starts
    // with /api, so the final URL is the origin plus /api/projects.
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    const client = new InsightFlowClient({
      baseUrl: 'http://api.local',
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    await client.listProjects();
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://api.local/api/projects');
  });

  it('passes pagination through to /projects/{id}/feedback', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, limit: 50, offset: 50 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.listFeedback('p1', { limit: 50, offset: 50 });
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/projects/p1/feedback?');
    expect(url).toContain('limit=50');
    expect(url).toContain('offset=50');
  });

  it('clamps page size to backend bounds (1..200)', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], total: 0, limit: 200, offset: 0 }));
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.listFeedback('p1', { limit: 9999 });
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('limit=200');
  });

  it('produces a friendly error for 422 with detail array', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { detail: [{ msg: 'feedback_id is required', loc: ['body', 0, 'feedback_id'] }] },
        { status: 422, statusText: 'Unprocessable' },
      ),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.importFeedback('p1', new File(['x'], 'x.csv'));
    expect(res.error).not.toBeNull();
    expect(res.error?.status).toBe(422);
    // The friendly derivation only reads .msg when detail is a single object;
    // the FastAPI 422 detail is an array of {msg, loc}. We assert that we
    // surface a non-empty message rather than the generic fallback, AND that
    // the raw detail array is preserved on the error body for diagnostics.
    expect(res.error?.message).toBeTruthy();
    const body = res.error?.body as { detail?: unknown };
    expect(Array.isArray(body.detail)).toBe(true);
  });

  it('falls back to a Chinese error message on 500', async () => {
    fetchMock.mockResolvedValueOnce(new Response('boom', { status: 500, statusText: 'Server Error' }));
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listProjects();
    expect(res.error?.message).toMatch(/服务/);
  });

  it('handles network failure gracefully', async () => {
    fetchMock.mockRejectedValueOnce(new Error('ECONNREFUSED'));
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listProjects();
    expect(res.error).not.toBeNull();
    expect(res.error?.status).toBe(0);
    expect(res.error?.message).toBe('ECONNREFUSED');
  });

  it('forwards AbortSignal to fetch', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([{ id: 'p1', name: 'P', product_name: 'P' }]));
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const controller = new AbortController();
    await client.listProjects(controller.signal);
    const [, init] = fetchMock.mock.calls[0]!;
    expect((init as RequestInit).signal).toBe(controller.signal);
  });

  it('does not send unsupported feedback filter params (search/sort)', async () => {
    // Backend only accepts limit/offset today. The client must not append
    // search/sort/user_type to the request URL or it would silently look
    // like a real filter while the server ignores those values.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.listFeedback('p1', {
      limit: 25,
      offset: 0,
      search: 'login',
      sort: '-created_at',
    });
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/projects/p1/feedback?');
    expect(url).toContain('limit=25');
    expect(url).toContain('offset=0');
    expect(url).not.toContain('search=');
    expect(url).not.toContain('sort=');
    expect(url).not.toContain('user_type=');
    expect(url).not.toContain('source=');
  });

  // ---------- Phase 7 B2: Requirements (real contract) ----------

  it('listProjectRequirements targets /projects/{id}/requirements (NOT /insights/{id}/requirements)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listProjectRequirements('proj-1', { limit: 25, offset: 0 });
    expect(res.error).toBeNull();
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/projects/proj-1/requirements?');
    expect(url).not.toContain('/api/insights/');
    expect(url).toContain('limit=25');
    expect(url).toContain('offset=0');
  });

  it('getRequirement targets /requirements/{id}', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 'req-1', project_id: 'proj-1', status: 'draft' }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.getRequirement('req-1');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/requirements/req-1');
    expect((init as RequestInit).method).toBe('GET');
  });

  it('patchRequirement sends PATCH /requirements/{id} with the real contract', async () => {
    // The body must contain BOTH optional keys when set: status and content.
    // The previous B1 placeholder used fake fields ({title, description,
    // acceptance_criteria, priority}) which the backend would have 422'd.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 'req-1', project_id: 'proj-1', status: 'reviewing' }, { status: 200 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    // Explicit ``RequirementPatchRequest`` annotation: TypeScript otherwise
    // widens ``status: 'reviewing'`` to ``string`` and the literal's
    // ``success_metrics[].type`` / ``risks[].category`` likewise widen to
    // ``string`` — none of which is assignable to the strict-union
    // RequirementPatchRequest the client expects. Annotating the body
    // also forces this test to drift when the contract changes, instead
    // of silently compiling a body that the backend would 422.
    const body: RequirementPatchRequest = {
      status: 'reviewing',
      content: {
        name: 'Refined requirement name',
        problem: 'Real problem statement',
        target_user: 'Real target user',
        user_story: 'As a ...',
        solution_hypothesis: 'Hypothesis',
        success_metrics: [
          {
            name: 'Engagement',
            type: 'metric_definition',
            sources: ['run-1'],
            note: 'Synthetic note',
          },
        ],
        risks: [
          {
            category: 'known',
            description: 'Coverage may be empty',
            evidence_ids: [],
            mitigation: 'Surface as Not Available',
          },
        ],
        evidence_references: [
          { external_id: 'fb-1', usage: 'representative quote' },
        ],
      },
    };
    const res = await client.patchRequirement('req-1', body);
    expect(res.error).toBeNull();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/requirements/req-1');
    expect((init as RequestInit).method).toBe('PATCH');
    const sent = JSON.parse((init as RequestInit).body as string);
    expect(sent).toEqual(body);
    // Specifically guard against the legacy B1 contract surfacing again.
    expect(sent).not.toHaveProperty('title');
    expect(sent).not.toHaveProperty('description');
    expect(sent).not.toHaveProperty('acceptance_criteria');
    expect(sent).not.toHaveProperty('priority');
  });

  it('createRequirement posts an empty body to /insights/{id}/requirement', async () => {
    // The backend's RequirementCreateRequest has no required fields, so the
    // client intentionally sends {} rather than synthesising placeholder
    // content. The PM later edits via patchRequirement.
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { id: 'req-1', project_id: 'proj-1', insight_id: 'ins-1', status: 'draft' },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.createRequirement('ins-1');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/insights/ins-1/requirement');
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });

  // ---------- Phase 7 C1: PRD real contract ----------

  it('listProjectPrds targets /projects/{id}/prds with limit/offset', async () => {
    // Phase 7 D1 — project-level PRD index that survives a page
    // refresh on /prds. Must NOT hit /requirements/{id}/prds (the
    // per-requirement history endpoint, which returns PrdPage with
    // active_prd_id / active_version). The project index endpoint
    // has no active-pointer field.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, limit: 25, offset: 25 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listProjectPrds('proj-1', { limit: 25, offset: 25 });
    expect(res.error).toBeNull();
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/projects/proj-1/prds?');
    expect(url).not.toContain('/api/requirements/');
    expect(url).not.toContain('/api/insights/');
    expect(url).toContain('limit=25');
    expect(url).toContain('offset=25');
  });

  it('listPrds returns a PrdPage envelope (NOT a bare array)', async () => {
    // Per-requirement PRD history. The backend wraps the slice in
    // a PrdPage envelope with ``active_prd_id`` / ``active_version``
    // so the caller can render a stable active pointer regardless
    // of pagination. The previous C0 stub returned a bare array
    // and silently dropped the active pointer.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
        active_prd_id: 'prd-active',
        active_version: 2,
      }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listPrds('req-1');
    expect(res.error).toBeNull();
    expect(res.data?.active_prd_id).toBe('prd-active');
    expect(res.data?.active_version).toBe(2);
    expect(Array.isArray(res.data?.items)).toBe(true);
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/requirements/req-1/prds');
  });

  it('getPrd targets /prds/{id}', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 'prd-1', requirement_id: 'req-1', version: 1, status: 'draft' }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.getPrd('prd-1');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/prds/prd-1');
    expect((init as RequestInit).method).toBe('GET');
  });

  it('createPrd posts the real contract {content?: PRDContent}, NOT {body: string}', async () => {
    // The previous C0 stub sent ``{ body: string }`` and would have
    // 422'd because the backend reads ``content: PRDContent``. This
    // test pins the structured-payload contract — we must NOT ship
    // the legacy ``body`` key, and an absent ``content`` is a valid
    // request (the AI client generates the PRD in that case).
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { id: 'prd-1', requirement_id: 'req-1', version: 1, status: 'draft' },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    // Annotation forces this test to drift if the contract changes,
    // rather than silently widening the body literal to ``string``
    // and letting the backend reject it later.
    const content: PrdContent = buildSamplePrdContent();
    const res = await client.createPrd('req-1', { content });
    expect(res.error).toBeNull();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/requirements/req-1/prds');
    expect((init as RequestInit).method).toBe('POST');
    const sent = JSON.parse((init as RequestInit).body as string);
    expect(sent).toEqual({ content });
    // Specifically guard against the legacy C0 contract surfacing again.
    expect(sent).not.toHaveProperty('body');
  });

  it('createPrd without content sends {} (AI-generated path)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { id: 'prd-1', requirement_id: 'req-1', version: 1, status: 'draft' },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.createPrd('req-1');
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });

  it('patchPrd sends PATCH /prds/{id} with {status?, content?}', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 'prd-1', requirement_id: 'req-1', version: 1, status: 'reviewing' }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const body: PrdPatchRequest = { status: 'reviewing' };
    await client.patchPrd('prd-1', body);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/prds/prd-1');
    expect((init as RequestInit).method).toBe('PATCH');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual(body);
  });

  it('acceptPrd posts an empty body to /prds/{id}/accept', async () => {
    // PRDAcceptRequest is empty by design (``extra=forbid``); any
    // submitted key already returns 422 before the service layer
    // runs. The client must therefore send exactly ``{}``.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 'prd-1', requirement_id: 'req-1', version: 1, status: 'accepted' }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.acceptPrd('prd-1');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/prds/prd-1/accept');
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });

  // ---------- Phase 7 D2a: Evaluation read contract ----------

  it('listProjectEvaluations targets /projects/{id}/evaluations with limit/offset', async () => {
    // Phase 7 D1 project-scoped EvaluationRun index. The route
    // encodes ``project_id`` in the path — it MUST NOT appear as
    // a query parameter, and the per-run metric / case endpoints
    // (which use ``/api/evaluations/{run_id}/...``) MUST NOT be
    // hit by this method.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, limit: 25, offset: 25 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listProjectEvaluations('proj-1', { limit: 25, offset: 25 });
    expect(res.error).toBeNull();
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/projects/proj-1/evaluations?');
    expect(url).not.toContain('/api/evaluations/');
    expect(url).toContain('limit=25');
    expect(url).toContain('offset=25');
    // The project_id is in the path — never forwarded as a query.
    expect(url).not.toContain('project_id=');
  });

  it('getEvaluation forwards optional project_id query (cross-project isolation)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 'run-1', project_id: 'proj-1' }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.getEvaluation('run-1', { project_id: 'proj-1' });
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/evaluations/run-1?');
    expect(url).toContain('project_id=proj-1');
  });

  it('getEvaluation omits project_id query when caller did not supply it', async () => {
    // Single-user mode — the backend returns the row
    // unconditionally. The frontend MUST NOT send ``project_id=``
    // when the caller didn't pass it, otherwise an empty string
    // would force a project match against nothing and the run
    // would 404 spuriously.
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'run-1' }));
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.getEvaluation('run-1');
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/evaluations/run-1');
    expect(url).not.toContain('project_id');
  });

  it('listEvaluationMetrics forwards project_id + limit + offset', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, limit: 10, offset: 5 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.listEvaluationMetrics('run-1', {
      project_id: 'proj-1',
      limit: 10,
      offset: 5,
    });
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/evaluations/run-1/metrics?');
    expect(url).toContain('project_id=proj-1');
    expect(url).toContain('limit=10');
    expect(url).toContain('offset=5');
  });

  it('listEvaluationCases forwards project_id + limit + offset', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, limit: 10, offset: 5 }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    await client.listEvaluationCases('run-1', {
      project_id: 'proj-1',
      limit: 10,
      offset: 5,
    });
    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/evaluations/run-1/cases?');
    expect(url).toContain('project_id=proj-1');
    expect(url).toContain('limit=10');
    expect(url).toContain('offset=5');
  });

  it('preserves EvaluationMetric value=0 vs value=null in the response shape', async () => {
    // Critical for the UI: a real zero must NOT collapse to
    // "Not Measured". Backend ships
    //   value: 0, value_display: '0.000000'
    // for measured zeros and
    //   value: null, value_display: 'NOT MEASURED'
    // otherwise. Both rows must round-trip verbatim so the page
    // can branch on ``value === null`` (or ``value_display``).
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [
          {
            id: 'm1',
            run_id: 'run-1',
            project_id: 'proj-1',
            stage: 'classification',
            metric_name: 'accuracy',
            metric_kind: 'classification_metric',
            sample_count: 100,
            value: 0,
            value_display: '0.000000',
            details: {},
            created_at: '2026-01-01T00:00:00Z',
          },
          {
            id: 'm2',
            run_id: 'run-1',
            project_id: 'proj-1',
            stage: 'classification',
            metric_name: 'f1_unmeasured',
            metric_kind: 'classification_metric',
            sample_count: 0,
            value: null,
            value_display: 'NOT MEASURED',
            details: {},
            created_at: '2026-01-01T00:00:01Z',
          },
        ],
        total: 2,
        limit: 50,
        offset: 0,
      }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listEvaluationMetrics('run-1');
    expect(res.error).toBeNull();
    expect(res.data?.items[0]?.value).toBe(0);
    expect(res.data?.items[0]?.value_display).toBe('0.000000');
    expect(res.data?.items[1]?.value).toBeNull();
    expect(res.data?.items[1]?.value_display).toBe('NOT MEASURED');
  });

  it('preserves dataset_label_status verbatim on EvaluationRunRead', async () => {
    // The brief requires the dataset provenance marker to surface
    // as-is so a caller can never silently treat the demo dataset
    // as ground-truth. The TS type keeps the field as a free
    // string so the literal
    // ``synthetic_reference_labels_unreviewed_no_human_review``
    // round-trips without being mapped to a friendly string.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: 'run-1',
        project_id: 'proj-1',
        evaluation_type: 'classification_metric',
        stage: 'classification',
        status: 'completed',
        dataset_name: 'demo',
        dataset_version: 'v1',
        dataset_content_hash: 'h',
        dataset_provenance: 'synthetic',
        dataset_split: 'test',
        dataset_label_status:
          'synthetic_reference_labels_unreviewed_no_human_review',
        calculation_version: 'cv1',
        input_count: 1,
        processed_count: 1,
        success_count: 1,
        failed_count: 0,
        unknown_token_calls: 0,
        attempt_count: 1,
        retry_count: 0,
        created_at: '2026-01-01T00:00:00Z',
      }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.getEvaluation('run-1');
    expect(res.data?.dataset_label_status).toBe(
      'synthetic_reference_labels_unreviewed_no_human_review',
    );
  });

  it('preserves failed_stage on EvaluationCaseRead (failure + non-failure rows)', async () => {
    // ``failed_stage`` is the backend's derived convenience field
    // for the failure dashboard. It MUST round-trip verbatim so
    // the UI can filter "failed at stage X" cases without
    // recomputing ``passed === false ? stage : null`` client-side.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [
          {
            id: 'c1',
            run_id: 'run-1',
            project_id: 'proj-1',
            case_id: 'case-1',
            stage: 'classification',
            input_hash: 'h1',
            expected_hash: 'h2',
            actual_hash: 'h3',
            passed: false,
            error_type: 'mismatch',
            eval_source: 'rule',
            notes: null,
            created_at: '2026-01-01T00:00:00Z',
            failed_stage: 'classification',
          },
          {
            id: 'c2',
            run_id: 'run-1',
            project_id: 'proj-1',
            case_id: 'case-2',
            stage: 'classification',
            input_hash: null,
            expected_hash: null,
            actual_hash: null,
            passed: null,
            error_type: null,
            eval_source: 'rule',
            notes: null,
            created_at: '2026-01-01T00:00:01Z',
            failed_stage: null,
          },
        ],
        total: 2,
        limit: 50,
        offset: 0,
      }),
    );
    const client = new InsightFlowClient({ baseUrl: '/api', fetchImpl: fetchMock as unknown as typeof fetch });
    const res = await client.listEvaluationCases('run-1');
    expect(res.data?.items[0]?.failed_stage).toBe('classification');
    expect(res.data?.items[1]?.failed_stage).toBeNull();
  });
});

describe('MockInsightFlowClient — Evaluation (Phase 7 D2a)', () => {
  // The real-client block above pins the wire contract; this block
  // pins the mock's IN-TEST contract so any future regression in
  // filtering / pagination / log shape is caught even when no
  // page test exercises that specific path.
  it('listProjectEvaluations filters by project_id + sorts created_at DESC + logs call', async () => {
    const mock = new MockInsightFlowClient();
    const older = buildSampleEvaluationRun({
      id: 'run-older',
      project_id: 'proj-a',
      created_at: '2026-01-01T00:00:00Z',
    });
    const newer = buildSampleEvaluationRun({
      id: 'run-newer',
      project_id: 'proj-a',
      created_at: '2026-01-02T00:00:00Z',
    });
    const foreign = buildSampleEvaluationRun({
      id: 'run-foreign',
      project_id: 'proj-b',
      created_at: '2026-01-03T00:00:00Z',
    });
    mock.evaluationRuns.push(older, newer, foreign);

    const res = await mock.listProjectEvaluations('proj-a', { limit: 25, offset: 0 });
    expect(res.error).toBeNull();
    expect(res.data?.total).toBe(2);
    // Newer first, then older — the foreign project row is excluded.
    expect(res.data?.items.map((r) => r.id)).toEqual(['run-newer', 'run-older']);
    // callLog records the GET with path + limit/offset query.
    const last = mock.callLog[mock.callLog.length - 1]!;
    expect(last).toMatchObject({
      method: 'GET',
      path: '/api/projects/proj-a/evaluations',
    });
    expect(last.body).toMatchObject({ limit: 25, offset: 0 });
  });

  it('listProjectEvaluations paginates with stable id DESC tiebreak', async () => {
    const mock = new MockInsightFlowClient();
    const base = '2026-01-01T00:00:00Z';
    const r1 = buildSampleEvaluationRun({ id: 'r1', project_id: 'proj-a', created_at: base });
    const r2 = buildSampleEvaluationRun({ id: 'r2', project_id: 'proj-a', created_at: base });
    const r3 = buildSampleEvaluationRun({ id: 'r3', project_id: 'proj-a', created_at: base });
    mock.evaluationRuns.push(r1, r2, r3);

    const page1 = await mock.listProjectEvaluations('proj-a', { limit: 2, offset: 0 });
    expect(page1.data?.items.map((r) => r.id)).toEqual(['r3', 'r2']);
    const page2 = await mock.listProjectEvaluations('proj-a', { limit: 2, offset: 2 });
    expect(page2.data?.items.map((r) => r.id)).toEqual(['r1']);
    expect(page2.data?.total).toBe(3);
  });

  it('getEvaluation enforces project_id cross-project isolation (404 on mismatch)', async () => {
    const mock = new MockInsightFlowClient();
    mock.evaluationRuns.push(
      buildSampleEvaluationRun({ id: 'run-1', project_id: 'proj-a' }),
    );

    const ok = await mock.getEvaluation('run-1', { project_id: 'proj-a' });
    expect(ok.error).toBeNull();
    expect(ok.data?.id).toBe('run-1');

    const denied = await mock.getEvaluation('run-1', { project_id: 'proj-b' });
    expect(denied.error).not.toBeNull();
    expect(denied.error?.status).toBe(404);

    // No project_id supplied → single-user mode, returns the row
    // unconditionally.
    const bypass = await mock.getEvaluation('run-1');
    expect(bypass.error).toBeNull();
    expect(bypass.data?.id).toBe('run-1');

    // Unknown run id → 404 regardless of project_id.
    const missing = await mock.getEvaluation('run-missing', { project_id: 'proj-a' });
    expect(missing.error?.status).toBe(404);
  });

  it('listEvaluationMetrics preserves value=0 vs value=null + paginates', async () => {
    const mock = new MockInsightFlowClient();
    mock.evaluationRuns.push(
      buildSampleEvaluationRun({ id: 'run-1', project_id: 'proj-a' }),
    );
    mock.evaluationMetrics.push({
      id: 'm1',
      run_id: 'run-1',
      project_id: 'proj-a',
      stage: 'classification',
      metric_name: 'accuracy',
      metric_kind: 'classification_metric',
      sample_count: 100,
      value: 0,
      value_display: '0.000000',
      details: {},
      created_at: '2026-01-01T00:00:00Z',
    });
    mock.evaluationMetrics.push({
      id: 'm2',
      run_id: 'run-1',
      project_id: 'proj-a',
      stage: 'classification',
      metric_name: 'f1_unmeasured',
      metric_kind: 'classification_metric',
      sample_count: 0,
      value: null,
      value_display: 'NOT MEASURED',
      details: {},
      created_at: '2026-01-01T00:00:01Z',
    });
    mock.evaluationMetrics.push({
      id: 'm3',
      run_id: 'run-1',
      project_id: 'proj-a',
      stage: 'classification',
      metric_name: 'precision',
      metric_kind: 'classification_metric',
      sample_count: 100,
      value: 0.81,
      value_display: '0.810000',
      details: {},
      created_at: '2026-01-01T00:00:02Z',
    });

    const page1 = await mock.listEvaluationMetrics('run-1', {
      project_id: 'proj-a',
      limit: 2,
      offset: 0,
    });
    expect(page1.error).toBeNull();
    expect(page1.data?.total).toBe(3);
    expect(page1.data?.items).toHaveLength(2);
    // 0 is preserved verbatim, not collapsed to null.
    expect(page1.data?.items[0]?.value).toBe(0);
    expect(page1.data?.items[0]?.value_display).toBe('0.000000');
    expect(page1.data?.items[1]?.value).toBeNull();
    expect(page1.data?.items[1]?.value_display).toBe('NOT MEASURED');

    const page2 = await mock.listEvaluationMetrics('run-1', {
      project_id: 'proj-a',
      limit: 2,
      offset: 2,
    });
    expect(page2.data?.items[0]?.value).toBe(0.81);
    expect(page2.data?.items[0]?.value_display).toBe('0.810000');
  });

  it('listEvaluationCases forwards project_id mismatch → 404', async () => {
    const mock = new MockInsightFlowClient();
    mock.evaluationRuns.push(
      buildSampleEvaluationRun({ id: 'run-1', project_id: 'proj-a' }),
    );
    mock.evaluationCases.push({
      id: 'c1',
      run_id: 'run-1',
      project_id: 'proj-a',
      case_id: 'case-1',
      stage: 'classification',
      input_hash: 'h1',
      expected_hash: 'h2',
      actual_hash: 'h3',
      passed: false,
      error_type: 'mismatch',
      eval_source: 'rule',
      notes: null,
      created_at: '2026-01-01T00:00:00Z',
      failed_stage: 'classification',
    });

    const ok = await mock.listEvaluationCases('run-1', { project_id: 'proj-a' });
    expect(ok.error).toBeNull();
    expect(ok.data?.items[0]?.failed_stage).toBe('classification');

    const denied = await mock.listEvaluationCases('run-1', { project_id: 'proj-b' });
    expect(denied.error?.status).toBe(404);

    const missing = await mock.listEvaluationCases('run-missing', {
      project_id: 'proj-a',
    });
    expect(missing.error?.status).toBe(404);
  });
});

/* ---------- helpers ---------- */

/** Build a minimal-but-valid PRDContent so the createPrd test
 *  exercises the structured-payload shape end-to-end without
 *  hand-writing 13 sections inline. The backend ``extra=forbid``
 *  schema rejects unknown keys, so this helper must stay aligned
 *  with backend/app/schemas/requirement_prd.py:PRDContent. */
function buildSamplePrdContent(): PrdContent {
  const ev = (text: string): { text: string; evidence_ids: string[] } => ({
    text,
    evidence_ids: ['ev-1'],
  });
  return {
    background: ev('background'),
    problem: ev('problem'),
    target_user: ev('target user'),
    user_story: ev('user story'),
    product_goal: ev('product goal'),
    proposed_solution: { text: 'solution', is_proposed: true, evidence_ids: ['ev-1'] },
    functional_requirements: [{ text: 'FR1', evidence_ids: ['ev-1'] }],
    user_flow: ev('user flow'),
    edge_cases: [
      { category: 'empty_state', description: 'No data', evidence_ids: ['ev-1'] },
    ],
    acceptance_criteria: [
      { text: 'AC1', verifiable: true, note: '', evidence_ids: ['ev-1'] },
    ],
    success_metrics: [
      { name: 'Metric 1', type: 'metric_definition', note: '', sources: ['run-1'] },
    ],
    risks: [
      { category: 'known', description: 'Risk', evidence_ids: [], mitigation: '' },
    ],
    mvp_scope: { in_scope: 'in', out_of_scope: 'out' },
    evidence_sources: ['run-1'],
    evidence_ids: ['ev-1'],
  };
}

/** Build a minimal-but-valid EvaluationRunRead so the mock
 *  contract tests can seed fixtures without hand-writing every
 *  field. The backend ``extra=forbid`` schema rejects unknown
 *  keys, so this helper must stay aligned with
 *  backend/app/schemas/evaluation.py:EvaluationRunRead. */
function buildSampleEvaluationRun(opts: {
  id: string;
  project_id: string;
  created_at?: string;
}) {
  const created_at = opts.created_at ?? '2026-01-01T00:00:00Z';
  return {
    id: opts.id,
    project_id: opts.project_id,
    evaluation_type: 'classification_metric',
    stage: 'classification',
    status: 'completed',
    dataset_name: 'demo',
    dataset_version: 'v1',
    dataset_content_hash: 'mock-hash',
    dataset_provenance: 'synthetic',
    dataset_split: 'test',
    dataset_label_status:
      'synthetic_reference_labels_unreviewed_no_human_review',
    model: 'mock-model',
    prompt_version: 'mock-prompt-v1',
    pipeline_version: 'mock-pipeline-v1',
    calculation_version: 'mock-calculation-v1',
    input_count: 1,
    processed_count: 1,
    success_count: 1,
    failed_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    unknown_token_calls: 0,
    latency_ms: 0,
    total_cost_usd: 0,
    attempt_count: 1,
    retry_count: 0,
    started_at: created_at,
    completed_at: created_at,
    created_at,
    error_message: null,
    notes: null,
  };
}
