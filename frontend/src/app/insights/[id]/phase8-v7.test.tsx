/**
 * Phase 8 v7 — page-level integration tests.
 *
 * Pins the v7 contract end-to-end at the page level
 * WITHOUT touching the existing page.test.tsx
 * harness. These tests use the same mock-client pattern
 * (MockInsightFlowClient + setApiClient + researchRuns /
 * researchFindings / researchSources fixtures) so the
 * assertions live on the rendered page subtree rather
 * than on the isolated ResearchAnswer component.
 *
 * Three surfaces covered:
 *
 *   1. v7 with answer — ``coverage_report.version ===
 *      'research-support-v7'`` AND
 *      ``coverage_report.answer.version ===
 *      'research-answer-v7'``. The page MUST route to
 *      ``<ResearchAnswer runId={...} findings={...}
 *      answer={...} />``, render the saved source
 *      span + provider_source_id annotation + source
 *      link, and NEVER touch the v5 ``failure_reasons``
 *      / ``items`` access (those would crash on
 *      undefined for a v7 envelope).
 *
 *   2. v7 planning failed — ``coverage_report.version ===
 *      'research-support-v7'`` but ``answer === null``
 *      AND ``task_contract === null``. The page MUST
 *      detect v7 by version alone (not by answer
 *      presence), render the v7 planning-failed honesty
 *      block, and NOT crash on the missing
 *      ``task_contract.items`` access.
 *
 *   3. v5 historical — a pre-v7 row with no
 *      ``coverage_report`` at all. The page MUST
 *      surface the historical "没有覆盖报告" banner
 *      and NOT attempt the v7 branch.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../../lib/api/mock';
import { setApiClient } from '../../../lib/api/client';
import type { InsightFlowClient } from '../../../lib/api/client';
import type {
  Insight,
  InsightDecision,
  InsightEvidenceEntry,
  OpportunityScore,
  ResearchAnswerV7,
  ResearchFindingRead,
  ResearchRunRead,
  ResearchSourceRead,
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

/* ---------- shared fixtures ---------- */

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

const SAMPLE_EVIDENCE: InsightEvidenceEntry[] = [];
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
    severity: { raw_value: '4', score: '0.8', available: true },
    growth: { raw_value: null, score: null, available: false, note: 'no history window' },
    user_value: { raw_value: '7', score: '0.7', available: true },
    business_value: { raw_value: null, score: null, available: false, note: 'Pending PM Input' },
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
  thresholds: { ai_priority_p0_below: '7.5', ai_priority_p1_below: '5' },
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

function makeDecision(): InsightDecision {
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
  };
}

/* ---------- v7 fixtures ---------- */

const SAMPLE_V7_ANSWER: ResearchAnswerV7 = {
  version: 'research-answer-v7',
  items: [
    {
      requirement_id: 'req-pricing',
      question: 'TrialHero 的 SMB 定价是否包含 14 天试用？',
      kind: 'capability',
      status: 'covered',
      answer_polarity: 'positive',
      quotes: [
        {
          finding_id: 'fact-1',
          evidence_id: 'ev-1',
          text: '标准版 14 天试用，企业版单独询价。',
          provider_source_id: 'src-1',
          url: 'https://trialhero.example/pricing',
        },
      ],
      gap: '',
      adopted_attempt: 2,
    },
  ],
  unresolved: [],
  analyses: [
    {
      finding_id: 'analysis-1',
      title: '样本差异',
      description: '推断：仅本次产品样本均有上述同维度原文支持。',
      claim_kind: 'inference',
      supporting_finding_ids: ['fact-1'],
      semantic_review: 'NOT_MEASURED',
    },
  ],
  semantic_review: 'NOT_MEASURED',
};

const SAMPLE_V7_RUN_WITH_ANSWER: ResearchRunRead = {
  id: 'rrun-v7-1',
  project_id: 'proj_a',
  insight_id: 'insight-1',
  classification_run_id: 'classification-1',
  clustering_run_id: 'clustering-1',
  insight_decision_id: null,
  research_question: 'TrialHero SMB 定价是否含 14 天试用？',
  scope: 'EU/SMB',
  provider: 'tavily',
  provider_query: 'TrialHero SMB pricing',
  model: 'gpt-4o-mini',
  prompt_version: 'research-agent-v7',
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
    research_question: 'TrialHero SMB 定价是否含 14 天试用？',
    scope: 'EU/SMB',
  },
  internal_context_hash: 'hash-v7',
  // The v7 envelope shape: the discriminator pins the
  // contract. v5 fields (``items`` / ``failure_reasons`` /
  // ``task_question``) are intentionally OMITTED so a v7
  // row cannot accidentally feed the v5 access path
  // (TS will still type them, but runtime guards in the
  // v5 branch keep the page honest).
  task_contract: null,
  coverage_report: {
    version: 'research-support-v7',
    task_question: '',
    parse_status: 'parsed',
    items: [],
    procedure_status: 'ok',
    overall_completable: true,
    failure_reasons: [],
    semantic_review: 'NOT_MEASURED',
    answer: SAMPLE_V7_ANSWER,
  },
  execution_trace: null,
  input_count: 1,
  processed_count: 1,
  source_count: 1,
  finding_count: 1,
  input_tokens: 100,
  output_tokens: 200,
  unknown_token_calls: 0,
  latency_ms: 5000,
  attempt_count: 2,
  retry_count: 0,
};

