import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../../lib/api/mock';
import { setApiClient } from '../../../lib/api/client';
import type { InsightFlowClient } from '../../../lib/api/client';
import {
  type DecisionOverrideReason,
  type DecisionPatchRequest,
  type FeedbackAnalysisEntry,
  type Insight,
  type InsightDecision,
  type InsightEvidenceEntry,
  type OpportunityScore,
} from '../../../lib/api/types';

const { pushMock, mockUseParams } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  mockUseParams: vi.fn<() => { id: string }>(() => ({ id: 'insight-1' })),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), back: vi.fn() }),
  useParams: () => mockUseParams(),
}));

import InsightDetailPage from './page';

/* ---------- fixtures ---------- */

const SAMPLE_INSIGHT: Insight = {
  id: 'insight-1',
  run_id: 'run-1',
  project_id: 'proj_a',
  cluster_id: 'cluster-1',
  clustering_run_id: 'clustering-1',
  title: 'Trial 用户首日激活率异常低',
  pain_point: '注册后不知下一步',
  affected_segment: '14 天试用用户',
  insight: '首次体验未引导到核心价值',
  recommendation: '增加 onboarding check-in',
  confidence: 0.6,
  status: 'draft',
  feedback_count: 5,
  evidence_count: 3,
  source_cluster_external_id: 'cluster-1',
  source_member_external_ids: ['ext-1', 'ext-2', 'ext-3'],
  model: 'gpt-4o-mini',
  prompt_version: 'v1',
  created_at: '2026-10-14T01:00:00Z',
};

const SAMPLE_EVIDENCE: InsightEvidenceEntry[] = [
  {
    id: 'ev-1',
    insight_id: 'insight-1',
    feedback_id: 'fb-1',
    feedback_external_id: 'ext-1',
    feedback_content: '注册之后没有提示，我没找到核心功能。',
    evidence_type: 'representative',
    relevance_score: 0.9,
    created_at: '2026-10-13T22:00:00Z',
    feedback_created_at: '2026-10-12T10:00:00Z',
    feedback_user_id: 'u-1',
    feedback_source: 'interview',
    feedback_analysis: {
      id: 'fa-1',
      feedback_id: 'fb-1',
      run_id: 'classification-1',
      feedback_type: 'pain_point',
      module: 'onboarding',
      intent: 'guidance_missing',
      pain_point: '注册后无引导',
      sentiment: 'negative',
      severity: 4,
      confidence: 0.85,
      created_at: '2026-10-13T21:00:00Z',
    } satisfies FeedbackAnalysisEntry,
  },
];

const SAMPLE_SCORE: OpportunityScore = {
  id: 'score-1',
  insight_id: 'insight-1',
  project_id: 'proj_a',
  classification_run_id: 'classification-1',
  source_insight_status: 'draft',
  source_snapshot_hash: 'hash-1',
  breakdown: {
    user_coverage: {
      raw_value: '0.42',
      score: '0.42',
      available: true,
      affected_count: 120,
      project_count: 1,
      feedback_count: 5,
    },
    severity: {
      raw_value: '4',
      score: '0.8',
      available: true,
    },
    growth: {
      raw_value: null,
      score: null,
      available: false,
      note: 'no history window',
    },
    user_value: {
      raw_value: '7',
      score: '0.7',
      available: true,
    },
    business_value: {
      raw_value: null,
      score: null,
      available: false,
      note: 'Pending PM Input',
    },
  },
  weights: {
    configured: {
      user_coverage: '0.30',
      severity: '0.30',
      growth: '0.10',
      user_value: '0.20',
      business_value: '0.10',
    },
    effective: {
      user_coverage: '0.40',
      severity: '0.40',
      user_value: '0.20',
    },
  },
  thresholds: {
    ai_priority_p0_below: '7.5',
    ai_priority_p1_below: '5',
  },
  business_value: null,
  total_score: '6.30',
  ai_priority: 'P1',
  ai_reasoning: '覆盖率 0.42，severity 4/5；growing 与 BV 暂未生成。',
  calculation_version: 'opportunity-score-v2',
  model: 'gpt-4o-mini',
  prompt_version: 'opportunity-score-v2',
  input_tokens: 0,
  output_tokens: 0,
  unknown_token_calls: 0,
  latency_ms: 0,
  attempt_count: 0,
  retry_count: 0,
};

