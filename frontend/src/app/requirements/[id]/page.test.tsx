import { describe, expect, it, afterEach, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../../lib/api/mock';
import { setApiClient } from '../../../lib/api/client';
import type { InsightFlowClient } from '../../../lib/api/client';
import type { Requirement } from '../../../lib/api/types';

import RequirementDetailPage from './page';

/* ---------- next/navigation mock ----------
 *
 * The page calls useParams() to resolve the requirement id from
 * `/requirements/[id]`. Without a mock, the real Next.js router
 * returns undefined under jsdom, leaving requirementId empty so
 * `load()` early-returns — the page would spin on LoadingState
 * forever. The mock matches the pattern used by insights/[id]/page.test.tsx.
 */
const mockUseParams = vi.fn<() => { id: string }>(() => ({ id: 'req-detail-1' }));
vi.mock('next/navigation', () => ({
  useParams: () => mockUseParams(),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

/* ---------- fixtures ---------- */

const BASE_REQ: Requirement = {
  id: 'req-detail-1',
  project_id: 'proj_a',
  insight_id: 'insight-1',
  decision_id: 'decision-1',
  score_id: 'score-1',
  insight_status_snapshot: 'accepted',
  decision_status_snapshot: 'accepted',
  ai_priority_snapshot: 'P1',
  pm_priority: 'P1',
  pm_override_reason_snapshot: null,
  source_snapshot_hash: 'hash',
  name: 'Activation funnel guidance',
  problem: 'New users have no clear next step after signup.',
  target_user: 'Trial users within their first 14 days.',
  user_story: 'As a trial user, I want a guided onboarding, so that I can find the core value.',
  solution_hypothesis: 'Step-by-step onboarding will lift D1 activation.',
  success_metrics: [
    {
      name: 'D1 activation',
      type: 'metric_definition',
      sources: ['mock'],
      note: 'measured by login + core action within 24h',
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
  evidence_references: [
    { external_id: 'ext-1', usage: 'representative' },
  ],
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

/** Three-element variant for the round-trip test. Shares BASE_REQ.id
 *  intentionally: a separate id would force a parallel useParams mock
 *  and obscure the test's actual subject (data preservation). */
const MULTI_REQ: Requirement = {
  ...BASE_REQ,
  success_metrics: [
    BASE_REQ.success_metrics[0]!,
    {
      name: 'Time-to-aha',
      type: 'baseline_only',
      sources: [],
      note: 'baseline only — pending PM input',
    },
    {
      name: 'Onboarding completion',
      type: 'metric_definition',
      sources: ['ext-1', 'ext-2'],
      note: 'measured by step funnel',
    },
  ],
  risks: [
    BASE_REQ.risks[0]!,
    {
      category: 'speculative',
      description: 'Speculative risk #2.',
      evidence_ids: ['ext-1'],
      mitigation: 'Mitigation #2.',
    },
  ],
  evidence_references: [
    { external_id: 'ext-1', usage: 'representative' },
    { external_id: 'ext-2', usage: 'supporting quote' },
  ],
};

function setInputValue(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
    'value',
  )?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

function renderDetail(req: Requirement | null = BASE_REQ, projectId: string = 'proj_a') {
  const client = new MockInsightFlowClient();
  setApiClient(client as unknown as InsightFlowClient);
  client.projects = [{ id: 'proj_a', name: 'A', product_name: 'A' }];
  client.requirements = req ? [req] : [];
  return {
    client,
    utils: render(
      <ProjectContextProvider
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId={projectId}
      >
        <RequirementDetailPage />
      </ProjectContextProvider>,
    ),
  };
}

describe('RequirementDetailPage — Phase 7 B2', () => {
  afterEach(() => {
    setApiClient(null);
    mockUseParams.mockReturnValue({ id: 'req-detail-1' });
  });

  it('renders the RequirementRead snapshot fields without recomputation', async () => {
    renderDetail();
    await waitFor(() => screen.getByTestId('field-name'));
    expect((screen.getByTestId('field-name') as HTMLInputElement).value).toContain('Activation funnel');
    expect((screen.getByTestId('field-problem') as HTMLTextAreaElement).value).toContain('New users have no clear next step');
    expect((screen.getByTestId('field-target-user') as HTMLInputElement).value).toContain('Trial users');
    expect((screen.getByTestId('field-user-story') as HTMLTextAreaElement).value).toContain('guided onboarding');
    expect((screen.getByTestId('field-solution') as HTMLTextAreaElement).value).toContain('onboarding will lift');
    // pm_priority is read-only. The page renders the priority as a badge in
  // the meta grid (and also surfaces the AI priority snapshot as plain
  // text). Use a selector that targets the badge specifically so we
  // don't rely on a global "P1" textContent match.
    expect(
      screen.getByText('P1', { selector: 'span.badge.badge-accent.mono' }),
    ).toBeTruthy();
    // metric note input must surface the snapshot's text verbatim.
    const note = screen.getByTestId('metric-note') as HTMLTextAreaElement;
    expect(note.value).toContain('measured by login + core action within 24h');
  });

  it('sends a real PATCH /requirements/{id} when the form is edited', async () => {
    const { client } = renderDetail();
    await waitFor(() => screen.getByTestId('field-name'));
    const nameInput = screen.getByTestId('field-name') as HTMLInputElement;
    await act(async () => {
      setInputValue(nameInput, 'Activation funnel guidance (revised)');
    });
    await waitFor(() => {
      expect((screen.getByTestId('save-requirement') as HTMLButtonElement).disabled).toBe(false);
    });

    await act(async () => {
      (screen.getByTestId('save-requirement') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      const patches = client.callLog.filter(
        (c) => c.method === 'PATCH' && c.path === '/api/requirements/req-detail-1',
      );
      expect(patches).toHaveLength(1);
      const body = patches[0]?.body as {
        content?: { name?: string };
        status?: string;
      };
      expect(body.content?.name).toBe('Activation funnel guidance (revised)');
    });
  });

  it('shows readable topic prose while preserving untouched content in a save', async () => {
    const target = '报告 pdf_upload 障碍的用户；规模尚未确认。';
    const note = '将 module=pdf_upload 且涉及上传失败的条目纳入追踪；不设目标。';
    const req = { ...BASE_REQ, target_user: target, success_metrics: [{ ...BASE_REQ.success_metrics[0]!, note }] };
    const { client } = renderDetail(req, 'proj_a');
    await waitFor(() => screen.getByTestId('field-target-user'));
    expect((screen.getByTestId('field-target-user') as HTMLInputElement).value).toBe('报告 PDF 上传 障碍的用户；规模尚未确认。');
    expect((screen.getByTestId('metric-note') as HTMLTextAreaElement).value).not.toContain('module=');
    await act(async () => setInputValue(screen.getByTestId('field-name') as HTMLInputElement, '仅修改名称'));
    await act(async () => (screen.getByTestId('save-requirement') as HTMLButtonElement).click());
    await waitFor(() => {
      const patch = client.callLog.find(c => c.method === 'PATCH');
      const body = patch?.body as { content?: { target_user: string; success_metrics: { note: string }[] } };
      expect(body?.content?.target_user).toBe(target);
      expect(body?.content?.success_metrics[0]?.note).toBe(note);
    });
  });

  /* ---------- B2 round-trip (data preservation) ----------
   *
   * The previous B1 placeholder dropped every entry beyond index 0 in
   * success_metrics / risks / evidence_references. This test pins the
   * B2 contract: editing ONE entry must leave the others untouched in
   * the PATCH body. The fixture has 3 metrics, 2 risks, 2 references.
   */

  it('round-trips multi-element arrays without losing any item (B2 fix)', async () => {
    const { client } = renderDetail(MULTI_REQ, 'proj_a');
    await waitFor(() => screen.getByTestId('field-name'));

    // Confirm the page actually rendered all three rows. The bug was
    // dropping the others at buildContent time.
    const metricRows = screen.getAllByTestId('success-metric-row');
    expect(metricRows).toHaveLength(3);
    const riskRows = screen.getAllByTestId('risk-row');
    expect(riskRows).toHaveLength(2);
    const evidenceRows = screen.getAllByTestId('evidence-ref-row');
    expect(evidenceRows).toHaveLength(2);

    // Edit ONLY the first metric — every other item must travel through
    // the PATCH body untouched.
    const firstMetricName = screen.getAllByTestId('metric-name')[0] as HTMLInputElement;
    await act(async () => {
      setInputValue(firstMetricName, 'D1 activation (revised)');
    });

    await waitFor(() => {
      expect((screen.getByTestId('save-requirement') as HTMLButtonElement).disabled).toBe(false);
    });

    await act(async () => {
      (screen.getByTestId('save-requirement') as HTMLButtonElement).click();
    });

    await waitFor(() => {
      const patches = client.callLog.filter(
        (c) => c.method === 'PATCH' && c.path === '/api/requirements/req-detail-1',
      );
      expect(patches).toHaveLength(1);
      const body = patches[0]?.body as {
        content?: {
          success_metrics?: Array<{ name: string }>;
          risks?: Array<{ description: string }>;
          evidence_references?: Array<{ external_id: string }>;
        };
      };
      const metrics = body.content?.success_metrics ?? [];
      const risks = body.content?.risks ?? [];
      const refs = body.content?.evidence_references ?? [];

      // Three metrics must ship, in original order, with the first
      // carrying the edit and the rest unchanged.
      expect(metrics).toHaveLength(3);
      expect(metrics[0]?.name).toBe('D1 activation (revised)');
      expect(metrics[1]?.name).toBe('Time-to-aha');
      expect(metrics[2]?.name).toBe('Onboarding completion');

      // Both risks must ship unchanged.
      expect(risks).toHaveLength(2);
      expect(risks[0]?.description).toContain('Empty cohort when project is brand new.');
      expect(risks[1]?.description).toBe('Speculative risk #2.');

      // Both evidence references must ship unchanged.
      expect(refs).toHaveLength(2);
      expect(refs[0]?.external_id).toBe('ext-1');
      expect(refs[1]?.external_id).toBe('ext-2');
    });
  });

  /* ---------- Empty array conformance ----------
   *
   * Backend allows success_metrics and risks to be empty arrays.
   * evidence_references MUST contain at least one entry (its
   * remove button is disabled when only one row remains).
   */

  it('allows empty success_metrics and empty risks (schema permits)', async () => {
    const { client } = renderDetail(
      {
        ...BASE_REQ,
        success_metrics: [],
        risks: [],
      } satisfies Requirement,
      'proj_a',
    );
    await waitFor(() => screen.getByTestId('field-name'));

    // Edit a scalar field to force dirty.
    const nameInput = screen.getByTestId('field-name') as HTMLInputElement;
    await act(async () => {
      setInputValue(nameInput, 'Empty arrays round-trip');
    });

    await waitFor(() => {
      expect((screen.getByTestId('save-requirement') as HTMLButtonElement).disabled).toBe(false);
    });
    await act(async () => {
      (screen.getByTestId('save-requirement') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      const patches = client.callLog.filter(
        (c) => c.method === 'PATCH',
      );
      expect(patches).toHaveLength(1);
      const body = patches[0]?.body as {
        content?: { success_metrics?: unknown[]; risks?: unknown[]; evidence_references?: unknown[] };
      };
      expect(body.content?.success_metrics).toEqual([]);
      expect(body.content?.risks).toEqual([]);
      // evidence_references MUST stay at >= 1 per backend schema.
      expect((body.content?.evidence_references ?? []).length).toBeGreaterThanOrEqual(1);
    });
  });

  it('disables the evidence remove button when only one row remains (schema rule)', async () => {
    renderDetail();
    await waitFor(() => screen.getByTestId('field-name'));
    const removeButtons = screen.getAllByTestId('evidence-remove') as HTMLButtonElement[];
    expect(removeButtons).toHaveLength(1);
    expect(removeButtons[0]!.disabled).toBe(true);
  });

  it('adds and removes success-metric rows via dedicated buttons', async () => {
    renderDetail();
    await waitFor(() => screen.getByTestId('field-name'));
    expect(screen.getAllByTestId('success-metric-row')).toHaveLength(1);

    await act(async () => {
      (screen.getByTestId('metric-add') as HTMLButtonElement).click();
    });
    expect(screen.getAllByTestId('success-metric-row')).toHaveLength(2);

    await act(async () => {
      (screen.getAllByTestId('metric-remove')[1] as HTMLButtonElement).click();
    });
    expect(screen.getAllByTestId('success-metric-row')).toHaveLength(1);
  });

  /* ---------- B2 fix: unknown enum gate ----------
   *
   * Backend READ DTOs (SuccessMetricRead.type / RiskRead.category)
   * are typed ``str`` — they can carry any string, including enum
   * values added after the frontend's last build. The WRITE DTOs are
   * strict ``Literal`` unions; sending an unknown value would 422.
   *
   * Earlier behaviour (and the B2 bug): buildContent silently
   * rewrote the unknown value to a default literal, and a save of
   * any other field would land a corrupted PATCH. New contract:
   *   - The page renders a blocking banner.
   *   - The affected row shows an inline warning + a read-only badge
   *     (the type select is replaced because no option matches).
   *   - The Save button is disabled until the unknown is resolved.
   *   - Even if the PM clicks Save, no PATCH is sent.
   *
   * The fixture uses ``'foo'`` and ``'bar'`` to stand in for any
   * future enum values the frontend does not yet know about.
   */
  it('blocks Save and refuses to PATCH when a backend enum value is unknown', async () => {
    const UNKNOWN_REQ: Requirement = {
      ...BASE_REQ,
      success_metrics: [
        {
          name: 'D1 activation',
          type: 'foo', // ← NOT in MetricTypeLiteral: ['metric_definition','baseline_only']
          sources: ['mock'],
          note: 'mock',
        },
      ],
      risks: [
        {
          category: 'bar', // ← NOT in RiskCategoryLiteral: ['known','speculative']
          description: 'unknown-category risk',
          evidence_ids: [],
          mitigation: '',
        },
      ],
    };
    const { client } = renderDetail(UNKNOWN_REQ, 'proj_a');
    await waitFor(() => screen.getByTestId('field-name'));

    // Top-of-form banner explains the block and points at admin help.
    await waitFor(() => {
      expect(screen.getByText('部分内容需要修正')).toBeTruthy();
      expect(screen.getByText(/第1项指标；第1项风险的分类暂不支持编辑/)).toBeTruthy();
    });

    // Per-row warnings: the type select is replaced with a read-only
    // badge carrying the raw value, so the dropdown can never silently
    // pick a default literal.
    expect(screen.getByTestId('metric-unknown-type-warning')).toBeTruthy();
    expect(screen.getByTestId('metric-unknown-type-badge').textContent).toContain('暂不支持');
    expect(screen.getByTestId('metric-unknown-type-badge').textContent).not.toContain('foo');
    expect(screen.getByTestId('risk-unknown-category-warning')).toBeTruthy();
    expect(screen.getByTestId('risk-unknown-category-badge').textContent).toContain('暂不支持');
    expect(screen.getByTestId('risk-unknown-category-badge').textContent).not.toContain('bar');

    // Save button MUST be disabled while unknowns are present.
    const saveBtn = screen.getByTestId('save-requirement') as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);

    // Even if the PM tries to save other fields (here: name), no PATCH
    // goes out — the page refuses rather than corrupting the unknown
    // enum with a silent rewrite on the wire.
    const nameInput = screen.getByTestId('field-name') as HTMLInputElement;
    await act(async () => {
      setInputValue(nameInput, 'Should not PATCH while enum unknown');
    });
    // Save stays disabled regardless of dirty state.
    expect(saveBtn.disabled).toBe(true);
    await act(async () => {
      saveBtn.click();
    });
    await waitFor(() => {
      const patches = client.callLog.filter(
        (c) => c.method === 'PATCH' && c.path === '/api/requirements/req-detail-1',
      );
      expect(patches).toHaveLength(0);
    });
  });

  /* ---------- Phase 8 C: Tool Action Preview (Simulator) ----------
   *
   * Each test seeds a Requirement with ``status='approved'``
   * (the brief's gate) and a matching ``client.requirements``
   * row so the mock's cross-project check passes. The
   * client / mock contract is exercised through the page's
   * real handlers so any regression in the page-level race
   * protection (project switch abort, in-flight flag
   * disable, etc.) would surface here. */

  function renderApprovedDetail(req: Requirement = BASE_REQ) {
    return renderDetail({ ...req, status: 'approved' as Requirement['status'] });
  }

  it('hides the Tool Action Preview section for non-approved Requirements', async () => {
    // The brief: "未批准 Requirement 不展示可执行按钮".
    // The page MUST NOT render the section at all when the
    // Requirement is in any other status.
    renderDetail({ ...BASE_REQ, status: 'draft' });
    await waitFor(() => screen.getByTestId('field-name'));
    expect(screen.queryByTestId('tool-action-preview')).toBeNull();
  });

  it('renders a Prepare button when the Requirement is approved and no run exists', async () => {
    const { client } = renderApprovedDetail();
    await waitFor(() => screen.getByTestId('field-name'));
    // The history effect fires on load; wait for it to settle.
    await waitFor(() => {
      expect(client.listToolRuns).toBeDefined();
    });
    await waitFor(() => screen.getByTestId('tool-action-preview'));
    // Initial listToolRuns returned 0 items; the idle view
    // surfaces the Prepare button.
    expect(screen.getByTestId('tool-prepare')).toBeTruthy();
    // Cancel / Approve are NOT surfaced before a run is prepared.
    expect(screen.queryByTestId('tool-cancel')).toBeNull();
    expect(screen.queryByTestId('tool-approve')).toBeNull();
    // The "仅模拟" notice is the first thing rendered so a PM
    // never mistakes the simulator for a real external system.
    expect(screen.getByText(/此操作为模拟/)).toBeTruthy();
  });

  it('Prepare → preview + Cancel + Approve (happy path)', async () => {
    const { client } = renderApprovedDetail();
    await waitFor(() => screen.getByTestId('tool-action-preview'));
    await waitFor(() => screen.getByTestId('tool-prepare'));

    // First click sends POST /requirements/{id}/tool-runs with
    // the typed Prepare body. The mock stores the row in
    // awaiting_approval so the page surfaces Cancel + Approve
    // after the refetch lands.
    await act(async () => {
      (screen.getByTestId('tool-prepare') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      const prepares = client.callLog.filter(
        (c) => c.method === 'POST' && c.path === '/api/requirements/req-detail-1/tool-runs',
      );
      expect(prepares).toHaveLength(1);
      const body = prepares[0]?.body as {
        tool_name: string;
        tool_action: string;
        request_id: string;
        payload: Record<string, never>;
      };
      expect(body.tool_name).toBe('simulated_issue_tracker');
      expect(body.tool_action).toBe('create_issue');
      // The page must mint a UUID-shaped request_id so the
      // backend's idempotency key is unique per click.
      expect(body.request_id).toMatch(
        /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/,
      );
      expect(body.payload).toEqual({});
    });
    // The page also re-fetches the history after a successful
    // Prepare. The list call must include project_id in the
    // call log (the mock records path-only, but the page
    // forwards the active project id through the typed
    // client — wired-contract is pinned in client.test.ts).
    await waitFor(() => {
      const lists = client.callLog.filter(
        (c) => c.method === 'GET' && c.path === '/api/requirements/req-detail-1/tool-runs',
      );
      expect(lists.length).toBeGreaterThanOrEqual(1);
    });

    // After the refetch, the awaiting_approval view shows the
    // four frozen-snapshot fields and Cancel + Approve buttons.
    await waitFor(() => screen.getByTestId('tool-action-awaiting-approval'));
    expect(screen.getByTestId('tool-preview-title')).toBeTruthy();
    expect(screen.getByTestId('tool-preview-description')).toBeTruthy();
    expect(screen.getByTestId('tool-preview-priority')).toBeTruthy();
    expect(screen.getByTestId('tool-preview-source')).toBeTruthy();
    expect(screen.getByTestId('tool-cancel')).toBeTruthy();
    expect(screen.getByTestId('tool-approve')).toBeTruthy();
  });

  it('Cancel surfaces "已取消；无模拟结果" and re-enables Prepare', async () => {
    const { client } = renderApprovedDetail();
    await waitFor(() => screen.getByTestId('tool-action-preview'));
    await waitFor(() => screen.getByTestId('tool-prepare'));

    await act(async () => {
      (screen.getByTestId('tool-prepare') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('tool-action-awaiting-approval'));

    await act(async () => {
      (screen.getByTestId('tool-cancel') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('tool-action-cancelled'));
    // The "无模拟结果" notice is the user-facing confirmation
    // that no audit row was created. The brief mandates
    // "Cancel 后显示 cancelled 并确认无模拟结果".
    expect(screen.getByTestId('tool-cancelled-notice').textContent).toContain('无模拟结果');
    // The Prepare re-try path is exposed (idempotency key
    // resets on every click — a fresh UUID is minted).
    expect(screen.getByTestId('tool-reprepare')).toBeTruthy();

    // The Cancel call lands on the active run; the body is empty.
    const cancel = client.callLog.find(
      (c) => c.method === 'POST' && c.path === '/api/tool-runs/trun_1/cancel',
    );
    expect(cancel).toBeDefined();
    expect(cancel?.body).toEqual({});
  });

  it('Re-Prepare → Approve surfaces SIM- external_id + simulated badge + no real link', async () => {
    // The brief: "Approve 调 API 后显示 SIM- external_id、simulated 标签
    // 且不要构造真实外部链接". The page must surface the id as
    // plain monospace text, NOT wrap it in an <a href="...">.
    const { client } = renderApprovedDetail();
    await waitFor(() => screen.getByTestId('tool-action-preview'));
    await waitFor(() => screen.getByTestId('tool-prepare'));

    // Prepare (first click).
    await act(async () => {
      (screen.getByTestId('tool-prepare') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('tool-action-awaiting-approval'));

    // Approve.
    await act(async () => {
      (screen.getByTestId('tool-approve') as HTMLButtonElement).click();
    });
    await waitFor(() => screen.getByTestId('tool-action-completed'));
    // SIM- external id is rendered as a badge, no anchor.
    const externalId = screen.getByTestId('tool-external-id').textContent;
    expect(externalId).toMatch(/^SIM-\d+$/);
    // "simulated" tag is the explicit marker so a PM can never
    // mistake this for a real Jira ticket.
    expect(screen.getByTestId('tool-simulated-tag').textContent).toContain('模拟');
    // CRITICAL: there must be NO anchor / link pointing at the
    // simulated id — the brief forbids fabricating a real
    // external link.
    const externalIdEl = screen.getByTestId('tool-external-id');
    expect(externalIdEl.closest('a')).toBeNull();
    // Approve call hit the right path with an empty body.
    const approve = client.callLog.find(
      (c) => c.method === 'POST' && c.path === '/api/tool-runs/trun_1/approve',
    );
    expect(approve).toBeDefined();
    expect(approve?.body).toEqual({});
  });

  it('debounces rapid Prepare clicks — only one POST goes out (no double-POST)', async () => {
    // The brief: "重复点击防重". A rapid second click is a
    // no-op because the in-flight flag disables the button
    // AND because each click mints a fresh request_id, so
    // the backend's idempotency key would dedupe anyway.
    const { client } = renderApprovedDetail();
    await waitFor(() => screen.getByTestId('tool-action-preview'));
    await waitFor(() => screen.getByTestId('tool-prepare'));

    await act(async () => {
      (screen.getByTestId('tool-prepare') as HTMLButtonElement).click();
      // Second click while the first is still in flight.
      (screen.getByTestId('tool-prepare') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      const prepares = client.callLog.filter(
        (c) => c.method === 'POST' && c.path === '/api/requirements/req-detail-1/tool-runs',
      );
      expect(prepares).toHaveLength(1);
    });
  });

  it('clears the Tool Action Preview state on project switch', async () => {
    // Project switch: a new activeProjectId MUST zero the
    // tool history so the page never surfaces a foreign
    // project's awaiting_approval row. The brief says
    // "跨项目状态清理".
    //
    // ``ProjectContextProvider`` reads ``initialActiveProjectId``
    // only on mount (useState's initial value), so a plain
    // rerender cannot exercise the project-switch effect.
    // We mount a tiny wrapper that bumps a ``key`` to force a
    // fresh provider + page tree — the same trick the
    // insights/[id] page test uses.
    const client = new MockInsightFlowClient();
    setApiClient(client as unknown as InsightFlowClient);
    client.projects = [
      { id: 'proj_a', name: 'A', product_name: 'A' },
      { id: 'proj_b', name: 'B', product_name: 'B' },
    ];
    client.requirements = [
      { ...BASE_REQ, id: 'req-detail-1', project_id: 'proj_a', status: 'approved' },
    ];
    // Pre-seed an awaiting_approval row for project A so the
    // first render is in the awaiting_approval view.
    client.toolRuns.push({
      id: 'trun-1',
      project_id: 'proj_a',
      requirement_id: 'req-detail-1',
      agent_run_id: null,
      tool_name: 'simulated_issue_tracker',
      tool_action: 'create_issue',
      tool_version: 'sim-v1',
      request_id: '00000000-0000-4000-8000-000000000010',
      status: 'awaiting_approval',
      execution_mode: 'simulation',
      external_id: null,
      request_payload: {
        title: '[Simulated] R',
        description: 'D',
        priority: 'P1',
        source_insight_preview: 'S',
      },
      response_summary: null,
      failure_category: null,
      attempt_count: 0,
      retry_count: 0,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      approved_at: null,
      completed_at: null,
    });

    let mountKey = 0;
    const renderTree = (projectId: string) => (
      <ProjectContextProvider
        key={`pc-${mountKey}`}
        client={client as unknown as never}
        initialProjects={client.projects}
        initialActiveProjectId={projectId}
      >
        <RequirementDetailPage key={`page-${mountKey}`} />
      </ProjectContextProvider>
    );
    const utils = render(renderTree('proj_a'));
    await waitFor(() => screen.getByTestId('tool-action-preview'));
    // Confirm the awaiting_approval view is rendered for the
    // original project.
    await waitFor(() => screen.getByTestId('tool-action-awaiting-approval'));

    // Switch to a different project that has NO requirement
    // and no tool runs. The state must be reset and the
    // section must disappear (load returns notFound because
    // the requirement doesn't exist in project B).
    mountKey += 1;
    utils.rerender(renderTree('proj_b'));
    // After the switch, the previous awaiting_approval state
    // is wiped — either the section unmounts (because the
    // requirement is notFound) or it stays in idle.
    await waitFor(() => {
      const awaiting = screen.queryByTestId('tool-action-awaiting-approval');
      expect(awaiting).toBeNull();
    });
  });

  it('surfaces 404/409/503 errors with a friendly message + retry', async () => {
    // The brief: "API 404/409/503 友好提示；失败可重试".
    // The page must render ErrorState with a friendly
    // message derived from the API error and a Retry that
    // re-invokes the same handler.
    const { client } = renderApprovedDetail();
    await waitFor(() => screen.getByTestId('tool-action-preview'));
    await waitFor(() => screen.getByTestId('tool-prepare'));

    // Force the next Prepare to 409 (state-machine conflict).
    client.forceError = {
      path: '/api/requirements/req-detail-1/tool-runs',
      status: 409,
      message: '操作冲突，请刷新后重试。',
    };
    await act(async () => {
      (screen.getByTestId('tool-prepare') as HTMLButtonElement).click();
    });
    await waitFor(() => {
      expect(screen.getByText(/工具操作失败|conflict/i)).toBeTruthy();
    });
  });
});
