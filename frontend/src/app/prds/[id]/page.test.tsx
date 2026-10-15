import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../../lib/api/mock';
import { setApiClient } from '../../../lib/api/client';
import type { InsightFlowClient } from '../../../lib/api/client';
import type { PrdRead, Requirement } from '../../../lib/api/types';

import PrdDetailPage from './page';

/* ---------- next/navigation mock ----------
 *
 * The detail page calls useParams() to resolve ``id`` from
 * ``/prds/[id]`` and useRouter().push() to navigate after Generate.
 * The mock follows the pattern used by insights/[id]/page.test.tsx.
 *
 * pushMock is hoisted because the page's regenerate handler
 * references the router instance that ``vi.mock('next/navigation')``
 * provides — vi.hoisted makes the function available before the
 * module factory runs.
 */
const { pushMock, mockUseParams } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  mockUseParams: vi.fn<() => { id: string }>(() => ({ id: 'prd-1' })),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), back: vi.fn() }),
  useParams: () => mockUseParams(),
}));

/* ---------- fixtures ---------- */

const BASE_REQ: Requirement = {
  id: 'req-1',
  project_id: 'proj_a',
  insight_id: 'insight-1',
  decision_id: 'decision-1',
  score_id: null,
  insight_status_snapshot: 'accepted',
  decision_status_snapshot: 'accepted',
  ai_priority_snapshot: 'P1',
  pm_priority: 'P1',
  pm_override_reason_snapshot: null,
  source_snapshot_hash: 'req-hash',
  name: 'Onboarding Activation',
  problem: 'Trial users have no clear next step after signup.',
  target_user: 'Trial users within their first 14 days.',
  user_story: 'As a trial user, I want a guided onboarding so I can find the core value.',
  solution_hypothesis: 'Step-by-step onboarding will lift D1 activation.',
  success_metrics: [
    {
      name: 'D1 activation',
      type: 'metric_definition',
      sources: ['mock'],
      note: 'login + core action within 24h',
    },
  ],
  risks: [
    {
      category: 'known',
      description: 'Empty cohort when project is brand new.',
      evidence_ids: [],
      mitigation: 'Surface as Not Available.',
    },
  ],
  evidence_references: [{ external_id: 'ext-1', usage: 'representative' }],
  status: 'draft',
  model: 'mock',
  prompt_version: 'v1',
  input_tokens: 0,
  output_tokens: 0,
  unknown_token_calls: 0,
  latency_ms: 0,
  attempt_count: 0,
  retry_count: 0,
  next_version: 1,
  created_at: '2026-10-14T01:00:00Z',
  updated_at: '2026-10-14T02:00:00Z',
};

const SAMPLE_PRD: PrdRead = {
  id: 'prd-1',
  requirement_id: 'req-1',
  project_id: 'proj_a',
  insight_id: 'insight-1',
  version: 1,
  status: 'reviewing',
  content: {
    background: { text: 'bg text', evidence_ids: ['ev-1'] },
    problem: { text: 'problem text', evidence_ids: ['ev-1'] },
    target_user: { text: 'target user', evidence_ids: ['ev-1'] },
    user_story: { text: 'user story text', evidence_ids: ['ev-1'] },
    product_goal: { text: 'product goal text', evidence_ids: ['ev-1'] },
    proposed_solution: { text: 'proposed solution text', is_proposed: true, evidence_ids: ['ev-1'] },
    functional_requirements: [{ text: 'FR1', evidence_ids: ['ev-1'] }],
    user_flow: { text: 'user flow text', evidence_ids: ['ev-1'] },
    edge_cases: [{ category: 'empty_state', description: 'empty state', evidence_ids: ['ev-1'] }],
    acceptance_criteria: [{ text: 'AC1', verifiable: true, note: 'note', evidence_ids: ['ev-1'] }],
    success_metrics: [{ name: 'metric 1', type: 'metric_definition', note: 'note', sources: ['run-1'] }],
    risks: [{ category: 'known', description: 'risk 1', evidence_ids: [], mitigation: 'mitigation' }],
    mvp_scope: { in_scope: 'in scope', out_of_scope: 'out of scope' },
    evidence_sources: ['run-1'],
    evidence_ids: ['ev-1'],
  },
  content_hash: 'hash-1',
  is_ai_generated_draft: true,
  grounding_warnings: [
    { code: 'MISSING_EVIDENCE', section: 'problem', message: 'No evidence matches problem', evidence_id: null },
  ],
  model: 'mock',
  prompt_version: 'mock-prompt-v1',
  input_tokens: 0,
  output_tokens: 0,
  unknown_token_calls: 0,
  latency_ms: 0,
  attempt_count: 0,
  retry_count: 0,
  accepted_at: null,
  accepted_by: null,
  created_at: '2026-10-14T01:00:00Z',
  updated_at: '2026-10-14T02:00:00Z',
};