function makeDecision(over: Partial<InsightDecision> = {}): InsightDecision {
  return {
    id: 'decision-1',
    insight_id: 'insight-1',
    project_id: 'proj_a',
    current_score_id: 'score-1',
    business_value: null,
    ai_priority: 'P1',
    pm_priority: null,
    status: 'pending_review',
    override_reason: null,
    override_note: null,
    version: 0,
    ...over,
  };
}

interface HarnessOpts {
  insight?: Insight | null;
  evidence?: InsightEvidenceEntry[];
  score?: OpportunityScore | null;
  scoreMissing?: boolean;
  decision?: InsightDecision | null;
  decisionMissing?: boolean;
  conflictOnPatch?: number;
}

interface HarnessHandle {
  client: MockInsightFlowClient;
  rerender: (opts: HarnessOpts) => void;
}

function renderInsightDetail(initial: HarnessOpts = {}): HarnessHandle {
  const client = new MockInsightFlowClient();
  // The page calls getApiClient() — a process-wide singleton — not the
  // context-provided client. Without setApiClient(...) the page would hit
  // the real fetch, which would explode in jsdom. Inject the mock into
  // the singleton and reset on teardown so cross-test bleed is impossible.
  setApiClient(client as unknown as InsightFlowClient);
  client.projects = [{ id: 'proj_a', name: 'A', product_name: 'Product A' }];
  client.insights = initial.insight === undefined ? [SAMPLE_INSIGHT] : initial.insight === null ? [] : [initial.insight];

  // Preload evidence score decision if provided.
  let lastEvidence: InsightEvidenceEntry[] = initial.evidence ?? [];
  let lastScore: OpportunityScore | null = initial.scoreMissing ? null : initial.score ?? SAMPLE_SCORE;
  let lastDecision: InsightDecision | null = initial.decisionMissing ? null : initial.decision ?? null;
  let lastConflict = initial.conflictOnPatch ?? 0;

  // Wire each call to the latest closure so rerender() can mutate behaviour.
  // Every override MUST call client.log(...) so callLog mirrors what the real
  // MockInsightFlowClient methods do; without that, tests asserting on
  // client.callLog see an empty array and fail for the wrong reason.
  client.getInsight = async () => {
    client.log('GET', '/api/insights/insight-1');
    return { data: client.insights[0] ?? null, error: null, requestId: 'mock' };
  };
  client.getInsightEvidence = async (_id, query) => {
    client.log('GET', '/api/insights/insight-1/evidence', query);
    return {
      data: {
        insight_id: 'insight-1',
        items: lastEvidence,
        total: lastEvidence.length,
        limit: 25,
        offset: 0,
      },
      error: null,
      requestId: 'mock',
    };
  };
  client.getOpportunityScore = async () => {
    client.log('GET', '/api/insights/insight-1/opportunity-score');
    if (!lastScore) {
      return {
        data: null,
        error: { status: 404, statusText: 'NotFound', url: '', method: 'GET', body: { detail: 'not found' }, message: 'not found', requestId: 'mock' },
        requestId: 'mock',
      };
    }
    return { data: lastScore, error: null, requestId: 'mock' };
  };
  client.getDecision = async () => {
    client.log('GET', '/api/insights/insight-1/decision');
    if (!lastDecision) {
      return {
        data: null,
        error: { status: 404, statusText: 'NotFound', url: '', method: 'GET', body: { detail: 'not found' }, message: 'not found', requestId: 'mock' },
        requestId: 'mock',
      };
    }
    return { data: lastDecision, error: null, requestId: 'mock' };
  };
  client.getDecisionHistory = async (_id, query) => {
    client.log('GET', '/api/insights/insight-1/decision/history', query);
    return {
      data: { items: [], total: 0, limit: 50, offset: 0 },
      error: null,
      requestId: 'mock',
    };
  };
  client.patchDecision = async (_id, body) => {
    client.log('PATCH', '/api/insights/insight-1/decision', body);
    if (lastConflict > 0) {
      lastConflict -= 1;
      return {
        data: null,
        error: {
          status: 409,
          statusText: 'Conflict',
          url: '/api/insights/insight-1/decision',
          method: 'PATCH',
          body: { detail: 'version mismatch' },
          message: 'version mismatch',
          requestId: 'mock',
        },
        requestId: 'mock',
      };
    }
    const nextVersion = (lastDecision?.version ?? -1) + 1;
    lastDecision = {
      id: 'decision-1',
      insight_id: 'insight-1',
      project_id: 'proj_a',
      current_score_id: lastScore?.id ?? null,
      business_value:
        body.business_value !== undefined && body.business_value !== null
          ? String(body.business_value)
          : (lastDecision?.business_value ?? null),
      ai_priority: lastScore?.ai_priority ?? null,
      pm_priority: body.pm_priority ?? lastDecision?.pm_priority ?? null,
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
      override_reason: body.override_reason ?? lastDecision?.override_reason ?? null,
      override_note: body.override_note ?? lastDecision?.override_note ?? null,
      version: nextVersion,
    };
    return { data: lastDecision, error: null, requestId: 'mock' };
  };
  client.createRequirement = async (_id) => {
    client.log('POST', '/api/insights/insight-1/requirement');
    return {
      data: {
        id: 'req-new',
        project_id: 'proj_a',
        insight_id: 'insight-1',
        decision_id: 'decision-1',
        score_id: null,
        insight_status_snapshot: 'accepted',
        decision_status_snapshot: 'accepted',
        ai_priority_snapshot: 'P1',
        pm_priority: 'P1',
        pm_override_reason_snapshot: null,
        source_snapshot_hash: 'req-new',
        name: 'AI 草稿',
        problem: '待 PM 完善',
        target_user: '待 PM 完善',
        user_story: '待 PM 完善',
        solution_hypothesis: '待 PM 完善',
        success_metrics: [],
        risks: [],
        evidence_references: [],
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
      },
      error: null,
      requestId: 'mock',
    };
  };

  // The insight detail page fetches once on mount. To exercise multiple
  // decision states in the same harness, we bump a `mountKey` so rerender
  // remounts the page and re-fetches with whatever the mock now returns.
  let mountKey = 0;

  const renderTree = () => (
    <ProjectContextProvider
      key={`pc-${mountKey}`}
      client={client as unknown as never}
      initialProjects={client.projects}
      initialActiveProjectId="proj_a"
    >
      <InsightDetailPage key={`page-${mountKey}`} />
    </ProjectContextProvider>
  );

  const utils = render(renderTree());

  const rerender = (opts: HarnessOpts) => {
    if (opts.insight !== undefined) {
      client.insights = opts.insight === null ? [] : [opts.insight];
    }
    if (opts.evidence !== undefined) {
      lastEvidence = opts.evidence;
    }
    if (opts.score !== undefined) {
      lastScore = opts.score;
    }
    if (opts.scoreMissing !== undefined) {
      lastScore = opts.scoreMissing ? null : (opts.score ?? lastScore);
    }
    if (opts.decision !== undefined) {
      lastDecision = opts.decision;
    }
    if (opts.decisionMissing !== undefined) {
      lastDecision = opts.decisionMissing ? null : (opts.decision ?? lastDecision);
    }
    if (opts.conflictOnPatch !== undefined) {
      lastConflict = opts.conflictOnPatch;
    }
    mountKey += 1;
    utils.rerender(renderTree());
  };

  return { client, rerender };
}

