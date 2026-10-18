import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../lib/api/mock';
import { setApiClient } from '../../lib/api/client';
import type { InsightFlowClient } from '../../lib/api/client';
import type { Requirement } from '../../lib/api/types';

import RequirementsListPage from './page';

const SAMPLE_REQ: Requirement = {
  id: 'req-test-1',
  project_id: 'proj_a',
  insight_id: 'insight-1',
  decision_id: 'decision-1',
  score_id: 'score-1',
  insight_status_snapshot: 'accepted',
  decision_status_snapshot: 'accepted',
  ai_priority_snapshot: 'P1',
  pm_priority: 'P1',
  pm_override_reason_snapshot: null,
  source_snapshot_hash: 'hash-req-1',
  name: '激活漏斗引导',
  problem: '注册后无引导',
  target_user: '14 天试用用户',
  user_story: 'As a trial user, I want onboarding, so that I can find the core value.',
  solution_hypothesis: 'hypothesis',
  success_metrics: [
    {
      name: 'D1 激活率',
      type: 'metric_definition',
      sources: ['mock-run'],
      note: 'mock',
    },
  ],
  risks: [
    {
      category: 'known',
      description: 'risk',
      evidence_ids: [],
      mitigation: 'mitigation',
    },
  ],
  evidence_references: [{ external_id: 'ext-1', usage: 'quote' }],
  status: 'reviewing',
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

function renderList(opts: { items?: Requirement[] | null; missing?: boolean } = {}) {
  const client = new MockInsightFlowClient();
  setApiClient(client as unknown as InsightFlowClient);
  client.projects = [{ id: 'proj_a', name: 'A', product_name: 'A' }];
  if (opts.missing) {
    client.requirements = [];
  } else if (opts.items) {
    client.requirements = opts.items;
  } else {
    client.requirements = [SAMPLE_REQ];
  }

  const utils = render(
    <ProjectContextProvider
      client={client as unknown as never}
      initialProjects={client.projects}
      initialActiveProjectId="proj_a"
    >
      <RequirementsListPage />
    </ProjectContextProvider>,
  );

  return { client, utils };
}

describe('RequirementsListPage — Phase 7 B2', () => {
  beforeEach(() => {
    // per-test placeholder; tests will not exercise router
  });
  afterEach(() => setApiClient(null));

  it('renders requirements returned by GET /projects/{id}/requirements', async () => {
    renderList();
    await waitFor(() => screen.getByTestId('requirement-list'));
    const list = screen.getByTestId('requirement-list');
    expect(list.textContent).toContain('激活漏斗引导');
    // Page maps the backend ``status`` literal to its localised label
    // before rendering — the UI surface is the Chinese label, not the
    // raw backend enum value.
    expect(list.textContent).toContain('复核中');
    expect(list.textContent).toContain('P1');
  });

  it('shows an empty state when the project has no requirements', async () => {
    renderList({ missing: true });
    await waitFor(() => {
      expect(screen.getByText(/还没有产品需求/)).toBeTruthy();
    });
    expect(screen.queryByTestId('requirement-list')).toBeNull();
  });
});
