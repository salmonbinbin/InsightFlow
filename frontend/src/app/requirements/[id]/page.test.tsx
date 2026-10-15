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
      expect(screen.getByText('检测到后端未知枚举值')).toBeTruthy();
      expect(screen.getByText(/Success Metric #1 类型 'foo'/)).toBeTruthy();
      expect(screen.getByText(/Risk #1 分类 'bar'/)).toBeTruthy();
    });

    // Per-row warnings: the type select is replaced with a read-only
    // badge carrying the raw value, so the dropdown can never silently
    // pick a default literal.
    expect(screen.getByTestId('metric-unknown-type-warning')).toBeTruthy();
    expect(screen.getByTestId('metric-unknown-type-badge').textContent).toContain('foo');
    expect(screen.getByTestId('risk-unknown-category-warning')).toBeTruthy();
    expect(screen.getByTestId('risk-unknown-category-badge').textContent).toContain('bar');

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
});