/* ---------- tests ---------- */

describe('InsightDetailPage — Phase 7 B1 surface', () => {
  beforeEach(() => {
    pushMock.mockClear();
  });

  afterEach(() => {
    // Drop the mock from the API singleton so the next test gets a clean
    // slate. Without this, a later test would inherit the previous
    // harness's MockInsightFlowClient instance.
    setApiClient(null);
    vi.useRealTimers();
  });

  it('renders backend opportunity score without recomputing totals', async () => {
    const { client } = renderInsightDetail();
    await waitFor(() => {
      // The snapshot total_score is "6.30" — this is what the backend emitted.
      // The page MUST surface it without any renormalisation of the effective
      // weights or arithmetic on the dimension scores. Any deviation here is a
      // B1 contract violation.
      expect(screen.getByText(/Opportunity Score · Snapshot/i)).toBeTruthy();
    });
    // The configured weight sum is "1.00" → (0.30+0.30+0.10+0.20+0.10) and we
    // expect "1" (int formatter) on the page. We assert on the data-testid so
    // we are robust against small copy changes.
    await waitFor(() => {
      const table = screen.getByTestId('score-table');
      expect(table.textContent).toContain('User Coverage');
      expect(table.textContent).toContain('Severity');
      expect(table.textContent).toContain('Growth');
      expect(table.textContent).toContain('User Value');
      expect(table.textContent).toContain('Business Value');
      // The unavailable growth + business_value rows render "Not Available"
      // exactly as the backend returned `available: false`.
      expect(table.textContent).toContain('Not Available');
    });
    // The page must surface the backend's total_score as-is. The numeric
    // formatter trims trailing zeros so "6.30" renders as "6.3". This is the
    // B1 contract: the snapshot is the source of truth, no client-side
    // arithmetic. Asserting on the literal value is the actual evidence of
    // "no recompute" — if the page computed a different total, this would
    // diverge. ("Exactly one GET" was the previous proxy; we replaced it.)
    // ``6.3`` is unique to the score headline (the decision section renders
    // the priority badge, never the numeric total), so a plain getByText is
    // safe here.
    expect(screen.getByText('6.3')).toBeTruthy();
    // AI priority is a separate backend-emitted field, not derived from the
    // total. It must surface the snapshot's value (P1) verbatim. Both the
    // score snapshot panel and the decision panel render the same "P1"
    // string when the snapshot has it, so we must scope to the score panel
    // to prove the snapshot value is what was actually rendered. The score
    // panel's badge carries the extra ``mono`` class
    // (``<span class="badge badge-accent mono">``) which the decision
    // panel's badge lacks — that class difference is the scope.
    expect(
      screen.getByText('P1', { selector: 'span.badge.badge-accent.mono' }),
    ).toBeTruthy();
    // Calculation version is the backend's audit tag. Render it as a stable
    // contract check — if the page ever derived this client-side, the test
    // would catch it. The version appears as the text content of a single
    // ``meta-v mono`` div in the score snapshot, so an unscoped getByText is
    // unique to that location.
    expect(screen.getByText('opportunity-score-v2')).toBeTruthy();
    // "No recompute" is best proven by "no POST that would create a new
    // snapshot". The createOpportunityScore endpoint is the only mutating
    // call on this resource; if the page never invokes it, the snapshot
    // displayed is whatever the backend GET returned. Reads (GETs) are fine
    // and may happen more than once under React strict mode / re-renders.
    expect(
      client.callLog.filter(
        (c) => c.method === 'POST' && c.path.includes('/opportunity-score'),
      ),
    ).toHaveLength(0);
  });

  it('PATCH /decision body carries request_id, expected_version, and override reason enum', async () => {
    const { client } = renderInsightDetail({
      decision: makeDecision({ version: 2 }),
    });
    await waitFor(() => screen.getByTestId('decision-actions'));

    // Open the override modal:
    await act(async () => {
      (screen.getByText('Override') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('submit-override'));

    await act(async () => {
      (screen.getByTestId('submit-override') as HTMLButtonElement).click();
    });

    await waitFor(() => {
      const patchCall = client.callLog.find(
        (c) => c.method === 'PATCH' && c.path === '/api/insights/insight-1/decision',
      );
      expect(patchCall).toBeTruthy();
      const body = patchCall?.body as DecisionPatchRequest;
      // Three invariants pinned by B1:
      expect(body.request_id).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
      );
      expect(body.expected_version).toBe(2);
      expect(body.action).toBe('override');
      // Override reason must be one of the legal enum values (no free string).
      const legalReasons: DecisionOverrideReason[] = [
        'strategic_priority',
        'core_user_experience',
        'revenue_impact',
        'development_cost',
        'management_decision',
        'data_quality',
        'other',
      ];
      expect(legalReasons).toContain(body.override_reason);
      expect(body.override_note).toBeNull();
    });
  });

  it('on 409 conflict the page refetches decision and shows a conflict alert', async () => {
    const initial = makeDecision({ version: 4 });
    const { client } = renderInsightDetail({
      decision: initial,
      conflictOnPatch: 1,
    });
    await waitFor(() => screen.getByTestId('decision-actions'));

    await act(async () => {
      (screen.getByText('Accept') as HTMLButtonElement).click();
    });

    // After the failed PATCH, the page must:
    //   1. surface the 409 conflict modal
    //   2. refetch the decision (visible as a second GET call to /decision)
    await waitFor(() => {
      expect(screen.getByText(/Decision 版本冲突/)).toBeTruthy();
    });
    await waitFor(() => {
      const gets = client.callLog.filter(
        (c) => c.method === 'GET' && c.path === '/api/insights/insight-1/decision',
      );
      expect(gets.length).toBeGreaterThanOrEqual(2);
    });
    // The version seen by the next PATCH must have been refreshed — the
    // mocked store still has version 4 because the conflict aborted the write.
    await waitFor(() => {
      expect(screen.getByText(/version v4/i)).toBeTruthy();
    });
  });

  it('gates the Create Requirement CTA on evidence + accepted/overridden + pm_priority', async () => {
    // Start with pending_review + no pm_priority → CTA disabled.
    const { rerender } = renderInsightDetail({
      decision: makeDecision({ status: 'pending_review', pm_priority: null }),
    });
    await waitFor(() => screen.getByTestId('cta-bar'));
    const cta = screen.getByTestId('cta-bar');
    expect(cta.getAttribute('data-disabled')).toBe('true');
    expect((screen.getByTestId('create-requirement') as HTMLButtonElement).disabled).toBe(true);

    // Defer the decision → still disabled (status not in {accepted, overridden}).
    rerender({ decision: makeDecision({ status: 'deferred', pm_priority: null }) });
    await waitFor(() => {
      expect(screen.getByTestId('cta-bar').getAttribute('data-disabled')).toBe('true');
    });

    // Reject → still disabled.
    rerender({ decision: makeDecision({ status: 'rejected', pm_priority: null }) });
    await waitFor(() => {
      expect(screen.getByTestId('cta-bar').getAttribute('data-disabled')).toBe('true');
    });

    // Accept + pm_priority set → CTA enabled.
    rerender({ decision: makeDecision({ status: 'accepted', pm_priority: 'P1' }) });
    await waitFor(() => {
      expect(screen.getByTestId('cta-bar').getAttribute('data-disabled')).toBe('false');
      expect((screen.getByTestId('create-requirement') as HTMLButtonElement).disabled).toBe(false);
    });

    // Click and confirm a real POST + route push (B2 will mount the detail).
    await act(async () => {
      (screen.getByTestId('create-requirement') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith('/requirements/req-new');
    });
  });

  it('renders raw feedback content (no AI rewrite) and surfaces feedback_source', async () => {
    renderInsightDetail({ evidence: SAMPLE_EVIDENCE });
    await waitFor(() => screen.getByTestId('evidence-list'));
    const list = screen.getByTestId('evidence-list');
    expect(list.textContent).toContain('注册之后没有提示，我没找到核心功能。');
    expect(list.textContent).toContain('ext-1');
    expect(list.textContent).toContain('interview');
    // AI analysis for that evidence row must be rendered (matched by run).
    expect(screen.getByTestId('evidence-ai')).toBeTruthy();
  });

  it('renders "AI Analysis N/A" when the evidence row has no analysis for the run', async () => {
    const evidenceWithoutAnalysis: InsightEvidenceEntry = {
      ...SAMPLE_EVIDENCE[0]!,
      id: 'ev-2',
      feedback_external_id: 'ext-2',
      feedback_analysis: null,
    };
    renderInsightDetail({ evidence: [evidenceWithoutAnalysis] });
    await waitFor(() => screen.getByTestId('evidence-ai-empty'));
    expect(screen.getByTestId('evidence-ai-empty').textContent).toContain('未生成');
  });
});

/* ---------- Phase 7 B1 fixes (project isolation + strict UUID) ---------- */

describe('InsightDetailPage — Phase 7 B1 fixes', () => {
  beforeEach(() => {
    pushMock.mockClear();
  });

  afterEach(() => {
    setApiClient(null);
    vi.useRealTimers();
  });

  it('refuses to render an insight from a different project (B1 project isolation)', async () => {
    // SAMPLE_INSIGHT carries project_id 'proj_a', but the active project is
    // 'proj_b', so the page must surface a cross-project refusal and never
    // render the foreign project's title or decision actions.
    const client = new MockInsightFlowClient();
    setApiClient(client as unknown as InsightFlowClient);
    client.projects = [
      { id: 'proj_a', name: 'A', product_name: 'A' },
      { id: 'proj_b', name: 'B', product_name: 'B' },
    ];
    client.insights = [SAMPLE_INSIGHT];

    render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_b"
      >
        <InsightDetailPage />
      </ProjectContextProvider>,
    );

    // The page should expose an explicit cross-project refusal rather than
    // rendering the title or any data from the wrong project.
    await waitFor(() => {
      expect(screen.getByText(/该洞察不属于当前项目/)).toBeTruthy();
    });
    // The foreign-project title MUST NOT appear. Its absence is the actual
    // contract assertion: the B1 bug was that the page happily rendered
    // foreign data — here the title is checked and must be missing.
    expect(screen.queryByText('Trial 用户首日激活率异常低')).toBeNull();
    // Decision action buttons must not be reachable in this state. The CTA
    // is also gated off, since the page refuses to surface anything.
    expect(screen.queryByTestId('decision-actions')).toBeNull();
    expect(screen.queryByTestId('create-requirement')).toBeNull();
  });

  it('refuses PATCH /decision when crypto.randomUUID is unavailable', async () => {
    // Phase 7 B1 fix: the backend rejects any non-UUID request_id with 422.
    // The previous client emitted ``req_<timestamp>_<rand>`` when
    // ``crypto.randomUUID`` was missing; that path is now a hard refusal.
    //
    // Implementation note: a plain ``delete globalThis.crypto.randomUUID``
    // does NOT reliably remove the method — ``Crypto.prototype`` (or a
    // configured own property descriptor) can keep randomUUID reachable,
    // so ``typeof crypto.randomUUID`` still returns ``'function'`` and the
    // page happily mints a UUID. We rewrite the own-property descriptor
    // to set the value to ``undefined`` instead, which makes the typeof
    // check return ``'undefined'`` and triggers the page's refusal path.
    const originalDescriptor = Object.getOwnPropertyDescriptor(
      globalThis.crypto,
      'randomUUID',
    );
    Object.defineProperty(globalThis.crypto, 'randomUUID', {
      configurable: true,
      writable: true,
      enumerable: originalDescriptor?.enumerable ?? true,
      value: undefined,
    });

    try {
      // Sanity check: the typeof probe inside mintRequestId must
      // observe ``undefined`` after the rewrite.
      expect(typeof (globalThis.crypto as Crypto).randomUUID).toBe('undefined');

      const { client } = renderInsightDetail({
        decision: makeDecision({ version: 1 }),
      });
      await waitFor(() => screen.getByTestId('decision-actions'));

      await act(async () => {
        (screen.getByText('Accept') as HTMLButtonElement).click();
      });

      // The page MUST NOT send a PATCH — error path is local.
      await waitFor(() => {
        const patches = client.callLog.filter(
          (c) => c.method === 'PATCH' && c.path.includes('/decision'),
        );
        expect(patches).toHaveLength(0);
      });
      // And it MUST show a human-readable refusal explaining the problem.
      await waitFor(() => {
        expect(screen.getByText(/无法生成请求 ID|crypto\.randomUUID/)).toBeTruthy();
      });
    } finally {
      // Restore the original descriptor exactly. If there was no own
      // descriptor originally, we delete the own property we just
      // created so the global ``crypto`` matches its pre-test shape.
      // We use ``Reflect.deleteProperty`` rather than the ``delete``
      // operator because TypeScript's strict-mode ``delete`` requires
      // the operand to be optional; ``Crypto.randomUUID`` is declared
      // required in the lib types, so ``delete crypto.randomUUID``
      // fails compilation even with a runtime cast.
      if (originalDescriptor) {
        Object.defineProperty(globalThis.crypto, 'randomUUID', originalDescriptor);
      } else {
        Reflect.deleteProperty(globalThis.crypto, 'randomUUID');
      }
    }
  });
});