import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, cleanup } from '@testing-library/react';
import type { PrdRead } from '../../../lib/api/types';
const { client, controller, view } = vi.hoisted(() => ({ client: { getPrd: vi.fn(), listPrds: vi.fn(), createPrd: vi.fn() }, controller: new AbortController(), view: { activeProjectId: 'proj_b', prdId: 'prd-1' } }));
vi.mock('next/navigation', () => ({ useParams: () => ({ id: view.prdId }), useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../../../contexts/ProjectContext', () => ({ useProjectContext: () => ({ activeProjectId: view.activeProjectId }) }));
vi.mock('../../../lib/useProjectScopedFetch', () => ({ useProjectScopedFetch: () => ({ signal: controller.signal }) }));
vi.mock('../../../lib/api/client', () => ({ getApiClient: () => client }));
import PrdDetailPage from './page';
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

beforeEach(() => { view.activeProjectId = 'proj_b'; view.prdId = 'prd-1'; client.getPrd.mockReset(); client.listPrds.mockReset(); client.createPrd.mockReset(); });
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('does not read A history after receiving a foreign A artifact while B is active', async () => {
  client.getPrd.mockResolvedValue({ data: SAMPLE_PRD, error: null });
  client.listPrds.mockResolvedValue({ data: { items: [], total: 0 }, error: null });
  render(<PrdDetailPage />);
  await waitFor(() => expect(client.getPrd).toHaveBeenCalled());
  await waitFor(() => expect(client.getPrd).toHaveBeenCalledWith('prd-1', controller.signal, 'proj_b'));
  expect(client.listPrds).not.toHaveBeenCalled();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function row(id: string, project: string, req: string, version: number, text: string): PrdRead {
  return { ...SAMPLE_PRD, id, project_id: project, requirement_id: req, version,
    content: { ...SAMPLE_PRD.content, background: { ...SAMPLE_PRD.content.background, text } } };
}
function history(row: PrdRead) {
  return { data: { items: [row], total: 1, limit: 25, offset: 0, active_prd_id: row.id, active_version: row.version }, error: null };
}
it('keeps B not-found state after a late A detail on the same URL', async () => {
  const a = row('prd-1', 'proj_a', 'req-a', 101, 'A_PRIVATE_BODY');
  const late = deferred<{ data: PrdRead; error: null }>();
  view.activeProjectId = 'proj_a';
  client.getPrd.mockImplementationOnce(() => late.promise).mockResolvedValue({ data: null, error: { status: 404, code: 'not_found', message: 'Not found' } });
  client.listPrds.mockResolvedValue(history(a));
  const mounted = render(<PrdDetailPage />);
  await waitFor(() => expect(client.getPrd).toHaveBeenCalledWith('prd-1', controller.signal, 'proj_a'));
  view.activeProjectId = 'proj_b';
  mounted.rerender(<PrdDetailPage />);
  await waitFor(() => expect(client.getPrd).toHaveBeenCalledWith('prd-1', controller.signal, 'proj_b'));
  await waitFor(() => expect(screen.getByText('文档不存在')).toBeTruthy());
  await act(async () => { late.resolve({ data: a, error: null }); await late.promise; });
  expect(controller.signal.aborted).toBe(false);
  expect(screen.getByText('文档不存在')).toBeTruthy();
  expect(screen.queryByText('A_PRIVATE_BODY')).toBeNull();
  expect(client.listPrds).not.toHaveBeenCalled();
});
it('keeps B body and history after a late already-started A history', async () => {
  const a = row('prd-a', 'proj_a', 'req-a', 101, 'A_PRIVATE_BODY');
  const b = row('prd-b', 'proj_b', 'req-b', 202, 'B_CURRENT_BODY');
  const late = deferred<ReturnType<typeof history>>();
  view.activeProjectId = 'proj_a'; view.prdId = 'prd-a';
  client.getPrd.mockImplementation((id: string) => Promise.resolve({ data: id === 'prd-a' ? a : b, error: null }));
  client.listPrds.mockImplementation((id: string) => id === 'req-a' ? late.promise : Promise.resolve(history(b)));
  const mounted = render(<PrdDetailPage />);
  await waitFor(() => expect(client.listPrds).toHaveBeenCalledWith('req-a', controller.signal, 'proj_a'));
  view.activeProjectId = 'proj_b'; view.prdId = 'prd-b'; mounted.rerender(<PrdDetailPage />);
  await waitFor(() => {
    expect(screen.getByText('B_CURRENT_BODY')).toBeTruthy();
    expect(client.listPrds).toHaveBeenCalledWith('req-b', controller.signal, 'proj_b');
    expect(screen.getAllByTestId('prd-history-version').map(e => e.textContent)).toEqual(['v202']);
  });
  const count = client.listPrds.mock.calls.length;
  await act(async () => { late.resolve(history(a)); await late.promise; });
  expect(controller.signal.aborted).toBe(false);
  expect(screen.getByText('B_CURRENT_BODY')).toBeTruthy();
  expect(screen.queryByText('A_PRIVATE_BODY')).toBeNull();
  expect(screen.getByTestId('prd-current-version').textContent).toBe('v202');
  expect(screen.getAllByTestId('prd-history-version').map(e => e.textContent)).toEqual(['v202']);
  expect(client.listPrds.mock.calls.slice(count).some(([id]) => id === 'req-a')).toBe(false);
});

it('regenerates a v4 artifact with the same grounded v4 contract and current project', async () => {
  const current = { ...row('prd-1', 'proj_b', 'req-b', 2, 'GROUNDED_BODY'), prompt_version: 'prd-generation-v4' };
  client.getPrd.mockResolvedValue({ data: current, error: null });
  client.listPrds.mockResolvedValue(history(current));
  client.createPrd.mockResolvedValue({ data: null, error: { status: 503, code: 'unavailable', message: 'unavailable' } });
  render(<PrdDetailPage />);
  await waitFor(() => expect(screen.getByText('GROUNDED_BODY')).toBeTruthy());
  await act(async () => { (screen.getByTestId('regenerate-prd') as HTMLButtonElement).click(); });
  expect(client.createPrd).toHaveBeenCalledWith('req-b', { prompt_version: 'prd-generation-v4' }, controller.signal, 'proj_b');
});
