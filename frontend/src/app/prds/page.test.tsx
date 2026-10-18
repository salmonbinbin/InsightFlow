import { describe, expect, it, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../lib/api/mock';
import { setApiClient } from '../../lib/api/client';
import type { InsightFlowClient } from '../../lib/api/client';
import type { PrdRead } from '../../lib/api/types';

import PrdsListPage from './page';

const SAMPLE_PRD: PrdRead = {
  id: 'prd-test-1',
  requirement_id: 'req-test-1',
  project_id: 'proj_a',
  insight_id: 'insight-1',
  version: 1,
  status: 'reviewing',
  content: {
    background: { text: 'bg', evidence_ids: ['ev-1'] },
    problem: { text: 'problem', evidence_ids: ['ev-1'] },
    target_user: { text: 'target user', evidence_ids: ['ev-1'] },
    user_story: { text: 'user story', evidence_ids: ['ev-1'] },
    product_goal: { text: 'product goal', evidence_ids: ['ev-1'] },
    proposed_solution: { text: 'solution', is_proposed: true, evidence_ids: ['ev-1'] },
    functional_requirements: [{ text: 'FR1', evidence_ids: ['ev-1'] }],
    user_flow: { text: 'flow', evidence_ids: ['ev-1'] },
    edge_cases: [{ category: 'empty_state', description: 'no data', evidence_ids: ['ev-1'] }],
    acceptance_criteria: [{ text: 'AC1', verifiable: true, note: '', evidence_ids: ['ev-1'] }],
    success_metrics: [{ name: 'metric 1', type: 'metric_definition', note: '', sources: ['run-1'] }],
    risks: [{ category: 'known', description: 'risk', evidence_ids: [], mitigation: '' }],
    mvp_scope: { in_scope: 'in', out_of_scope: 'out' },
    evidence_sources: ['run-1'],
    evidence_ids: ['ev-1'],
  },
  content_hash: 'hash-prd-1',
  is_ai_generated_draft: true,
  grounding_warnings: [],
  model: 'mock-model',
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

function renderList(opts: { items?: PrdRead[] | null; missing?: boolean } = {}) {
  const client = new MockInsightFlowClient();
  setApiClient(client as unknown as InsightFlowClient);
  client.projects = [{ id: 'proj_a', name: 'A', product_name: 'A' }];
  if (opts.missing) {
    client.prdReadList = [];
  } else if (opts.items) {
    client.prdReadList = opts.items;
  } else {
    client.prdReadList = [SAMPLE_PRD];
  }

  const utils = render(
    <ProjectContextProvider
      client={client as unknown as never}
      initialProjects={client.projects}
      initialActiveProjectId="proj_a"
    >
      <PrdsListPage />
    </ProjectContextProvider>,
  );

  return { client, utils };
}

describe('PrdsListPage — Phase 7 C1', () => {
  afterEach(() => setApiClient(null));

  it('renders PRDs returned by GET /projects/{id}/prds', async () => {
    renderList();
    await waitFor(() => screen.getByTestId('prd-list'));
    const list = screen.getByTestId('prd-list');
    // Version + status badge visible
    expect(list.textContent).toContain('v1');
    expect(list.textContent).toContain('复核中');
    // AI 草稿 badge surfaces the read-side flag
    expect(list.textContent).toContain('AI 草稿');
    // Each row links back to its Requirement so PMs can jump upstream
    expect(screen.getByTestId('prd-requirement-link')).toBeTruthy();
  });

  it('shows an empty state when the project has no PRDs', async () => {
    renderList({ missing: true });
    await waitFor(() => {
      expect(screen.getByText(/还没有需求文档/)).toBeTruthy();
    });
    expect(screen.queryByTestId('prd-list')).toBeNull();
  });

  it('displays multiple versions of the same PRD requirement without collapsing', async () => {
    // The backend returns every version. The page must surface all
    // of them; collapsing to one row per requirement would silently
    // drop earlier drafts / accepted versions from the audit trail.
    renderList({
      items: [
        { ...SAMPLE_PRD, id: 'prd-v1', version: 1, status: 'draft' },
        { ...SAMPLE_PRD, id: 'prd-v2', version: 2, status: 'reviewing' },
        { ...SAMPLE_PRD, id: 'prd-v3', version: 3, status: 'accepted', accepted_at: '2026-10-14T03:00:00Z' },
      ],
    });
    await waitFor(() => screen.getByTestId('prd-list'));
    const rows = screen.getAllByTestId('prd-row');
    expect(rows).toHaveLength(3);
    // Phase 7 C2: each row's version pill is now a Link to the
    // detail route. The testid moved to ``prd-row-link``; we read
    // its visible text to confirm version ordering survived C2.
    const versions = rows.map((row) => row.querySelector('[data-testid="prd-row-link"]')?.textContent);
    expect(versions).toEqual(['v3', 'v2', 'v1']);
  });

  it('links each row to /prds/{id} so PMs can reach the C2 detail workspace', async () => {
    renderList();
    await waitFor(() => screen.getByTestId('prd-list'));
    // Each row exposes two real anchors (the version pill and the
    // explicit "详情" button), both pointing at the C2 detail route.
    const versionLink = screen.getByTestId('prd-row-link');
    expect((versionLink as HTMLAnchorElement).getAttribute('href')).toBe('/prds/prd-test-1');
    const detailLink = screen.getByTestId('prd-open-detail');
    expect((detailLink as HTMLAnchorElement).getAttribute('href')).toBe('/prds/prd-test-1');
  });

  it('shows a pagination footer with both controls when total exceeds page size', async () => {
    const items: PrdRead[] = [];
    for (let i = 0; i < 30; i++) {
      items.push({ ...SAMPLE_PRD, id: `prd-${i}`, version: i + 1 });
    }
    renderList({ items });
    await waitFor(() => screen.getByTestId('prd-list'));
    // Default limit is 25 — 30 items should expose both buttons.
    const nextBtn = screen.getByRole('button', { name: /下一页/ });
    const prevBtn = screen.getByRole('button', { name: /上一页/ });
    expect(nextBtn).toBeTruthy();
    expect(prevBtn).toBeTruthy();
    expect(prevBtn.hasAttribute('disabled')).toBe(true);
  });
});
