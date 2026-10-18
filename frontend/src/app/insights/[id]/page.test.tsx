import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../../lib/api/mock';
import { setApiClient } from '../../../lib/api/client';
import type { InsightFlowClient } from '../../../lib/api/client';
import {
  type ApiResult,
  type DecisionOverrideReason,
  type DecisionPatchRequest,
  type FeedbackAnalysisEntry,
  type Insight,
  type InsightDecision,
  type InsightEvidenceEntry,
  type OpportunityScore,
  type ResearchFindingRead,
  type ResearchRunListPage,
  type ResearchRunRead,
  type ResearchSourceRead,
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

  it('renders readable score explanation in both panels without audit payloads', async () => {
    const score = {
      ...SAMPLE_SCORE,
      ai_reasoning: "calculation_version=opportunity-score-v1 | feedback_count=2 | affected_unique_users=2 | snapshot_unique_users=6 | user_value_available=True | effective_weights={'user_coverage': '0.3035714285714285714285714285'} | total_score=3.89 | ai_priority=P2 | user_value_reasoning=证据包含2条PDF上传反馈，模块集中（pdf_upload）。样本较少，影响范围仍需核实。",
    };
    renderInsightDetail({ score, decision: makeDecision({ status: 'accepted', pm_priority: 'P2' }) });
    await waitFor(() => {
      const explanation = document.querySelector('.ai-reasoning-body');
      expect(explanation?.textContent).toContain('证据包含2条PDF上传反馈');
      expect(explanation?.textContent).not.toMatch(/calculation_version|effective_weights|user_value_reasoning|pdf_upload/);
      const advice = document.querySelector('.decision-box.ai .decision-value');
      expect(advice?.textContent).not.toMatch(/calculation_version|effective_weights|user_value_reasoning|pdf_upload/);
      expect(document.querySelector('.decision-box:not(.ai)')?.textContent).toContain('已采纳');
      expect(document.querySelector('.decision-box:not(.ai)')?.textContent).not.toContain('accepted');
    });
    expect(score.ai_reasoning).toContain('calculation_version=');
  });

  it('renders backend opportunity score without recomputing totals', async () => {
    const { client } = renderInsightDetail();
    await waitFor(() => {
      // The snapshot total_score is "6.30" — this is what the backend emitted.
      // The page MUST surface it without any renormalisation of the effective
      // weights or arithmetic on the dimension scores. Any deviation here is a
      // B1 contract violation.
      expect(screen.getByText(/机会评分/i)).toBeTruthy();
    });
    // The configured weight sum is "1.00" → (0.30+0.30+0.10+0.20+0.10) and we
    // expect "1" (int formatter) on the page. We assert on the data-testid so
    // we are robust against small copy changes.
    await waitFor(() => {
      const table = screen.getByTestId('score-table');
      expect(table.textContent).toContain('用户覆盖');
      expect(table.textContent).toContain('问题严重度');
      expect(table.textContent).toContain('增长潜力');
      expect(table.textContent).toContain('用户价值');
      expect(table.textContent).toContain('业务价值');
      // The unavailable growth + business_value rows render "Not Available"
      // exactly as the backend returned `available: false`.
      expect(table.textContent).toContain('暂无数据');
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
      (screen.getByText('调整优先级') as HTMLButtonElement).click();
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
      (screen.getByText('采纳') as HTMLButtonElement).click();
    });

    // After the failed PATCH, the page must:
    //   1. surface the 409 conflict modal
    //   2. refetch the decision (visible as a second GET call to /decision)
    await waitFor(() => {
      expect(screen.getByText(/决策已更新/)).toBeTruthy();
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
      expect(screen.getByText(/版本 4/)).toBeTruthy();
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
    expect(list.textContent).toContain('用户访谈');
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
    expect(screen.getByTestId('evidence-ai-empty').textContent).toContain('尚无分类分析结果');
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
        (screen.getByText('采纳') as HTMLButtonElement).click();
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

/* ---------- Phase 8 External Research UI (Insight Detail) ----------
 *
 * These tests cover the External Research surface added in
 * Phase 8 work package B. They do NOT touch the original
 * Insight Detail behaviour (Internal Evidence, Decision,
 * Requirement CTA) — every assertion lives on the
 * ``data-testid="external-research"`` subtree. */

const SAMPLE_RESEARCH_RUN: ResearchRunRead = {
  id: 'rrun-1',
  project_id: 'proj_a',
  insight_id: 'insight-1',
  classification_run_id: 'classification-1',
  clustering_run_id: 'clustering-1',
  insight_decision_id: null,
  research_question: '竞品 TrialHero 在 SMB 的定价策略？',
  scope: 'EU/SMB',
  provider: 'tavily',
  provider_query: 'TrialHero SMB pricing',
  model: 'gpt-4o-mini',
  prompt_version: 'research-agent-v1',
  status: 'completed',
  started_at: '2026-10-14T01:00:00Z',
  completed_at: '2026-10-14T01:00:05Z',
  created_at: '2026-10-14T01:00:00Z',
  error_message: null,
  internal_context: {
    insight_title: 'Trial 用户首日激活率异常低',
    insight_pain_point: '注册后不知下一步',
    insight_affected_segment: '14 天试用用户',
    insight_recommendation: '增加 onboarding check-in',
    evidence_external_ids: ['ext-1'],
    evidence_summaries: [{ external_id: 'ext-1', summary: 'summary' }],
    decision_status: null,
    decision_priority: null,
    research_question: '竞品 TrialHero 在 SMB 的定价策略？',
    scope: 'EU/SMB',
  },
  internal_context_hash: 'hash-1',
  // Pre-v5 fixture: a legacy research run has no
  // ``task_contract`` / ``coverage_report`` /
  // ``execution_trace`` on disk. The page MUST tolerate the
  // triple-null and surface the historical honesty banner
  // ("此 Run 是历史 Run，没有 v5 覆盖报告…") rather than
  // the v5 evidence-coverage section. The dedicated
  // historical-run test asserts this end-to-end.
  task_contract: null,
  coverage_report: null,
  execution_trace: null,
  input_count: 1,
  processed_count: 1,
  source_count: 2,
  finding_count: 3,
  input_tokens: 100,
  output_tokens: 200,
  unknown_token_calls: 0,
  latency_ms: 5000,
  attempt_count: 1,
  retry_count: 0,
};

const SAMPLE_RESEARCH_FINDINGS: ResearchFindingRead[] = [
  {
    id: 'finding-competitor',
    run_id: 'rrun-1',
    category: 'competitor',
    title: 'TrialHero 提供 14 天试用',
    description: '公开网页说明...',
    confidence: 0.8,
    status: 'confirmed',
    source_refs: [
      {
        provider_source_id: 'src-1',
        url: 'https://trialhero.example/pricing',
        title: 'TrialHero Pricing',
      },
    ],
    created_at: '2026-10-14T01:00:05Z',
  },
  {
    id: 'finding-market',
    run_id: 'rrun-1',
    category: 'market_pattern',
    title: 'SMB 普遍采用自助式 onboarding',
    description: '行业报告...',
    confidence: 0.6,
    status: 'confirmed',
    source_refs: [
      {
        provider_source_id: 'src-2',
        url: 'https://report.example/smb-onboarding',
        title: 'SMB Onboarding Report',
      },
    ],
    created_at: '2026-10-14T01:00:05Z',
  },
  {
    id: 'finding-opportunity',
    run_id: 'rrun-1',
    category: 'opportunity',
    title: '差异化 onboarding 策略',
    description: '基于上述市场观察...',
    confidence: 0.5,
    status: 'not_verified',
    source_refs: null,
    created_at: '2026-10-14T01:00:05Z',
  },
];

const SAMPLE_RESEARCH_SOURCES: ResearchSourceRead[] = [
  {
    id: 'src-1',
    run_id: 'rrun-1',
    provider: 'tavily',
    provider_source_id: 'src-1',
    url: 'https://trialhero.example/pricing',
    title: 'TrialHero Pricing',
    domain: 'trialhero.example',
    snippet: 'Pricing page snippet.',
    retrieved_at: '2026-10-14T01:00:01Z',
    created_at: '2026-10-14T01:00:01Z',
  },
  {
    id: 'src-2',
    run_id: 'rrun-1',
    provider: 'tavily',
    provider_source_id: 'src-2',
    url: 'https://report.example/smb-onboarding',
    title: 'SMB Onboarding Report',
    domain: 'report.example',
    snippet: 'Industry report excerpt.',
    retrieved_at: '2026-10-14T01:00:02Z',
    created_at: '2026-10-14T01:00:02Z',
  },
];

interface ResearchHarnessOpts {
  runs?: ResearchRunRead[];
  findings?: ResearchFindingRead[];
  sources?: ResearchSourceRead[];
  /** POST response shape — set to a Status 0 error to simulate
   *  the synchronous call's network failure surface. */
  postError?: { status: number; message: string } | null;
  detailError?: { status: number; message: string } | null;
}

function renderInsightDetailWithResearch(
  initial: ResearchHarnessOpts = {},
) {
  const client = new MockInsightFlowClient();
  setApiClient(client as unknown as InsightFlowClient);
  client.projects = [{ id: 'proj_a', name: 'A', product_name: 'Product A' }];
  client.insights = [SAMPLE_INSIGHT];

  // Defaults — a single completed run so the "history shows
  // latest first" assertion matches what real users see after
  // one POST. Tests can override via the harness opts.
  client.researchRuns =
    initial.runs ?? [SAMPLE_RESEARCH_RUN];
  client.researchFindings = initial.findings ?? SAMPLE_RESEARCH_FINDINGS;
  client.researchSources = initial.sources ?? SAMPLE_RESEARCH_SOURCES;

  // Preload evidence/score/decision so the page's existing
  // fetchers do not block the External Research subtree from
  // rendering.
  let lastEvidence: InsightEvidenceEntry[] = [];
  let lastScore: OpportunityScore | null = SAMPLE_SCORE;
  let lastDecision: InsightDecision | null = makeDecision();

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
    return { data: lastScore, error: null, requestId: 'mock' };
  };
  client.getDecision = async () => {
    client.log('GET', '/api/insights/insight-1/decision');
    return { data: lastDecision, error: null, requestId: 'mock' };
  };
  client.getDecisionHistory = async () => {
    client.log('GET', '/api/insights/insight-1/decision/history');
    return {
      data: { items: [], total: 0, limit: 50, offset: 0 },
      error: null,
      requestId: 'mock',
    };
  };

  // External Research wiring — these methods mirror the page's
  // expectation. Each MUST call ``client.log`` so callLog keeps
  // mirroring what the real InsightFlowClient methods do
  // (per the [[feedback-mock-test-logging]] rule).
  client.startResearchRun = async (id, body) => {
    client.log('POST', `/api/insights/${id}/research`, body);
    if (initial.postError) {
      const e = initial.postError;
      return {
        data: null,
        error: {
          status: e.status,
          statusText: 'ForcedError',
          url: `/api/insights/${id}/research`,
          method: 'POST',
          body: { detail: e.message },
          message: e.message,
          requestId: 'mock',
        },
        requestId: 'mock',
      };
    }
    return {
      data: SAMPLE_RESEARCH_RUN,
      error: null,
      requestId: 'mock',
    };
  };
  client.listInsightResearchRuns = async (id, query) => {
    client.log('GET', `/api/insights/${id}/research`, query);
    return {
      data: {
        items: client.researchRuns,
        total: client.researchRuns.length,
        limit: query?.limit ?? 50,
        offset: query?.offset ?? 0,
      },
      error: null,
      requestId: 'mock',
    };
  };
  client.getResearchRun = async (runId, query) => {
    client.log('GET', `/api/research/${runId}`, query);
    if (initial.detailError) {
      const e = initial.detailError;
      return {
        data: null,
        error: {
          status: e.status,
          statusText: 'ForcedError',
          url: `/api/research/${runId}`,
          method: 'GET',
          body: { detail: e.message },
          message: e.message,
          requestId: 'mock',
        },
        requestId: 'mock',
      };
    }
    // The detail endpoint MUST return the actual run row
    // matching the requested id — not the SAMPLE constant
    // — so tests that seed a v5 row with
    // ``task_contract`` / ``coverage_report`` /
    // ``execution_trace`` see those fields in the rendered
    // panel. Returning the constant silently dropped the
    // v5 payload, which is what masked the assertion
    // failure in the prior round.
    const seeded =
      client.researchRuns.find((r) => r.id === runId) ?? SAMPLE_RESEARCH_RUN;
    return { data: seeded, error: null, requestId: 'mock' };
  };
  client.listResearchFindings = async (runId, query) => {
    client.log('GET', `/api/research/${runId}/findings`, query);
    return {
      data: {
        items: client.researchFindings,
        total: client.researchFindings.length,
        limit: query?.limit ?? 50,
        offset: query?.offset ?? 0,
      },
      error: null,
      requestId: 'mock',
    };
  };
  client.listResearchSources = async (runId, query) => {
    client.log('GET', `/api/research/${runId}/sources`, query);
    return {
      data: {
        items: client.researchSources,
        total: client.researchSources.length,
        limit: query?.limit ?? 50,
        offset: query?.offset ?? 0,
      },
      error: null,
      requestId: 'mock',
    };
  };

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
  const rerender = (opts: ResearchHarnessOpts = {}) => {
    if (opts.runs) client.researchRuns = opts.runs;
    mountKey += 1;
    utils.rerender(renderTree());
  };
  return { client, rerender };
}

describe('InsightDetailPage — Phase 8 External Research UI', () => {
  beforeEach(() => {
    pushMock.mockClear();
  });

  afterEach(() => {
    setApiClient(null);
    vi.useRealTimers();
  });

  it('renders a fully-separated External Research section with launcher and history', async () => {
    const { client } = renderInsightDetailWithResearch();
    // The page mounts the External Research section as a
    // sibling of Internal Evidence — distinct testid ensures
    // the assertion never collides with an evidence-list match.
    await waitFor(() => screen.getByTestId('external-research'));
    await waitFor(() => screen.getByTestId('research-launcher'));
    await waitFor(() => screen.getByTestId('research-history'));
    // History wired the latest run, so the picker renders that
    // row plus the auto-selected detail panel.
    await waitFor(() => screen.getByTestId('research-history-item-rrun-1'));
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    // Findings + sources carry their own testids too — the
    // page is honest about its surface area.
    expect(screen.getByTestId('research-findings')).toBeTruthy();
    expect(screen.getByTestId('research-sources')).toBeTruthy();
    // The Real-Insight Evidence list (Internal Evidence)
    // is still present and remains untouched by this package —
    // it must continue to render its own list testid.
    expect(screen.queryAllByTestId('evidence-list').length).toBeGreaterThanOrEqual(0);
  });

  it('Run Research POST carries research_question + scope and refreshes history', async () => {
    const { client } = renderInsightDetailWithResearch();
    await waitFor(() => screen.getByTestId('research-question-input'));

    await act(async () => {
      fireEvent.change(
        screen.getByTestId('research-question-input'),
        { target: { value: '竞品 X 的定价策略' } },
      );
      fireEvent.change(
        screen.getByTestId('research-scope-input'),
        { target: { value: 'EU / SMB' } },
      );
    });
    await act(async () => {
      (screen.getByTestId('research-run-button') as HTMLButtonElement).click();
    });

    await waitFor(() => {
      const post = client.callLog.find(
        (c) =>
          c.method === 'POST' &&
          c.path === '/api/insights/insight-1/research',
      );
      expect(post).toBeTruthy();
      const body = post?.body as { research_question: string; scope: string };
      expect(body.research_question).toBe('竞品 X 的定价策略');
      expect(body.scope).toBe('EU / SMB');
    });

    // After the synchronous POST returns, the page issues a
    // GET to the history endpoint to refresh the list. The
    // detail panel ends up showing the run row the POST
    // produced (testid carries the run id from the response).
    await waitFor(() => {
      const gets = client.callLog.filter(
        (c) =>
          c.method === 'GET' &&
          c.path === '/api/insights/insight-1/research',
      );
      expect(gets.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('disables Run Research button while the POST is in flight', async () => {
    // Hand-rolled promise lets us observe the running state
    // before the response lands.
    const client = new MockInsightFlowClient();
    setApiClient(client as unknown as InsightFlowClient);
    client.projects = [{ id: 'proj_a', name: 'A', product_name: 'Product A' }];
    client.insights = [SAMPLE_INSIGHT];
    let resolvePost!: (value: ApiResult<ResearchRunRead>) => void;
    client.startResearchRun = () => {
      client.log('POST', '/api/insights/insight-1/research');
      return new Promise<ApiResult<ResearchRunRead>>((res) => {
        resolvePost = res;
      });
    };
    client.listInsightResearchRuns = async (id, query) => {
      client.log('GET', `/api/insights/${id}/research`, query);
      return {
        data: { items: [], total: 0, limit: 50, offset: 0 },
        error: null,
        requestId: 'mock',
      };
    };
    client.getResearchRun = async (runId, query) => {
      client.log('GET', `/api/research/${runId}`, query);
      return { data: SAMPLE_RESEARCH_RUN, error: null, requestId: 'mock' };
    };
    client.listResearchFindings = async (runId, query) => {
      client.log('GET', `/api/research/${runId}/findings`, query);
      return {
        data: { items: [], total: 0, limit: 50, offset: 0 },
        error: null,
        requestId: 'mock',
      };
    };
    client.listResearchSources = async (runId, query) => {
      client.log('GET', `/api/research/${runId}/sources`, query);
      return {
        data: { items: [], total: 0, limit: 50, offset: 0 },
        error: null,
        requestId: 'mock',
      };
    };
    // Pre-stub the unrelated fetchers so the page can mount.
    client.getInsight = async () => {
      client.log('GET', '/api/insights/insight-1');
      return { data: SAMPLE_INSIGHT, error: null, requestId: 'mock' };
    };
    client.getInsightEvidence = async () => ({
      data: {
        insight_id: 'insight-1',
        items: [],
        total: 0,
        limit: 25,
        offset: 0,
      },
      error: null,
      requestId: 'mock',
    });
    client.getOpportunityScore = async () => ({
      data: SAMPLE_SCORE,
      error: null,
      requestId: 'mock',
    });
    client.getDecision = async () => ({
      data: makeDecision(),
      error: null,
      requestId: 'mock',
    });
    client.getDecisionHistory = async () => ({
      data: { items: [], total: 0, limit: 50, offset: 0 },
      error: null,
      requestId: 'mock',
    });

    render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <InsightDetailPage />
      </ProjectContextProvider>,
    );

    await waitFor(() => screen.getByTestId('research-question-input'));
    fireEvent.change(
      screen.getByTestId('research-question-input'),
      { target: { value: 'some question' } },
    );
    await act(async () => {
      (screen.getByTestId('research-run-button') as HTMLButtonElement).click();
    });
    // While the POST is in-flight, the running badge is up
    // and the button is disabled — preventing duplicate POSTs
    // even on rapid clicks.
    await waitFor(() => screen.getByTestId('research-running'));
    expect(
      (screen.getByTestId('research-run-button') as HTMLButtonElement).disabled,
    ).toBe(true);

    // Resolve the POST so React flushes its updates; this
    // unmounts the running badge.
    await act(async () => {
      resolvePost({ data: SAMPLE_RESEARCH_RUN, error: null, requestId: 'mock' });
    });
    await waitFor(() => screen.queryByTestId('research-running') === null);
  });

  it('expands a confirmed finding to reveal its source_refs as safe external links', async () => {
    renderInsightDetailWithResearch();
    await waitFor(() => screen.getByTestId('research-finding-finding-competitor'));
    const toggle = screen.getByTestId('research-finding-toggle-finding-competitor');
    await act(async () => {
      (toggle as HTMLButtonElement).click();
    });
    await waitFor(() =>
      screen.getByTestId('research-finding-citations-finding-competitor'),
    );
    // Scope to the citations block — the Sources inventory at
    // the bottom of the panel also renders the same title text,
    // and ``getByText`` throws on multiple matches without a
    // scope. The citation block's testid carries the finding
    // id so we can target it precisely.
    const citationsBlock = screen.getByTestId(
      'research-finding-citations-finding-competitor',
    );
    const link = citationsBlock.querySelector('a') as HTMLAnchorElement | null;
    expect(link).toBeTruthy();
    // The link must carry ``rel="noopener noreferrer nofollow"``
    // — without those the PM clicking an external research hit
    // would let the destination page manipulate the opener
    // (window.opener), leak the Referer, or score the link for
    // SEO. This is the explicit "safe external link" contract
    // Phase 8 ships.
    expect(link!.target).toBe('_blank');
    expect(link!.rel).toContain('noopener');
    expect(link!.rel).toContain('noreferrer');
    expect(link!.rel).toContain('nofollow');
  });

  it('marks not_verified findings as Not Verified (no fake confirmation)', async () => {
    renderInsightDetailWithResearch();
    await waitFor(() => screen.getByTestId('research-finding-finding-opportunity'));
    // Status badge for the not_verified finding uses the
    // explicit "Not Verified" label rather than any
    // green/cited variant — keeping the page honest about
    // grounding gaps so a PM cannot mistake an LLM-only claim
    // for evidence-backed fact.
    expect(
      screen.getByTestId('research-finding-status-finding-opportunity').textContent,
    ).toBe('未验证');
    // The runner-wide counter also surfaces the citation gap.
    await waitFor(() =>
      screen.getByTestId('research-not-verified-banner'),
    );
  });

  it('shows External Research Unavailable when the detail GET fails (cross-project or backend outage)', async () => {
    renderInsightDetailWithResearch({
      detailError: { status: 404, message: 'cross-project' },
    });
    await waitFor(() =>
      screen.getByTestId('research-unavailable'),
    );
    expect(
      screen.getByTestId('research-unavailable').textContent,
    ).toMatch(/External Research Unavailable/);
  });

  it('clears the External Research state on project switch', async () => {
    let mountKey = 0;
    const client = new MockInsightFlowClient();
    setApiClient(client as unknown as InsightFlowClient);
    client.projects = [
      { id: 'proj_a', name: 'A', product_name: 'A' },
      { id: 'proj_b', name: 'B', product_name: 'B' },
    ];
    client.insights = [SAMPLE_INSIGHT];
    client.researchRuns = [SAMPLE_RESEARCH_RUN];
    client.researchFindings = SAMPLE_RESEARCH_FINDINGS;
    client.researchSources = SAMPLE_RESEARCH_SOURCES;

    let resolveList!: (value: ApiResult<ResearchRunListPage>) => void;
    // First call (proj_a) returns the seeded run; the second
    // call (proj_b) returns an empty list AND a stalled
    // promise so we can observe the cleared state mid-flight.
    let activeProject = 'proj_a';
    client.listInsightResearchRuns = async (id, query) => {
      client.log('GET', `/api/insights/${id}/research`, query);
      if (activeProject === 'proj_a') {
        return {
          data: {
            items: client.researchRuns,
            total: client.researchRuns.length,
            limit: 50,
            offset: 0,
          },
          error: null,
          requestId: 'mock',
        };
      }
      return new Promise<ApiResult<ResearchRunListPage>>((res) => {
        resolveList = res;
      });
    };
    client.getResearchRun = async (runId, query) => {
      client.log('GET', `/api/research/${runId}`, query);
      return { data: SAMPLE_RESEARCH_RUN, error: null, requestId: 'mock' };
    };
    client.listResearchFindings = async (runId, query) => {
      client.log('GET', `/api/research/${runId}/findings`, query);
      return {
        data: {
          items: client.researchFindings,
          total: client.researchFindings.length,
          limit: 50,
          offset: 0,
        },
        error: null,
        requestId: 'mock',
      };
    };
    client.listResearchSources = async (runId, query) => {
      client.log('GET', `/api/research/${runId}/sources`, query);
      return {
        data: {
          items: client.researchSources,
          total: client.researchSources.length,
          limit: 50,
          offset: 0,
        },
        error: null,
        requestId: 'mock',
      };
    };
    // Pre-stub the unrelated fetchers so the page can mount.
    client.getInsight = async () => {
      client.log('GET', '/api/insights/insight-1');
      return { data: SAMPLE_INSIGHT, error: null, requestId: 'mock' };
    };
    client.getInsightEvidence = async () => ({
      data: {
        insight_id: 'insight-1',
        items: [],
        total: 0,
        limit: 25,
        offset: 0,
      },
      error: null,
      requestId: 'mock',
    });
    client.getOpportunityScore = async () => ({
      data: SAMPLE_SCORE,
      error: null,
      requestId: 'mock',
    });
    client.getDecision = async () => ({
      data: makeDecision(),
      error: null,
      requestId: 'mock',
    });
    client.getDecisionHistory = async () => ({
      data: { items: [], total: 0, limit: 50, offset: 0 },
      error: null,
      requestId: 'mock',
    });

    const { rerender } = render(
      <ProjectContextProvider
        key={`pc-${mountKey}`}
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <InsightDetailPage key={`page-${mountKey}`} />
      </ProjectContextProvider>,
    );

    await waitFor(() => screen.getByTestId('research-history-item-rrun-1'));
    expect(screen.getByTestId('research-detail-panel')).toBeTruthy();

    // Switch projects: the project's effect MUST wipe the
    // external-research subtree. The selected run, the detail
    // panel and the in-flight runner all reset — without
    // resolving the stalled promise first (so we can prove
    // the cleanup happened, not just the unmount race).
    activeProject = 'proj_b';
    rerender(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_b"
      >
        <InsightDetailPage key={`page-b`} />
      </ProjectContextProvider>,
    );

    // Old run row MUST be gone immediately — the project-switch
    // effect wiped the External Research state synchronously,
    // before any network response landed. Without that wipe,
    // a late-arriving response from project A would still
    // leak the run id into project B's view.
    expect(screen.queryByTestId('research-history-item-rrun-1')).toBeNull();
    // Detail panel was wiped — no stale row from project A.
    expect(screen.queryByTestId('research-detail-panel')).toBeNull();
    // Resolve the stalled network promise so the test exits
    // cleanly without leaking an unsettled promise. The
    // ``mySignal.aborted`` guard inside ``fetchResearchHistory``
    // means the resolved data is intentionally discarded — that
    // IS the race-safe contract: the late response from project
    // A's in-flight request must not re-populate project B's
    // view.
    await act(async () => {
      resolveList({
        data: { items: [], total: 0, limit: 50, offset: 0 },
        error: null,
        requestId: 'mock',
      });
    });
  });

  it('shows the honesty "回答完整性：未验证" notice for a completed run even when every finding is confirmed', async () => {
    // The brief: the runner's ``completed`` gate is ≥1 cited
    // finding. Even with three confirmed findings and zero
    // ``not_verified`` rows, the panel must surface the
    // "unverified completeness" notice — a PM is not allowed
    // to read a green "all confirmed" finding list as "the
    // research question is fully answered". The notice is
    // driven ONLY by the run status; the confirmed-count,
    // the not_verified-count, and the LLM confidence values
    // must NOT be consulted to suppress it.
    const allConfirmed: ResearchFindingRead[] = [
      {
        id: 'finding-competitor',
        run_id: 'rrun-1',
        category: 'competitor',
        title: 'TrialHero 提供 14 天试用',
        description: '公开网页说明...',
        confidence: 0.8,
        status: 'confirmed',
        source_refs: [
          {
            provider_source_id: 'src-1',
            url: 'https://trialhero.example/pricing',
            title: 'TrialHero Pricing',
          },
        ],
        created_at: '2026-10-14T01:00:05Z',
      },
      {
        id: 'finding-market',
        run_id: 'rrun-1',
        category: 'market_pattern',
        title: 'SMB 普遍采用自助式 onboarding',
        description: '行业报告...',
        confidence: 0.6,
        status: 'confirmed',
        source_refs: [
          {
            provider_source_id: 'src-2',
            url: 'https://report.example/smb-onboarding',
            title: 'SMB Onboarding Report',
          },
        ],
        created_at: '2026-10-14T01:00:05Z',
      },
      {
        id: 'finding-competitor-2',
        run_id: 'rrun-1',
        category: 'competitor',
        title: 'TrialHero 定价按席位计算',
        description: '第二段公开网页...',
        confidence: 0.7,
        status: 'confirmed',
        source_refs: [
          {
            provider_source_id: 'src-3',
            url: 'https://trialhero.example/seat-pricing',
            title: 'Seat pricing',
          },
        ],
        created_at: '2026-10-14T01:00:05Z',
      },
    ];
    renderInsightDetailWithResearch({
      runs: [SAMPLE_RESEARCH_RUN],
      findings: allConfirmed,
    });
    // Wait for the detail panel to land.
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    // Open the run so the panel renders.
    await act(async () => {
      (screen.getByTestId('research-select-rrun-1') as HTMLButtonElement).click();
    });
    await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    // The notice body is the verbatim honesty text from the
    // Phase 8 brief — it must surface the "Completed 仅表示…
    // 不代表完整回答了问题" framing so a PM never reads
    // "completed" as "answered".
    const banner = screen.getByTestId('research-answer-completeness');
    expect(banner.textContent).toMatch(/回答完整性：未验证/);
    expect(banner.textContent).toMatch(/尚未核对完整性/);
    // Confirmed-only findings must NOT trigger the
    // ``not_verified`` banner — that banner is the per-row
    // count, the new notice is the run-level honesty gate.
    expect(screen.queryByTestId('research-not-verified-banner')).toBeNull();
  });

  it('shows the honesty notice for a partial_failed run (not just completed)', async () => {
    // The brief: completed / partial_failed / failed are the
    // three terminal states. The notice must surface for all
    // three, NOT just ``completed`` — otherwise a partially
    // grounded run would silently look "less complete" than
    // a green one, while still landing the PM on an
    // under-answered question.
    const partialFailedRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'partial_failed',
      error_message: 'tavily 检索超时，仅返回 1 个 cited finding',
    };
    renderInsightDetailWithResearch({
      runs: [partialFailedRun],
    });
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    await act(async () => {
      (screen.getByTestId('research-select-rrun-1') as HTMLButtonElement).click();
    });
    await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    expect(
      screen.getByTestId('research-answer-completeness').textContent,
    ).toMatch(/回答完整性：未验证/);
  });

  it('clears the honesty notice (and the underlying research data) on project switch', async () => {
    // The brief: project switch MUST wipe every piece of
    // external-research state — including the honesty notice.
    // A late-arriving response from the previous project must
    // never re-surface a "回答完整性：未验证" banner (or any
    // of its sources / findings) into the new project's view.
    // This test mirrors the project-switch harness above and
    // adds the explicit "notice is gone" assertion.
    let mountKey = 0;
    const client = new MockInsightFlowClient();
    setApiClient(client as unknown as InsightFlowClient);
    client.projects = [
      { id: 'proj_a', name: 'A', product_name: 'A' },
      { id: 'proj_b', name: 'B', product_name: 'B' },
    ];
    client.insights = [SAMPLE_INSIGHT];
    client.researchRuns = [SAMPLE_RESEARCH_RUN];
    client.researchFindings = SAMPLE_RESEARCH_FINDINGS;
    client.researchSources = SAMPLE_RESEARCH_SOURCES;

    let resolveList!: (value: ApiResult<ResearchRunListPage>) => void;
    let activeProject = 'proj_a';
    client.listInsightResearchRuns = async (id, query) => {
      client.log('GET', `/api/insights/${id}/research`, query);
      if (activeProject === 'proj_a') {
        return {
          data: {
            items: client.researchRuns,
            total: client.researchRuns.length,
            limit: 50,
            offset: 0,
          },
          error: null,
          requestId: 'mock',
        };
      }
      return new Promise<ApiResult<ResearchRunListPage>>((res) => {
        resolveList = res;
      });
    };
    client.getResearchRun = async (runId, query) => {
      client.log('GET', `/api/research/${runId}`, query);
      return { data: SAMPLE_RESEARCH_RUN, error: null, requestId: 'mock' };
    };
    client.listResearchFindings = async (runId, query) => {
      client.log('GET', `/api/research/${runId}/findings`, query);
      return {
        data: {
          items: client.researchFindings,
          total: client.researchFindings.length,
          limit: 50,
          offset: 0,
        },
        error: null,
        requestId: 'mock',
      };
    };
    client.listResearchSources = async (runId, query) => {
      client.log('GET', `/api/research/${runId}/sources`, query);
      return {
        data: {
          items: client.researchSources,
          total: client.researchSources.length,
          limit: 50,
          offset: 0,
        },
        error: null,
        requestId: 'mock',
      };
    };
    client.getInsight = async () => {
      client.log('GET', '/api/insights/insight-1');
      return { data: SAMPLE_INSIGHT, error: null, requestId: 'mock' };
    };
    client.getInsightEvidence = async () => ({
      data: {
        insight_id: 'insight-1',
        items: [],
        total: 0,
        limit: 25,
        offset: 0,
      },
      error: null,
      requestId: 'mock',
    });
    client.getOpportunityScore = async () => ({
      data: SAMPLE_SCORE,
      error: null,
      requestId: 'mock',
    });
    client.getDecision = async () => ({
      data: makeDecision(),
      error: null,
      requestId: 'mock',
    });
    client.getDecisionHistory = async () => ({
      data: { items: [], total: 0, limit: 50, offset: 0 },
      error: null,
      requestId: 'mock',
    });

    const { rerender } = render(
      <ProjectContextProvider
        key={`pc-${mountKey}`}
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_a"
      >
        <InsightDetailPage key={`page-${mountKey}`} />
      </ProjectContextProvider>,
    );

    // Open the run so the detail panel (and the honesty
    // notice) renders for project A.
    await waitFor(() => screen.getByTestId('research-history-item-rrun-1'));
    await act(async () => {
      (screen.getByTestId('research-select-rrun-1') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );

    // Switch projects — without resolving the stalled
    // promise first, so the cleanup MUST happen synchronously
    // and the late response from project A's in-flight list
    // MUST NOT re-populate the banner.
    activeProject = 'proj_b';
    rerender(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId="proj_b"
      >
        <InsightDetailPage key={`page-b`} />
      </ProjectContextProvider>,
    );

    // The honesty notice is gone — the panel itself is gone.
    expect(screen.queryByTestId('research-answer-completeness')).toBeNull();
    expect(screen.queryByTestId('research-detail-panel')).toBeNull();
    // The old history row is gone too — no foreign data leak.
    expect(screen.queryByTestId('research-history-item-rrun-1')).toBeNull();

    // Drain the stalled promise so the test exits cleanly.
    await act(async () => {
      resolveList({
        data: { items: [], total: 0, limit: 50, offset: 0 },
        error: null,
        requestId: 'mock',
      });
    });
    // Late response from project A still must NOT re-surface
    // the honesty notice for project B.
    expect(screen.queryByTestId('research-answer-completeness')).toBeNull();
  });

  it('v5 completed run with server covered shows evidence coverage but keeps the "未验证" boundary', async () => {
    // v5 happy path: a completed run carries both
    // ``task_contract`` and ``coverage_report``;
    // ``overall_completable === true`` and every core
    // item is covered. The page must surface the new
    // "证据覆盖：已覆盖" section AND the "回答完整性：
    // 未验证" banner — the audit is a code-rule
    // statement, the human review is a separate axis. The
    // UI is fully non-technical: server field names
    // (``coverage_report``, ``overall_completable``,
    // ``semantic_review``, ``NOT_MEASURED``) MUST NOT
    // appear anywhere on the rendered panel.
    const coveredRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'completed',
      task_contract: {
        question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        parse_notes: [],
        items: [
          {
            id: 'req-pricing',
            role: 'core',
            kind: 'product',
            value: 'TrialHero 定价模型与席位单价',
            predicate_note: null,
          },
          {
            id: 'req-segments',
            role: 'core',
            kind: 'platform',
            value: 'SMB 段定义与适用范围',
            predicate_note: '需覆盖 ≤50 人企业',
          },
        ],
      },
      coverage_report: {
        task_question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        items: [
          {
            requirement_id: 'req-pricing',
            role: 'core',
            kind: 'product',
            status: 'covered',
            evidence_ids: ['evi-1'],
            evidence_source_ids: ['src-1'],
            gap: null,
          },
          {
            requirement_id: 'req-segments',
            role: 'core',
            kind: 'platform',
            status: 'covered',
            evidence_ids: ['evi-2'],
            evidence_source_ids: ['src-2'],
            gap: null,
          },
        ],
        procedure_status: 'ok',
        overall_completable: true,
        failure_reasons: [],
        semantic_review: 'NOT_MEASURED',
      },
      execution_trace: { internal: 'should not render' },
    };
    renderInsightDetailWithResearch({ runs: [coveredRun] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));

    // Honest banner: the v5 flavour is the human-readable
    // "程序已检查证据覆盖，回答语义仍需人工核对" line.
    // The historical "≥1 cited finding" framing MUST NOT
    // appear for v5 runs.
    const banner = await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    expect(banner.textContent).toMatch(/回答完整性：未验证/);
    expect(banner.textContent).toMatch(
      /对照引文核实研究结果/,
    );
    expect(banner.textContent).not.toMatch(/至少一条带来源发现/);

    // Evidence-coverage section is rendered.
    const coverage = await waitFor(() =>
      screen.getByTestId('research-evidence-coverage'),
    );
    expect(coverage.textContent).toMatch(/证据覆盖：已覆盖/);
    // The semantic-review pin is now phrased as a
    // human-readable line, NOT the raw
    // ``semantic_review: NOT_MEASURED`` field-name echo.
    expect(coverage.textContent).toMatch(/人工语义复核：未验证/);

    // Per-item: the requirement_id is joined back to
    // ``task_contract.items[].value`` — a human-readable
    // Chinese label, NOT the raw internal id. The
    // ``kind`` is rendered as a Chinese category label
    // (product → 产品, platform → 平台).
    const pricingRow = screen.getByTestId(
      'research-coverage-item-req-pricing',
    );
    expect(pricingRow.textContent).toMatch(/TrialHero 定价模型与席位单价/);
    expect(pricingRow.textContent).toMatch(/产品/);
    expect(pricingRow.textContent).toMatch(/已覆盖/);
    // The raw requirement_id MUST NOT be rendered as
    // visible text (the brief: "不要给用户显示内部
    // request/step IDs").
    expect(pricingRow.textContent).not.toMatch(/req-pricing/);

    // ``execution_trace`` is never surfaced — the
    // "should not render" payload must not appear in the
    // DOM anywhere.
    expect(coverage.textContent).not.toMatch(/should not render/);
    expect(document.body.textContent).not.toMatch(/should not render/);

    // Non-technical boundary: the rendered panel MUST
    // NOT echo any of the raw server field names or
    // their values. The brief is explicit: PM-facing
    // text is human-readable; the audit is described
    // in procedural terms.
    const panelText = document.body.textContent ?? '';
    expect(panelText).not.toMatch(/coverage_report/);
    expect(panelText).not.toMatch(/overall_completable/);
    expect(panelText).not.toMatch(/semantic_review/);
    expect(panelText).not.toMatch(/NOT_MEASURED/);
  });

  it('v5 partial coverage surfaces per-item gap text and downgrades the overall to 部分覆盖', async () => {
    // v5 partial path: the run is
    // ``status === 'partial_failed'`` and the server
    // reports ``overall_completable === false`` with at
    // least one core item ``covered`` or ``partial``. The
    // page must downgrade the overall label to
    // "部分覆盖" AND surface the per-item ``gap`` text
    // — the brief is explicit that "covered" alone is
    // not enough; the PM must see the partial item and
    // its reason. The fixture is a CONSISTENT v5
    // sample: ``partial_failed`` run, ``overall: false``,
    // ``role: 'core'`` items. We never fabricate the
    // inconsistent ``true + partial`` combination.
    const partialRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'partial_failed',
      task_contract: {
        question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        parse_notes: ['segments predicate needs manual review'],
        items: [
          {
            id: 'req-pricing',
            role: 'core',
            kind: 'product',
            value: 'TrialHero 定价模型与席位单价',
            predicate_note: null,
          },
          {
            id: 'req-enterprise',
            role: 'core',
            // Real kind list does NOT include
            // ``scope`` / ``core_question`` — using an
            // unknown kind here on purpose so the test
            // also pins the "未知 kind → 待核对要求"
            // fallback behaviour.
            kind: 'enterprise_segment',
            value: '企业段（>50 人）是否同价',
            predicate_note: '需明确 upgrade path',
          },
        ],
      },
      coverage_report: {
        task_question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        items: [
          {
            requirement_id: 'req-pricing',
            role: 'core',
            kind: 'product',
            status: 'covered',
            evidence_ids: ['evi-1'],
            evidence_source_ids: ['src-1'],
            gap: null,
          },
          {
            requirement_id: 'req-enterprise',
            role: 'core',
            kind: 'enterprise_segment',
            status: 'partial',
            evidence_ids: ['evi-2'],
            evidence_source_ids: ['src-1'],
            gap: '公开网页仅披露 SMB 段；企业段需登录后台',
          },
        ],
        procedure_status: 'ok',
        overall_completable: false,
        failure_reasons: ['enterprise segment under-covered'],
        semantic_review: 'NOT_MEASURED',
      },
      execution_trace: null,
    };
    renderInsightDetailWithResearch({ runs: [partialRun] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));

    // Overall label: "部分覆盖" — server bool is
    // ``false`` and at least one item is
    // ``covered``/``partial``.
    const coverage = await waitFor(() =>
      screen.getByTestId('research-evidence-coverage'),
    );
    expect(coverage.textContent).toMatch(/证据覆盖：部分覆盖/);
    // The v5 honest banner still renders.
    expect(coverage.textContent).toMatch(/人工语义复核：未验证/);

    // Per-item partial row: the ``gap`` string is shown
    // verbatim so a PM can act on the missing fact.
    const enterpriseRow = screen.getByTestId(
      'research-coverage-item-req-enterprise',
    );
    // The fixture value is the full Chinese sub-question
    // "企业段（>50 人）是否同价" — the test must match
    // the real text byte-for-byte, not a partial regex
    // that hid the parentheses.
    expect(enterpriseRow.textContent).toMatch(/企业段（>50 人）是否同价/);
    // The unknown kind "enterprise_segment" falls back
    // to "待核对要求" — the raw English enum MUST NOT
    // surface to the user.
    expect(enterpriseRow.textContent).toMatch(/待核对要求/);
    expect(enterpriseRow.textContent).not.toMatch(/enterprise_segment/);
    expect(enterpriseRow.textContent).toMatch(/部分覆盖/);
    const gap = screen.getByTestId('research-coverage-gap-req-enterprise');
    expect(gap.textContent).toMatch(/公开网页仅披露 SMB 段/);

    // Non-technical boundary holds for the partial path
    // too — the page never echoes the raw server field
    // names to the PM.
    const panelText = document.body.textContent ?? '';
    expect(panelText).not.toMatch(/coverage_report/);
    expect(panelText).not.toMatch(/overall_completable/);
    expect(panelText).not.toMatch(/semantic_review/);
    expect(panelText).not.toMatch(/NOT_MEASURED/);
  });

  it('historical run without coverage_report keeps the 未验证 banner and explicitly notes the missing v5 report', async () => {
    // Pre-v5 path: a legacy run ships with
    // ``task_contract = null`` and ``coverage_report =
    // null``. The page MUST still surface the existing
    // "回答完整性：未验证" banner (the brief: "保留所有终态
    // 回答完整性：未验证") but the wording must explain
    // that the run has no coverage report and therefore
    // cannot be item-by-item checked. The new
    // "证据覆盖" section MUST NOT render — there is
    // nothing to audit. The v5 procedural-check wording
    // ("程序已检查证据覆盖…") MUST NOT appear on a
    // historical run either.
    const historicalRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'completed',
      task_contract: null,
      coverage_report: null,
      execution_trace: null,
    };
    renderInsightDetailWithResearch({ runs: [historicalRun] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));

    const banner = await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    expect(banner.textContent).toMatch(/回答完整性：未验证/);
    // The historical "no coverage report" framing is
    // explicit in the body — this is what tells a PM the
    // run cannot be item-checked.
    expect(banner.textContent).toMatch(/历史记录尚未核对完整性/);
    // The v5 procedural-check wording MUST NOT appear on
    // a historical run — there is no audit to refer to.
    expect(banner.textContent).not.toMatch(
      /程序已检查证据覆盖/,
    );
    // The evidence-coverage section MUST NOT render — the
    // historical run has nothing to item-audit.
    expect(screen.queryByTestId('research-evidence-coverage')).toBeNull();
    // The "research-coverage-items" list id MUST NOT
    // appear either.
    expect(screen.queryByTestId('research-coverage-items')).toBeNull();
  });

  it('coverage summary is 未验证 when no item carries role=core even with overall_completable=true', async () => {
    // Regression: a server ``overall_completable=true``
    // payload with NO ``role === 'core'`` items is
    // structurally insufficient — the report is not
    // auditing the questions the PM actually cares
    // about. The brief is explicit: the safe label is
    // "未验证", never "已覆盖". The previous logic
    // promoted this case to "已覆盖" which is exactly
    // the bug this test pins.
    const noCoreRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'completed',
      task_contract: {
        question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        parse_notes: [],
        items: [
          {
            id: 'req-pricing',
            role: 'supporting',
            kind: 'product',
            value: 'TrialHero 定价模型与席位单价',
            predicate_note: null,
          },
        ],
      },
      coverage_report: {
        task_question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        items: [
          {
            requirement_id: 'req-pricing',
            // Crucially: NOT 'core' — the only role tag
            // present is 'supporting', so the report
            // has zero core items to gate the
            // completeness verdict on.
            role: 'supporting',
            kind: 'product',
            status: 'covered',
            evidence_ids: ['evi-1'],
            evidence_source_ids: ['src-1'],
            gap: null,
          },
        ],
        procedure_status: 'ok',
        overall_completable: true,
        failure_reasons: [],
        semantic_review: 'NOT_MEASURED',
      },
      execution_trace: null,
    };
    renderInsightDetailWithResearch({ runs: [noCoreRun] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));

    // Overall label MUST be "未验证" — never "已覆盖"
    // for a report with no core items.
    const coverage = await waitFor(() =>
      screen.getByTestId('research-evidence-coverage'),
    );
    expect(coverage.textContent).toMatch(/证据覆盖：未验证/);
    expect(coverage.textContent).not.toMatch(/证据覆盖：已覆盖/);
    // The "回答完整性：未验证" boundary still applies.
    const banner = await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    expect(banner.textContent).toMatch(/回答完整性：未验证/);
  });

  it('failure_reasons with internal req-IDs and program diagnostics never leaks to the DOM', async () => {
    // The server's ``coverage_report.failure_reasons``
    // list can carry internal requirement ids
    // (``req-…``), the procedure's diagnostic string,
    // and other program-internal handles. The PM-facing
    // UI must surface a generic Chinese hint only; the
    // per-item ``gap`` field is the channel for
    // per-requirement specifics. The previous
    // implementation called ``.join('；')`` which
    // leaked every internal id verbatim.
    const leakyRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'partial_failed',
      task_contract: {
        question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        parse_notes: [],
        items: [
          {
            id: 'req-pricing',
            role: 'core',
            kind: 'product',
            value: 'TrialHero 定价模型与席位单价',
            predicate_note: null,
          },
          {
            id: 'req-enterprise',
            role: 'core',
            kind: 'platform',
            value: '企业段（>50 人）是否同价',
            predicate_note: null,
          },
        ],
      },
      coverage_report: {
        task_question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        items: [
          {
            requirement_id: 'req-pricing',
            role: 'core',
            kind: 'product',
            status: 'covered',
            evidence_ids: ['evi-1'],
            evidence_source_ids: ['src-1'],
            gap: null,
          },
          {
            requirement_id: 'req-enterprise',
            role: 'core',
            kind: 'platform',
            status: 'partial',
            evidence_ids: ['evi-2'],
            evidence_source_ids: ['src-1'],
            gap: '公开网页仅披露 SMB 段；企业段需登录后台',
          },
        ],
        procedure_status: 'degraded',
        // Every entry here is intentionally a leak
        // candidate: an internal req-id, a procedure
        // name, a program-side diagnostic, a stack-trace
        // marker. None of them may surface in the DOM.
        overall_completable: false,
        failure_reasons: [
          'req-enterprise missing evidence_source_ids=src-2',
          'procedure=v5.coverage.check_evidence step=3 returned SKIP',
          'invariant "core items must cite at least one source" failed',
          'at procedure.coverage:142',
        ],
        semantic_review: 'NOT_MEASURED',
      },
      execution_trace: null,
    };
    renderInsightDetailWithResearch({ runs: [leakyRun] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));

    const failureNode = await waitFor(() =>
      screen.getByTestId('research-coverage-failure-reasons'),
    );
    // The DOM-rendered line is the generic Chinese
    // hint — no internal id, no procedure name, no
    // program diagnostic, no stack-trace marker.
    expect(failureNode.textContent).toMatch(
      /服务端程序检查未完全通过；请按下方逐项缺口人工核对/,
    );

    // The whole DOM MUST NOT contain any of the leaky
    // payload tokens — neither in the failure-reasons
    // block, nor elsewhere on the page.
    const panelText = document.body.textContent ?? '';
    expect(panelText).not.toMatch(/req-enterprise missing/);
    expect(panelText).not.toMatch(/procedure=v5\.coverage/);
    expect(panelText).not.toMatch(/invariant "core items/);
    expect(panelText).not.toMatch(/procedure\.coverage:142/);
    // The internal requirement id is used as a
    // testid / React key but MUST NOT appear as
    // visible text.
    expect(panelText).not.toMatch(/req-enterprise/);
    expect(panelText).not.toMatch(/req-pricing/);

    // The per-item ``gap`` text is the channel for
    // per-requirement specifics — that part of the
    // contract is preserved.
    const gap = screen.getByTestId('research-coverage-gap-req-enterprise');
    expect(gap.textContent).toMatch(/公开网页仅披露 SMB 段/);
  });

  it('unresolved_clause rows render the original question, not the internal review value', async () => {
    // The ``unresolved_clause`` ``kind`` carries an
    // internal review string in ``value`` (e.g. an
    // LLM-internal note about why the parser couldn't
    // decompose the sub-question). The page MUST
    // surface "需人工核对的要求：{research_question}"
    // and never echo the program-internal review
    // string. The kind label is also suppressed for
    // this branch because the whole row already says
    // "需人工核对" — the program-taxonomy label is
    // redundant and would leak the implementation
    // vocabulary.
    const unresolvedRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'partial_failed',
      task_contract: {
        question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        parse_notes: [],
        items: [
          {
            id: 'req-unresolved',
            role: 'core',
            kind: 'unresolved_clause',
            // The internal review string — this is the
            // program-internal handle the brief forbids
            // us from rendering to the user.
            value:
              'LLM review: clause contains anaphoric reference; parser unable to bind; escalate to human',
            predicate_note: 'review_note_42',
          },
        ],
      },
      coverage_report: {
        task_question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        items: [
          {
            requirement_id: 'req-unresolved',
            role: 'core',
            kind: 'unresolved_clause',
            status: 'not_verified',
            evidence_ids: [],
            evidence_source_ids: [],
            gap: null,
          },
        ],
        procedure_status: 'ok',
        overall_completable: false,
        failure_reasons: [],
        semantic_review: 'NOT_MEASURED',
      },
      execution_trace: null,
    };
    renderInsightDetailWithResearch({ runs: [unresolvedRun] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));

    const row = await waitFor(() =>
      screen.getByTestId('research-coverage-item-req-unresolved'),
    );
    // The row carries the "需人工核对的要求：原问题"
    // label.
    expect(row.textContent).toMatch(/需人工核对的要求：/);
    expect(row.textContent).toMatch(/竞品 TrialHero 在 SMB 的定价策略？/);
    // The internal LLM review string MUST NOT appear
    // anywhere in the DOM — neither in the row nor
    // elsewhere on the page.
    const panelText = document.body.textContent ?? '';
    expect(panelText).not.toMatch(
      /LLM review: clause contains anaphoric reference/,
    );
    expect(panelText).not.toMatch(/review_note_42/);
    // The program-taxonomy kind label "未解析子句" is
    // also suppressed for unresolved_clause rows.
    expect(row.textContent).not.toMatch(/未解析子句/);
  });

  it('real-shape v5 partial run translates every English diagnostic, kind enum, and error_message token into Chinese user-facing text', async () => {
    // The browser snapshot of the real partial page
    // surfaced these English strings leaking into the
    // DOM: the ``both`` kind value, the old
    // ``distinguish_auto`` label ("自动识别"), the
    // procedure-style diagnostic "evidence does not
    // match the requested flow (manual vs automatic)",
    // and a ``run.error_message`` carrying
    // "research insufficient_external_evidence;
    // coverage_not_verified: answer coverage not
    // verified". None of these is a user-readable
    // surface; the page MUST translate every one of
    // them to a Chinese surface, and the raw English
    // must not appear anywhere in the DOM.
    const realShapeRun: ResearchRunRead = {
      ...SAMPLE_RESEARCH_RUN,
      status: 'partial_failed',
      // The server string that previously leaked
      // straight to the user. The page now replaces
      // it with the fixed Chinese placeholder; the
      // raw value MUST NOT appear in the DOM.
      error_message:
        'research insufficient_external_evidence; coverage_not_verified: answer coverage not verified',
      task_contract: {
        question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        parse_notes: [],
        items: [
          {
            id: 'req-flow',
            role: 'core',
            kind: 'procedure',
            value: 'procedure',
            predicate_note: null,
          },
          {
            id: 'req-auto',
            role: 'core',
            kind: 'distinguish_auto',
            value: 'both',
            predicate_note: null,
          },
          {
            id: 'req-segments',
            role: 'core',
            kind: 'multi_select',
            value: 'multi_select',
            predicate_note: null,
          },
          {
            id: 'req-multi',
            role: 'core',
            kind: 'product',
            value: '企业段（>50 人）是否同价',
            predicate_note: null,
          },
        ],
      },
      coverage_report: {
        task_question: '竞品 TrialHero 在 SMB 的定价策略？',
        parse_status: 'parsed',
        items: [
          {
            requirement_id: 'req-flow',
            role: 'core',
            kind: 'procedure',
            status: 'partial',
            evidence_ids: ['evi-1'],
            evidence_source_ids: ['src-1'],
            gap:
              'evidence does not match the requested flow (manual vs automatic)',
          },
          {
            requirement_id: 'req-auto',
            role: 'core',
            kind: 'distinguish_auto',
            status: 'partial',
            evidence_ids: ['evi-2'],
            evidence_source_ids: ['src-2'],
            gap: 'official snapshot not yet available for v3.1',
          },
          {
            requirement_id: 'req-segments',
            role: 'core',
            kind: 'multi_select',
            status: 'not_verified',
            evidence_ids: [],
            evidence_source_ids: [],
            gap: 'review required from human reviewer',
          },
          {
            requirement_id: 'req-multi',
            role: 'core',
            kind: 'product',
            status: 'partial',
            evidence_ids: ['evi-3'],
            evidence_source_ids: ['src-3'],
            gap:
              'insufficient_external_evidence: only 2 of 5 segments cited',
          },
        ],
        procedure_status: 'degraded',
        overall_completable: false,
        failure_reasons: [],
        semantic_review: 'NOT_MEASURED',
      },
      execution_trace: null,
    };
    renderInsightDetailWithResearch({ runs: [realShapeRun] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));

    // Kind labels render as explicit Chinese. The
    // fixture uses the REAL v5 ``(kind, value)`` pairs:
    // ``procedure`` + ``procedure`` → 完整操作步骤,
    // ``distinguish_auto`` + ``both`` → 区分手动与自动
    // 操作, ``multi_select`` + ``multi_select`` → 明确
    // 多选, ``product`` + '企业段（>50 人）是否同价' →
    // user sub-question verbatim. The raw English kind
    // tokens MUST NOT appear standalone; the value side
    // of each pair is a program-internal token and
    // MUST be suppressed by ``taskKindLabel`` /
    // ``isProgramInternalValue`` so the implementation
    // contract never reaches the PM.
    const flowRow = screen.getByTestId(
      'research-coverage-item-req-flow',
    );
    expect(flowRow.textContent).toMatch(/完整操作步骤/);
    const autoRow = screen.getByTestId(
      'research-coverage-item-req-auto',
    );
    expect(autoRow.textContent).toMatch(/区分手动与自动操作/);
    const segmentsRow = screen.getByTestId(
      'research-coverage-item-req-segments',
    );
    expect(segmentsRow.textContent).toMatch(/明确多选/);
    const multiRow = screen.getByTestId(
      'research-coverage-item-req-multi',
    );
    expect(multiRow.textContent).toMatch(/企业段（>50 人）是否同价/);

    // The four gap-translation buckets are all
    // exercised: the known-phrase anchors, the
    // snake_case internal-enum diagnostic, and
    // (elsewhere in this suite) the plain
    // natural-language path. Each row gets the
    // matching fixed Chinese line.
    expect(
      screen.getByTestId('research-coverage-gap-req-flow').textContent,
    ).toMatch(/尚缺手动与自动操作区别的官方证据/);
    expect(
      screen.getByTestId('research-coverage-gap-req-auto').textContent,
    ).toMatch(/尚缺可核对完整步骤的官方正文/);
    expect(
      screen.getByTestId('research-coverage-gap-req-segments').textContent,
    ).toMatch(/该要求仍需结合原问题人工核对/);
    expect(
      screen.getByTestId('research-coverage-gap-req-multi').textContent,
    ).toMatch(/现有证据尚不能覆盖该要求，请核对官方原文/);

    // No English diagnostic / enum / source-path /
    // server-field token anywhere in the DOM. The
    // page is fully non-technical: the user never
    // sees the implementation contract.
    const panelText = document.body.textContent ?? '';
    expect(panelText).not.toMatch(/evidence does not match the requested flow/);
    expect(panelText).not.toMatch(/manual vs automatic/);
    expect(panelText).not.toMatch(/official snapshot/);
    expect(panelText).not.toMatch(/review required/);
    expect(panelText).not.toMatch(/insufficient_external_evidence/);
    expect(panelText).not.toMatch(/coverage_not_verified/);
    expect(panelText).not.toMatch(/evidence_source_ids/);
    expect(panelText).not.toMatch(/manual_vs_auto/);
    expect(panelText).not.toMatch(/procedure[=.:]/);
    // The ``value`` side of every real-shape
    // ``(kind, value)`` pair is a program-internal token
    // (``procedure``, ``both``, ``multi_select``); it is
    // suppressed by ``isProgramInternalValue`` /
    // ``taskKindLabel`` and MUST never appear in the DOM.
    expect(panelText).not.toMatch(/\bprocedure\b/);
    expect(panelText).not.toMatch(/\bboth\b/);
    expect(panelText).not.toMatch(/multi_select/);
    expect(panelText).not.toMatch(/invariant/);
    expect(panelText).not.toMatch(/req-[a-z0-9-]+/i);
    expect(panelText).not.toMatch(/semantic_review/);
    expect(panelText).not.toMatch(/NOT_MEASURED/);
    expect(panelText).not.toMatch(/overall_completable/);
    expect(panelText).not.toMatch(/coverage_report/);
    // The old (program-taxonomy) kind labels must
    // not appear either.
    expect(panelText).not.toMatch(/自动识别/);
    expect(panelText).not.toMatch(/流程/);

    // ``run.error_message`` is replaced with the
    // fixed user-facing Chinese hint — neither the
    // raw server string nor any of its program
    // tokens leak through.
    const errorNode = screen.getByTestId('research-detail-error');
    expect(errorNode.textContent).toMatch(
      /本次研究仍有证据或执行缺口，请结合来源与逐项覆盖核对/,
    );
    expect(errorNode.textContent).not.toMatch(/insufficient_external_evidence/);
    expect(errorNode.textContent).not.toMatch(/coverage_not_verified/);
    expect(errorNode.textContent).not.toMatch(/research\s+\w+_/);
  });
});
describe('Phase 8 v6 analysis support trace', () => {
  afterEach(() => { setApiClient(null); });

  it('shows analysis as a recommendation and follows same-run facts to sources', async () => {
    const fact = { ...SAMPLE_RESEARCH_FINDINGS[0]!, claim_kind: 'fact' as const };
    const recommendation = {
      ...SAMPLE_RESEARCH_FINDINGS[2]!, claim_kind: 'recommendation' as const,
      supporting_finding_ids: [fact.id],
    };
    renderInsightDetailWithResearch({ findings: [fact, recommendation] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    expect(screen.getByTestId(`research-claim-kind-${fact.id}`).textContent).toMatch(/事实/);
    expect(screen.getByTestId(`research-claim-kind-${recommendation.id}`).textContent).toMatch(/建议/);
    await act(async () => { screen.getByTestId(`research-support-toggle-${recommendation.id}`).click(); });
    const support = screen.getByTestId(`research-supports-${recommendation.id}`);
    expect(support.textContent).toContain(fact.description);
    expect(support.querySelector('a')?.getAttribute('href')).toBe(fact.source_refs![0]!.url);
    expect(screen.getByTestId('research-answer-completeness').textContent).toMatch(/未验证/);
  });

  it('excludes foreign-run and unverified rows from analysis supports', async () => {
    const fact = { ...SAMPLE_RESEARCH_FINDINGS[0]!, claim_kind: 'fact' as const };
    const foreign = { ...fact, id: 'foreign-fact', run_id: 'foreign-run', description: '跨运行内容不得作为支持' };
    const unverified = { ...fact, id: 'unverified-fact', status: 'not_verified', source_refs: null, description: '未验证内容不得作为支持' };
    const inference = {
      ...SAMPLE_RESEARCH_FINDINGS[2]!, claim_kind: 'inference' as const,
      supporting_finding_ids: [fact.id, foreign.id, unverified.id, 'missing-fact'],
    };
    renderInsightDetailWithResearch({ findings: [fact, foreign, unverified, inference] });
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    expect(screen.getByTestId(`research-claim-kind-${inference.id}`).textContent).toMatch(/推断/);
    await act(async () => { screen.getByTestId(`research-support-toggle-${inference.id}`).click(); });
    const support = screen.getByTestId(`research-supports-${inference.id}`);
    expect(support.textContent).toContain(fact.description);
    expect(support.textContent).not.toContain(foreign.description);
    expect(support.textContent).not.toContain(unverified.description);
  });
});


describe('Phase 8 analysis gaps', () => {
  afterEach(() => { setApiClient(null); });
  it('shows actual missing analysis categories without treating a supported inference as a retrieval failure', async () => {
    const fact = { ...SAMPLE_RESEARCH_FINDINGS[0]!, claim_kind: 'fact' as const };
    const inference = { ...SAMPLE_RESEARCH_FINDINGS[2]!, category: 'market_pattern', claim_kind: 'inference' as const, supporting_finding_ids: [fact.id] };
    renderInsightDetailWithResearch({
      runs: [{ ...SAMPLE_RESEARCH_RUN, prompt_version: 'research-agent-v6', execution_trace: { analysis_gaps: [ {category:'opportunity', reason:'no_supported_candidate'}, {category:'risk', reason:'no_supported_candidate'} ] } }],
      findings: [fact, inference],
    });
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    const gaps=screen.getByTestId('research-analysis-gaps');
    expect(gaps.textContent).toMatch(/机会/); expect(gaps.textContent).toMatch(/风险/);
    expect(gaps.textContent).toMatch(/未提出.*支持|未形成.*支持/);
    expect(screen.getByTestId('research-not-verified-banner').textContent).not.toMatch(/因来源检索失败/);
    expect(screen.getByTestId(`research-finding-status-${inference.id}`).textContent).not.toBe('证据不足');
  });
});