interface HarnessOpts {
  prd?: PrdRead | null;
  prdList?: PrdRead[];
  activeProjectId?: string;
}

function buildHarness(opts: HarnessOpts = {}) {
  const client = new MockInsightFlowClient();
  setApiClient(client as unknown as InsightFlowClient);
  client.projects = [
    { id: 'proj_a', name: 'A', product_name: 'A' },
    { id: 'proj_b', name: 'B', product_name: 'B' },
  ];
  client.requirements = [BASE_REQ];
  if (opts.prdList !== undefined) {
    client.prdReadList = structuredClone(opts.prdList);
  } else if (opts.prd === null) {
    client.prdReadList = [];
  } else {
    client.prdReadList = [structuredClone(opts.prd ?? SAMPLE_PRD)];
  }
  return client;
}

function renderDetail(opts: HarnessOpts = {}) {
  const client = buildHarness(opts);
  const activeProject = opts.activeProjectId ?? 'proj_a';
  return {
    client,
    utils: render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId={activeProject}
      >
        <PrdDetailPage />
      </ProjectContextProvider>,
    ),
  };
}

describe('PrdDetailPage — Phase 7 C2', () => {
  beforeEach(() => {
    pushMock.mockClear();
  });

  afterEach(() => {
    setApiClient(null);
    mockUseParams.mockReturnValue({ id: 'prd-1' });
  });

  it('renders all 13 PRD sections, the meta grid, and grounding warnings', async () => {
    renderDetail();
    await waitFor(() => screen.getByTestId('prd-status-badge'));
    // Page surfaces version + status + AI draft + model + prompt version
    // + accepted_at in the meta grid without recomputing any snapshot field.
    // The version "v1" appears in multiple places (page title, meta-v,
    // history row), so we scope to the dedicated testid rather than a
    // bare ``getByText(/v1/)`` which would multi-match. The same scoped
    // assertion pattern is reused below for prompt-version chip to avoid
    // matching the literal model id.
    expect(screen.getByTestId('prd-page-title').textContent).toContain('v1');
    expect(screen.getByTestId('prd-current-version').textContent).toContain('v1');
    expect(screen.getByText('复核中')).toBeTruthy();
    expect(screen.getByTestId('prd-ai-draft-flag').textContent).toContain('是');
    expect(screen.getByText('mock')).toBeTruthy();
    expect(screen.getByText('mock-prompt-v1')).toBeTruthy();
    // Every one of the 13 sections gets a data-testid from PrdBody.
    const sections = [
      'prd-section-background',
      'prd-section-problem',
      'prd-section-target-user',
      'prd-section-user-story',
      'prd-section-product-goal',
      'prd-section-proposed-solution',
      'prd-section-functional-requirements',
      'prd-section-user-flow',
      'prd-section-edge-cases',
      'prd-section-acceptance-criteria',
      'prd-section-success-metrics',
      'prd-section-risks',
      'prd-section-mvp-scope',
      'prd-section-evidence',
    ];
    for (const id of sections) {
      expect(screen.getByTestId(id)).toBeTruthy();
    }
    // Grounding warnings render at the page level, not inside PrdBody.
    expect(screen.getByTestId('grounding-warning-list').textContent).toContain('MISSING_EVIDENCE');
  });

  it('loads the PRD via GET /prds/{id} (no rebuild, no derivation)', async () => {
    const { client } = renderDetail();
    await waitFor(() => screen.getByTestId('prd-status-badge'));
    const gets = client.callLog.filter(
      (c) => c.method === 'GET' && c.path === '/api/prds/prd-1',
    );
    expect(gets.length).toBeGreaterThanOrEqual(1);
    // No PATCH /prds/{id} at this point — accepting is the next test.
    const patches = client.callLog.filter(
      (c) => c.method === 'PATCH' && c.path === '/api/prds/prd-1',
    );
    expect(patches).toHaveLength(0);
  });

  it('Regenerate POSTs /requirements/{id}/prds with {} body and routes to the new PRD', async () => {
    // Pre-load so the original PRD "prd-1" sits in history; clicking
    // regenerate adds a new row "prd_2" (mock's createPrd pushes onto
    // prdReadList and returns the new row with a fresh id).
    const { client } = renderDetail();
    await waitFor(() => screen.getByTestId('regenerate-prd'));

    await act(async () => {
      (screen.getByTestId('regenerate-prd') as HTMLButtonElement).click();
    });


    await waitFor(() => {
      const creates = client.callLog.filter(
        (c) => c.method === 'POST' && c.path === '/api/requirements/req-1/prds',
      );
      expect(creates).toHaveLength(1);
      // Body MUST be the empty object for the AI-generated path; the
      // backend treats `{}` as "let the AI client fill in the PRD".
      expect(creates[0]?.body).toEqual({});
      // Router push must target the new PRD id, not the original.
      expect(pushMock).toHaveBeenCalledWith('/prds/prd_2');
    });

    // After the new PRD lands in the mock list, history for the
    // requirement should also be re-queried (the page refetches
    // history once the createPrd call returns).
    await waitFor(() => {
      const lists = client.callLog.filter(
        (c) => c.method === 'GET' && c.path === '/api/requirements/req-1/prds',
      );
      expect(lists.length).toBeGreaterThanOrEqual(2);
    });
  });

  it('history keeps earlier versions visible after Regenerate', async () => {
    // Mount on prd-original (which IS in the fixture) instead of the
    // default mockUseParams id, so the history for req-1 shows the
    // archived v1 row before any regenerate click.
    const client = buildHarness();
    client.prdReadList = [
      { ...SAMPLE_PRD, id: 'prd-original', version: 1, status: 'archived' },
    ];
    setApiClient(client as unknown as InsightFlowClient);
    mockUseParams.mockReturnValue({ id: 'prd-original' });

    render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <PrdDetailPage />
      </ProjectContextProvider>,
    );

    // Wait for the initial history snapshot (single archived v1 row) to
    // settle. ``prd-history-list`` is now always rendered as a
    // container, so we gate on the rows — that's the actual data
    // contract we care about.
    await waitFor(() => {
      expect(screen.getAllByTestId('prd-history-row')).toHaveLength(1);
    });
    // Initial history has just 'prd-original'.
    expect(screen.getAllByTestId('prd-history-row')).toHaveLength(1);

    await act(async () => {
      (screen.getByTestId('regenerate-prd') as HTMLButtonElement).click();
    });

    // After Regen the mock's createPrd pushes the new row onto
    // prdReadList, listPrds returns both, and the page's
    // ``displayedHistory`` shows both rows. The earlier version
    // (status=archived) MUST still be present.
    await waitFor(() => {
      expect(screen.getAllByTestId('prd-history-row').length).toBeGreaterThanOrEqual(2);
    });
    // Sanity: the archived row's version is still visible. We scope
    // to the page-title testid (and also assert the archived row's
    // own version badge) so the assertion does not multi-match
    // every "v1" in the DOM — both refs + meta + history rows all
    // carry v1 here.
    expect(screen.getByTestId('prd-page-title').textContent).toContain('v1');
    expect(
      screen.getAllByTestId('prd-history-version').some(
        (el) => el.textContent === 'v1',
      ),
    ).toBe(true);
  });

  it('Accept POSTs /prds/{id}/accept with {} body and disables Accept but leaves Regenerate open', async () => {
    const { client } = renderDetail();
    await waitFor(() => screen.getByTestId('accept-prd'));

    await act(async () => {
      (screen.getByTestId('accept-prd') as HTMLButtonElement).click();
    });

    await waitFor(() => {
      const accepts = client.callLog.filter(
        (c) => c.method === 'POST' && c.path === '/api/prds/prd-1/accept',
      );
      expect(accepts).toHaveLength(1);
      expect(accepts[0]?.body).toEqual({});
    });
    // Accept itself MUST stay disabled — the row is now ``accepted``
    // and the backend refuses a second accept. Edit / PATCH (the
    // ``prd-edit-button``) are also disabled via ``isReadOnly``.
    // Regenerate, however, stays enabled: it spawns a NEW row via
    // POST /api/requirements/{id}/prds and the backend does not
    // gate Regenerate on the source PRD's status. The C2 contract
    // is explicit — old / accepted versions can seed a fresh
    // timeline via Regenerate, only Accept / PATCH are blocked.
    await waitFor(() => {
      expect((screen.getByTestId('accept-prd') as HTMLButtonElement).disabled).toBe(true);
    });
    expect((screen.getByTestId('regenerate-prd') as HTMLButtonElement).disabled).toBe(false);
    // Status badge flipped to "已采纳" via the res.data round-trip.
    expect(screen.getByTestId('prd-status-badge').textContent).toContain('已采纳');
  });

  it('refuses to display or mutate a PRD from a different project (C2 isolation)', async () => {
    // SAMPLE_PRD carries project_id 'proj_a', but the active project is
    // 'proj_b'. The page MUST surface the cross-project refusal and
    // refuse any action button click — no POST goes out for a foreign PRD.
    const { client } = renderDetail({ activeProjectId: 'proj_b' });

    await waitFor(() => {
      expect(screen.getByText(/PRD 与当前项目不匹配/)).toBeTruthy();
    });
    // Status badge must NOT render in this branch — the body is hidden
    // because the cross-project PRD could carry a status the active
    // project is not entitled to mutate.
    expect(screen.queryByTestId('prd-status-badge')).toBeNull();
    // Defense-in-depth: even if a stale render briefly exposes the
    // button, clicking it must not POST. Querying the testid is the
    // primary gate; null confirms the page deliberately omitted the CTA.
    const regen = screen.queryByTestId('regenerate-prd') as HTMLButtonElement | null;
    const accept = screen.queryByTestId('accept-prd') as HTMLButtonElement | null;
    expect(regen).toBeNull();
    expect(accept).toBeNull();
    // No POST should have been attempted either way. listPrds
    // (history) would still run, but no action-call method has its
    // signature on the wire.
    expect(
      client.callLog.filter(
        (c) =>
          c.method === 'POST' &&
          (c.path.includes('/accept') || c.path.endsWith('/prds') /* create */),
      ),
    ).toHaveLength(0);
  });

  it('renders an empty state when GET /prds/{id} returns 404', async () => {
    renderDetail({ prd: null });
    await waitFor(() => {
      // The empty state surfaces the PRD id in the description so the
      // PM can confirm which id they tried to load.
      expect(screen.getByText(/PRD 不存在/)).toBeTruthy();
    });
    // No action buttons reach the wire in this branch.
    expect(screen.queryByTestId('regenerate-prd')).toBeNull();
    expect(screen.queryByTestId('accept-prd')).toBeNull();
  });

  it('paginates the version history client-side and toggles prev/next controls', async () => {
    // HISTORY_PAGE_SIZE on the detail page is 5. Pre-load 8 versions
    // of the same PRD requirement so the second page actually has
    // rows. The mock's listPrds returns all of them (default
    // limit=50, offset=0); the page slices in render via
    // displayedHistory so the prev/next buttons exercise real
    // client-side pagination rather than a fake "see all" claim.
    const all: PrdRead[] = [];
    for (let i = 0; i < 8; i++) {
      all.push({
        ...SAMPLE_PRD,
        id: `prd-v${i + 1}`,
        version: i + 1,
        status: i === 7 ? 'reviewing' : 'draft',
      });
    }
    // The fixture ids are ``prd-v1``...``prd-v8`` (NOT ``prd-1``)
    // because the previous C1 detail page anchored everything on
    // the literal ``prd-1`` slot. The default ``mockUseParams``
    // value still points at ``prd-1`` (the bare fixture default),
    // so ``getPrd('prd-1')`` would 404 and the page would render
    // EmptyState instead of the version timeline — the pagination
    // controls and rows would never appear, surfacing the test as
    // "history is empty" rather than the real bug. Anchor the URL
    // to the first row so getPrd resolves and loadHistory returns
    // all 8 rows for the client-side slice.
    mockUseParams.mockReturnValue({ id: all[0]!.id });
    renderDetail({ prd: all[0]!, prdList: all });
    // The list container is now always rendered (C2 history
    // restructure moved the loading/empty/error branches inside the
    // UL); waiting for the rows themselves is the actual readiness
    // gate. ``waitFor`` polls with a default timeout (1s) and
    // succeeds once both rows and pagination controls settle in.
    await waitFor(() => {
      expect(screen.getAllByTestId('prd-history-row').length).toBeGreaterThan(0);
      expect(screen.getByTestId('history-prev')).toBeTruthy();
      expect(screen.getByTestId('history-next')).toBeTruthy();
    });

    // Page 1: prev disabled, next enabled, 5 rows visible.
    const prev = screen.getByTestId('history-prev') as HTMLButtonElement;
    const next = screen.getByTestId('history-next') as HTMLButtonElement;
    expect(prev.disabled).toBe(true);
    expect(next.disabled).toBe(false);
    expect(screen.getAllByTestId('prd-history-row')).toHaveLength(5);

    // Click next → the displayed slice shifts to rows 5..9 (3 rows).
    await act(async () => {
      next.click();
    });
    await waitFor(() => {
      expect(screen.getAllByTestId('prd-history-row')).toHaveLength(3);
    });
    expect(prev.disabled).toBe(false);
    // On the last page Next must disable; historyTotal=8, pageSize=5
    // → 2 pages, so page 2 is the last one.
    expect(next.disabled).toBe(true);

    // Back to page 1.
    await act(async () => {
      prev.click();
    });
    await waitFor(() => {
      expect(screen.getAllByTestId('prd-history-row')).toHaveLength(5);
    });
    expect(prev.disabled).toBe(true);
  });

  it('resets state when the URL id changes (stale-form regression guard)', async () => {
    // Mount on PRD 'prd-A' first, verify version renders as 1.
    const client = buildHarness();
    client.prdReadList = [
      { ...SAMPLE_PRD, id: 'prd-A', version: 1 },
      { ...SAMPLE_PRD, id: 'prd-B', version: 2 },
    ];
    setApiClient(client as unknown as InsightFlowClient);
    mockUseParams.mockReturnValue({ id: 'prd-A' });

    const utils = render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <PrdDetailPage />
      </ProjectContextProvider>,
    );

    await waitFor(() => screen.getByTestId('prd-status-badge'));
    // Confirm the page rendered v1 — i.e. prd-A loaded. We use the
    // dedicated ``prd-page-title`` testid to scope the assertion
    // since ``.page-title`` would otherwise multi-match the meta
    // grid's version chip and any history row carrying v1.
    expect(screen.getByTestId('prd-page-title').textContent).toContain('v1');

    // Switch the URL id. The reset effect keyed on prdId MUST wipe
    // every piece of state so the page can fetch prd-B cleanly.
    mockUseParams.mockReturnValue({ id: 'prd-B' });
    await act(async () => {
      utils.rerender(
        <ProjectContextProvider
          client={client as unknown as never}
          initialProjects={client.projects}
          initialActiveProjectId="proj_a"
        >
          <PrdDetailPage />
        </ProjectContextProvider>,
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId('prd-page-title').textContent).toContain('v2');
    });
    // getPrd must have been called for the new id (NOT just prd-A cached).
    const gets = client.callLog.filter(
      (c) => c.method === 'GET' && c.path === '/api/prds/prd-B',
    );
    expect(gets.length).toBeGreaterThanOrEqual(1);
  });

  it('surfaces a Regenerate error and refuses to navigate on failure', async () => {
    const client = buildHarness();
    setApiClient(client as unknown as InsightFlowClient);

    // Force a 503 on the create endpoint, mirroring an AI transport
    // failure. applyError uses the in-harness ``forceError`` slot;
    // we narrow it to the create path so other endpoints are unaffected.
    client.forceError = {
      path: '/api/requirements/req-1/prds',
      status: 503,
      message: 'AI client not configured',
    };
    // Override createPrd so the failure surfaces through the same
    // call surface the page uses. MUST call client.log first per the
    // MockInsightFlowClient test-harness rule, otherwise callLog stays
    // empty and the assertion below would fail for the wrong reason.
    const origCreate = client.createPrd.bind(client);
    client.createPrd = async (reqId, body, sig) => {
      client.log('POST', `/api/requirements/${reqId}/prds`, body);
      if (client.shouldError(`/api/requirements/${reqId}/prds`)) return client.applyError();
      return origCreate(reqId, body, sig);
    };

    render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <PrdDetailPage />
      </ProjectContextProvider>,
    );

    // Wait for the PRD to actually load before clicking regenerate.
    // The ``regenerate-prd`` button is rendered unconditionally
    // (no ``if (!prd)`` guard around the action section), so
    // gating only on its presence races against ``prd === null`` —
    // a click that fires before ``getPrd`` resolves hits the
    // ``if (!prd) return;`` early exit inside ``onRegenerate``
    // and the page never reaches ``setRegenerateError``. The
    // status badge only renders once ``prd`` is populated, which
    // is the real readiness signal we need here. This is not a
    // relaxed assertion — the original waitFor did not actually
    // establish "PRD loaded", it just confirmed the button exists
    // on the page; correcting the gate surfaces the contract.
    await waitFor(() => screen.getByTestId('prd-status-badge'));

    await act(async () => {
      (screen.getByTestId('regenerate-prd') as HTMLButtonElement).click();
    });


    await waitFor(() => {
      // The page now wraps the regenerate ErrorState in a div
      // carrying ``data-testid="regenerate-error"`` so the user-
      // visible failure banner (title="生成 PRD 失败",
      // role="alert") is reliable to query regardless of where
      // the text "生成 PRD 失败" might also surface (e.g. button
      // label during loading). Without this scoping the previous
      // ``getByText(/.../)`` could multi-match once multiple
      // alerts stack during a follow-up Accept attempt.
      const errorBanner = screen.getByTestId('regenerate-error');
      expect(errorBanner.textContent).toContain('生成 PRD 失败');
      expect(errorBanner.textContent).toContain('服务暂不可用');
      // role="alert" must surface the error to assistive tech —
      // PMs depending on screen readers MUST hear the failure
      // even when the visual banner is missed.
      expect(errorBanner.querySelector('[role="alert"]')).toBeTruthy();
    });
    // Critical: even though the regenerate call was made, the page
    // MUST NOT navigate — a failed regenerate must leave the user on
    // the same PRD id they were viewing.
    expect(pushMock).not.toHaveBeenCalled();
  });

  /* ---------- Phase 7 C3a — Edit / Save ---------- */

  it('keeps comma-separated evidence and source IDs editable across consecutive keystrokes', async () => {
    const { client } = renderDetail();
    await waitFor(() => screen.getByTestId('prd-status-badge'));
    await act(async () => {
      (screen.getByTestId('prd-edit-button') as HTMLButtonElement).click();
    });
    const evidence = screen.getByTestId('prd-draft-problem-evidence') as HTMLInputElement;
    const sources = screen.getByTestId('prd-draft-evidence-sources') as HTMLInputElement;
    for (const [input, first, second] of [
      [evidence, 'ev-1', 'ev-2'],
      [sources, 'run-1', 'run-2'],
    ] as const) {
      fireEvent.change(input, { target: { value: `${first},` } });
      expect(input.value).toBe(`${first},`);
      fireEvent.change(input, { target: { value: `${first}, ` } });
      expect(input.value).toBe(`${first}, `);
      fireEvent.change(input, { target: { value: `${first}, ${second}` } });
      expect(input.value).toBe(`${first}, ${second}`);
    }
    await act(async () => {
      (screen.getByTestId('prd-save-button') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      const patch = client.callLog.find((c) => c.method === 'PATCH' && c.path === '/api/prds/prd-1');
      const body = patch?.body as { content?: PrdRead['content'] } | undefined;
      expect(body?.content?.problem.evidence_ids).toEqual(['ev-1', 'ev-2']);
      expect(body?.content?.evidence_sources).toEqual(['run-1', 'run-2']);
    });
  });

  it('enters edit mode and persists a structured content patch via PATCH /prds/{id}', async () => {
    const { client } = renderDetail();
    await waitFor(() => screen.getByTestId('prd-status-badge'));

    // Enter edit mode. The editor swaps in for the read-only body
    // and the draft must mirror ``prd.content`` so the first
    // ``isDirty`` computation is false (the Save button stays
    // disabled until the PM actually changes something).
    await act(async () => {
      (screen.getByTestId('prd-edit-button') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      expect(screen.getByTestId('prd-editor')).toBeTruthy();
    });
    // No PATCH yet — we have not edited anything, so Save stays
    // disabled and the wire is quiet on the PATCH method.
    expect(screen.getByTestId('prd-save-button').hasAttribute('disabled')).toBe(true);

    // Mutate the Problem section text + evidence_ids, plus add a
    // second functional requirement. The structured payload MUST
    // round-trip: arrays stay as arrays, evidence_ids stay as
    // arrays (NOT collapsed into a free-text JSON blob).
    const newProblemText = 'updated problem statement from PM';
    const newEvidence = 'ev-pm-1, ev-pm-2';
    const problemTextArea = screen.getByTestId('prd-draft-problem-text') as HTMLTextAreaElement;
    const problemEvidenceInput = screen.getByTestId('prd-draft-problem-evidence') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(problemTextArea, { target: { value: newProblemText } });
      fireEvent.change(problemEvidenceInput, { target: { value: newEvidence } });
    });
    await act(async () => {
      (screen.getByTestId('prd-draft-fr-add') as HTMLButtonElement).click();
    });

    // Save now enabled because the draft diverged from prd.content.
    await waitFor(() => {
      expect(screen.getByTestId('prd-save-button').hasAttribute('disabled')).toBe(false);
    });

    await act(async () => {
      (screen.getByTestId('prd-save-button') as HTMLButtonElement).click();
    });

    await waitFor(() => {
      const patches = client.callLog.filter(
        (c) => c.method === 'PATCH' && c.path === '/api/prds/prd-1',
      );
      expect(patches).toHaveLength(1);
      const body = patches[0]?.body as {
        content?: {
          problem?: { text?: string; evidence_ids?: string[] };
          functional_requirements?: Array<{ text: string; evidence_ids: string[] }>;
        };
      };
      // ``status`` MUST NOT be sent — we only edit content here.
      // Sending a phantom status would force the backend through
      // a transition check and surface as a 409 for no reason.
      expect(body.content).toBeTruthy();
      expect(body).not.toHaveProperty('status');
      expect(body.content?.problem?.text).toBe(newProblemText);
      expect(body.content?.problem?.evidence_ids).toEqual(['ev-pm-1', 'ev-pm-2']);
      // functional_requirements grew from 1 to 2; both rows must
      // preserve their ``evidence_ids`` arrays.
      expect(body.content?.functional_requirements?.length).toBe(2);
      expect(body.content?.functional_requirements?.[1]?.text).toBe('');
      expect(body.content?.functional_requirements?.[1]?.evidence_ids).toEqual([]);
    });

    // Success banner surfaces; editor exits; preview flips to the
    // PM-authored Problem text. The page MUST NOT flash a false
    // success — we only flip state after res.data is in hand.
    await waitFor(() => {
      expect(screen.getByTestId('prd-save-success')).toBeTruthy();
    });
    // Editor is gone, read-only body is back.
    expect(screen.queryByTestId('prd-editor')).toBeNull();
    // The saved Problem section text shows in the read-only view.
    expect(screen.getByText(newProblemText)).toBeTruthy();
  });

  it('keeps the draft and shows an error on PATCH failure (no false success)', async () => {
    const client = buildHarness();
    setApiClient(client as unknown as InsightFlowClient);

    // Force a 422 on the PATCH endpoint, mirroring the backend's
    // grounding-validation response. ``applyError`` returns the
    // ApiError shape the page already knows how to classify.
    client.forceError = {
      path: '/api/prds/prd-1',
      status: 422,
      message: 'evidence_ids 引用未在允许的来源内',
    };
    const origPatch = client.patchPrd.bind(client);
    client.patchPrd = async (prdId, body, sig) => {
      client.log('PATCH', `/api/prds/${prdId}`, body);
      if (client.shouldError(`/api/prds/${prdId}`)) return client.applyError();
      return origPatch(prdId, body, sig);
    };

    render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <PrdDetailPage />
      </ProjectContextProvider>,
    );
    await waitFor(() => screen.getByTestId('prd-status-badge'));

    // Enter edit mode + edit Problem.
    await act(async () => {
      (screen.getByTestId('prd-edit-button') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('prd-editor'));
    const textArea = screen.getByTestId('prd-draft-problem-text') as HTMLTextAreaElement;
    await act(async () => {
      fireEvent.change(textArea, { target: { value: 'will fail on save' } });
    });
    await act(async () => {
      (screen.getByTestId('prd-save-button') as HTMLButtonElement).click();
    });

    await waitFor(() => {
      // The save error wrapper is the same pattern we use for the
      // regenerate error so screen-reader users hear the failure.
      const errorBanner = screen.getByTestId('prd-save-error');
      expect(errorBanner.textContent).toContain('保存 PRD 失败');
      expect(errorBanner.textContent).toContain('evidence_ids');
      expect(errorBanner.querySelector('[role="alert"]')).toBeTruthy();
      // CRITICAL: success banner must NOT also be in the DOM — a
      // false-positive success would silently drop the PM's edits.
      expect(screen.queryByTestId('prd-save-success')).toBeNull();
    });

    // Draft preserved: the editor is still mounted and still shows
    // the unsaved Problem text. The PM can fix the evidence list
    // and retry without re-typing the section.
    expect(screen.queryByTestId('prd-editor')).toBeTruthy();
    expect(
      (screen.getByTestId('prd-draft-problem-text') as HTMLTextAreaElement).value,
    ).toBe('will fail on save');
    // The mock's persisted PRD body is the source of truth for
    // "no overwrite happened": the override returns ``applyError``
    // BEFORE delegating to ``origPatch``, so ``client.prdReadList``
    // still carries the original fixture. We assert against the
    // mock's array directly rather than the DOM — Testing Library
    // can surface textarea values through ``getByText`` depending on
    // the DOM walk, which made the previous
    // ``queryByText('will fail on save')`` assertion ambiguous
    // (contradicting the textarea retention check above). The mock
    // state assertion is the unambiguous "no PATCH landed" signal.
    expect(client.prdReadList[0]?.content?.problem?.text).toBe('problem text');
  });

  it('keeps the Edit button disabled on an accepted PRD (read-only)', async () => {
    // ``status='accepted'`` matches the backend's
    // ``prd content immutable once accepted`` contract — the PM
    // must create a new version instead of editing in place. The
    // button stays in the DOM (with a ``title`` hint + the read-
    // only notice paragraph) so the UI can explain why it is
    // greyed out rather than silently vanishing.
    renderDetail({
      prd: { ...SAMPLE_PRD, status: 'accepted', accepted_at: '2026-10-14T03:00:00Z' },
    });
    await waitFor(() => screen.getByTestId('prd-status-badge'));
    const editBtn = screen.getByTestId('prd-edit-button') as HTMLButtonElement;
    expect(editBtn.disabled).toBe(true);
    // ``title`` carries the explicit reason so a hovering PM sees
    // why the action is unavailable — a missing title would
    // regress the accessible explanation.
    expect(editBtn.title).toContain('已采纳');
    // Read-only banner surfaces so the PM knows editing is
    // locked and a fresh version is needed (via Regenerate).
    expect(screen.getByText(/^本版本已采纳/)).toBeTruthy();
  });

  it('keeps the Edit button disabled on an archived PRD (read-only)', async () => {
    renderDetail({ prd: { ...SAMPLE_PRD, status: 'archived' } });
    await waitFor(() => screen.getByTestId('prd-status-badge'));
    const editBtn = screen.getByTestId('prd-edit-button') as HTMLButtonElement;
    expect(editBtn.disabled).toBe(true);
    expect(editBtn.title).toContain('已归档');
    expect(screen.getByText(/^本版本已归档/)).toBeTruthy();
  });

  it('hides the edit entry point on a cross-project PRD (project mismatch)', async () => {
    // SAMPLE_PRD lives in proj_a but the active project is proj_b.
    // The page surfaces the blocking ErrorState and refuses every
    // mutation; the Edit button must not even be reachable.
    const { client } = renderDetail({ activeProjectId: 'proj_b' });
    await waitFor(() => {
      expect(screen.getByText(/PRD 与当前项目不匹配/)).toBeTruthy();
    });
    expect(screen.queryByTestId('prd-edit-button')).toBeNull();
    expect(screen.queryByTestId('regenerate-prd')).toBeNull();
    expect(screen.queryByTestId('accept-prd')).toBeNull();
    expect(
      client.callLog.filter(
        (c) =>
          c.method === 'PATCH' ||
          (c.method === 'POST' && c.path.includes('/prds')),
      ),
    ).toHaveLength(0);
  });

  it('blocks Regenerate / Accept while a dirty draft is pending (no silent loss)', async () => {
    const { client } = renderDetail();
    await waitFor(() => screen.getByTestId('prd-status-badge'));

    await act(async () => {
      (screen.getByTestId('prd-edit-button') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('prd-editor'));
    await act(async () => {
      fireEvent.change(
        screen.getByTestId('prd-draft-problem-text') as HTMLTextAreaElement,
        { target: { value: 'unsaved edit' } },
      );
    });

    // Both Regenerate and Accept disable themselves once a draft
    // is dirty. Inline warning banner surfaces so the PM knows
    // why the buttons went grey.
    await waitFor(() => {
      expect(
        (screen.getByTestId('regenerate-prd') as HTMLButtonElement).disabled,
      ).toBe(true);
      expect(
        (screen.getByTestId('accept-prd') as HTMLButtonElement).disabled,
      ).toBe(true);
      expect(screen.getByTestId('prd-dirty-notice')).toBeTruthy();
    });

    // Force-click (defense-in-depth on the handler) — neither
    // POST /prds nor POST /prds/{id}/accept may have fired.
    await act(async () => {
      (screen.getByTestId('regenerate-prd') as HTMLButtonElement).click();
    });
    expect(
      client.callLog.filter(
        (c) =>
          (c.method === 'POST' && c.path.includes('/prds')) ||
          (c.method === 'POST' && c.path.includes('/accept')),
      ),
    ).toHaveLength(0);
    // Draft still intact.
    expect(
      (screen.getByTestId('prd-draft-problem-text') as HTMLTextAreaElement).value,
    ).toBe('unsaved edit');
  });

  it('wipes the in-flight draft on URL id change (no draft bleed across PRDs)', async () => {
    // Mount on prd-A with an active unsaved edit, then flip
    // ``mockUseParams`` to prd-B and rerender. The reset effect
    // must drop ``draftContent`` so prd-B never inherits prd-A's
    // edit. We assert by checking that prd-B is the new active
    // PRD and the editor is NOT mounted (the read-only body of
    // prd-B is the only sensible view).
    const client = buildHarness();
    client.prdReadList = [
      { ...SAMPLE_PRD, id: 'prd-A', version: 1 },
      { ...SAMPLE_PRD, id: 'prd-B', version: 2 },
    ];
    setApiClient(client as unknown as InsightFlowClient);
    mockUseParams.mockReturnValue({ id: 'prd-A' });

    const utils = render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <PrdDetailPage />
      </ProjectContextProvider>,
    );
    await waitFor(() => screen.getByTestId('prd-status-badge'));

    await act(async () => {
      (screen.getByTestId('prd-edit-button') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('prd-editor'));
    await act(async () => {
      fireEvent.change(
        screen.getByTestId('prd-draft-problem-text') as HTMLTextAreaElement,
        { target: { value: 'would leak across PRDs' } },
      );
    });

    // Switch URL id; the reset effect clears ``editing`` +
    // ``draftContent`` so the new page mounts clean.
    mockUseParams.mockReturnValue({ id: 'prd-B' });
    await act(async () => {
      utils.rerender(
        <ProjectContextProvider
          client={client as unknown as never}
          initialProjects={client.projects}
          initialActiveProjectId="proj_a"
        >
          <PrdDetailPage />
        </ProjectContextProvider>,
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId('prd-page-title').textContent).toContain('v2');
    });
    // The editor MUST NOT survive the URL switch.
    expect(screen.queryByTestId('prd-editor')).toBeNull();
    // Edit button is shown again (prd-B is in 'reviewing' state in
    // the fixture; status is 'reviewing' which is editable).
    expect(screen.getByTestId('prd-edit-button')).toBeTruthy();
  });
});