const SAMPLE_V7_RUN_PLANNING_FAILED: ResearchRunRead = {
  ...SAMPLE_V7_RUN_WITH_ANSWER,
  id: 'rrun-v7-2',
  status: 'partial_failed',
  error_message: null,
  // Planning failed: ``answer`` is null, ``task_contract``
  // is null. The page MUST detect v7 from ``version``
  // alone and render the planning-failed honesty row
  // instead of crashing on undefined v5 fields.
  task_contract: null,
  coverage_report: {
    version: 'research-support-v7',
    task_question: '',
    parse_status: 'parsed',
    items: [],
    procedure_status: 'ok',
    overall_completable: false,
    failure_reasons: [],
    semantic_review: 'NOT_MEASURED',
    answer: null,
  },
};

const SAMPLE_V7_FINDINGS: ResearchFindingRead[] = [
  {
    id: 'fact-1',
    run_id: 'rrun-v7-1',
    category: 'competitor',
    title: 'Competitor price page',
    description: '公开网页说明：标准版 14 天试用，企业版单独询价。',
    confidence: 0.9,
    status: 'confirmed',
    claim_kind: 'fact',
    source_refs: [
      {
        provider_source_id: 'src-1',
        url: 'https://trialhero.example/pricing',
        title: 'TrialHero Pricing',
      },
    ],
    created_at: '2026-10-14T01:00:05Z',
  },
];

const SAMPLE_V7_SOURCES: ResearchSourceRead[] = [
  {
    id: 'src-1',
    run_id: 'rrun-v7-1',
    provider: 'tavily',
    provider_source_id: 'src-1',
    url: 'https://trialhero.example/pricing',
    title: 'TrialHero Pricing',
    domain: 'trialhero.example',
    snippet: 'Pricing page snippet.',
    retrieved_at: '2026-10-14T01:00:01Z',
    created_at: '2026-10-14T01:00:01Z',
  },
];

const SAMPLE_HISTORICAL_RUN: ResearchRunRead = {
  ...SAMPLE_V7_RUN_WITH_ANSWER,
  id: 'rrun-historical-1',
  prompt_version: 'research-agent-v1',
  // Pre-v7: NO version field on coverage_report — the
  // legacy audit shape. v7 path must NOT trigger.
  coverage_report: null,
  task_contract: null,
  execution_trace: null,
};

/* ---------- mock harness ---------- */

interface PageV7HarnessOpts {
  runs?: ResearchRunRead[];
  findings?: ResearchFindingRead[];
}

function renderPageV7(initial: PageV7HarnessOpts = {}): { client: MockInsightFlowClient } {
  const client = new MockInsightFlowClient();
  setApiClient(client as unknown as InsightFlowClient);
  client.projects = [{ id: 'proj_a', name: 'A', product_name: 'Product A' }];
  client.insights = [SAMPLE_INSIGHT];
  client.researchRuns = initial.runs ?? [SAMPLE_V7_RUN_WITH_ANSWER];
  client.researchFindings = initial.findings ?? SAMPLE_V7_FINDINGS;
  client.researchSources = SAMPLE_V7_SOURCES;

  client.getInsight = async () => {
    client.log('GET', '/api/insights/insight-1');
    return { data: client.insights[0] ?? null, error: null, requestId: 'mock' };
  };
  client.getInsightEvidence = async (_id, query) => {
    client.log('GET', '/api/insights/insight-1/evidence', query);
    return {
      data: {
        insight_id: 'insight-1',
        items: SAMPLE_EVIDENCE,
        total: SAMPLE_EVIDENCE.length,
        limit: 25,
        offset: 0,
      },
      error: null,
      requestId: 'mock',
    };
  };
  client.getOpportunityScore = async () => {
    client.log('GET', '/api/insights/insight-1/opportunity-score');
    return { data: SAMPLE_SCORE, error: null, requestId: 'mock' };
  };
  client.getDecision = async () => {
    client.log('GET', '/api/insights/insight-1/decision');
    return { data: makeDecision(), error: null, requestId: 'mock' };
  };
  client.getDecisionHistory = async () => ({
    data: { items: [], total: 0, limit: 50, offset: 0 },
    error: null,
    requestId: 'mock',
  });
  client.startResearchRun = async (id, body) => {
    client.log('POST', `/api/insights/${id}/research`, body);
    return {
      data: SAMPLE_V7_RUN_WITH_ANSWER,
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
    const seeded =
      client.researchRuns.find((r) => r.id === runId) ??
      SAMPLE_V7_RUN_WITH_ANSWER;
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

  render(
    <ProjectContextProvider
      client={client as unknown as never}
      initialProjects={client.projects}
      initialActiveProjectId="proj_a"
    >
      <InsightDetailPage />
    </ProjectContextProvider>,
  );

  return { client };
}

/* ---------- tests ---------- */

describe('Phase 8 v7 — page-level v7 routing & legacy preservation', () => {
  beforeEach(() => {
    pushMock.mockClear();
  });

  afterEach(() => {
    setApiClient(null);
    vi.useRealTimers();
  });

  it('v7 with answer renders the saved source span + provider_source_id + source link without touching v5 fields', async () => {
    renderPageV7({
      runs: [SAMPLE_V7_RUN_WITH_ANSWER],
      findings: SAMPLE_V7_FINDINGS,
    });
    // The detail panel must land before any v7 assertion.
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    // The v7 renderer testid (``research-answer-v7``) is
    // mounted by the new branch above.
    const v7Block = await waitFor(() =>
      screen.getByTestId('research-answer-v7'),
    );
    expect(v7Block.getAttribute('data-version')).toBe('research-answer-v7');
    // The quote is the saved source span — NOT the
    // model-supplied title and NOT the internal evidence
    // id. The page MUST surface the URL the run cited.
    const quoteText = screen.getByTestId(
      'research-answer-v7-quote-text-req-pricing-0',
    );
    expect(quoteText.textContent).toMatch(/原文：标准版 14 天试用/);
    expect(quoteText.textContent).not.toMatch(/Competitor price page/);
    expect(quoteText.textContent).not.toMatch(/ev-1/);
    const sourceId = screen.getByTestId(
      'research-answer-v7-quote-source-id-req-pricing-0',
    );
    expect(sourceId.textContent).toMatch(/来源编号：src-1/);
    const link = screen.getByTestId(
      'research-answer-v7-quote-link-req-pricing-0',
    );
    expect(link.getAttribute('href')).toBe('https://trialhero.example/pricing');
    // The v5 evidence-coverage audit MUST NOT render for
    // a v7 row — the two branches are mutually exclusive.
    expect(screen.queryByTestId('research-evidence-coverage')).toBeNull();
    // The "回答完整性：未验证" banner must still surface
    // — the v7 contract is also unverified per the
    // manual-semantic-review boundary.
    const banner = await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    expect(banner.textContent).toMatch(/回答完整性：未验证/);
  });

  it('v7 planning-failed (no answer, task_contract=null) renders the honest v7 row instead of crashing', async () => {
    renderPageV7({
      runs: [SAMPLE_V7_RUN_PLANNING_FAILED],
      findings: [],
    });
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    // The v7 honesty row (planning failed) MUST surface
    // — detected by ``coverage_report.version ===
    // 'research-support-v7'`` alone, NOT by answer
    // presence. Without this guard the page would try
    // to read ``coverage_report.items`` / ``failure_reasons``
    // on a v7 row and crash with "Cannot read properties
    // of undefined".
    const planningFailed = await waitFor(() =>
      screen.getByTestId('research-answer-v7-planning-failed'),
    );
    expect(planningFailed.textContent).toMatch(/尚未形成回答/);
    // The page MUST NOT advertise any v7 quote / source
    // link for a planning-failed run — there is no
    // answer to render.
    expect(
      screen.queryByTestId('research-answer-v7-quote-text-req-pricing-0'),
    ).toBeNull();
    // The v5 evidence-coverage audit must NOT render —
    // a v7 envelope never carries the v5 ``items``
    // array.
    expect(screen.queryByTestId('research-evidence-coverage')).toBeNull();
    // The honesty banner still surfaces.
    const banner = await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    expect(banner.textContent).toMatch(/回答完整性：未验证/);
  });

  it('v5 historical run (no coverage_report) keeps the legacy honesty banner and skips the v7 branch', async () => {
    renderPageV7({
      runs: [SAMPLE_HISTORICAL_RUN],
      findings: [],
    });
    await waitFor(() => screen.getByTestId('research-detail-panel'));
    // The historical "没有覆盖报告" banner must surface —
    // the same banner the pre-v7 page rendered. The v7
    // branch MUST NOT trigger for a row with no
    // coverage_report.
    const banner = await waitFor(() =>
      screen.getByTestId('research-answer-completeness'),
    );
    expect(banner.textContent).toMatch(/回答完整性：未验证/);
    expect(banner.textContent).toMatch(/历史记录尚未核对完整性/);
    // Neither v7 surface is rendered.
    expect(screen.queryByTestId('research-answer-v7')).toBeNull();
    expect(
      screen.queryByTestId('research-answer-v7-planning-failed'),
    ).toBeNull();
    // The v5 evidence-coverage section is absent — the
    // historical row has nothing to audit.
    expect(screen.queryByTestId('research-evidence-coverage')).toBeNull();
  });
});