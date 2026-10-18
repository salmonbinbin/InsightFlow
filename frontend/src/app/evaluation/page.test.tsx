import { afterEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider } from '../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../lib/api/mock';
import { setApiClient, type InsightFlowClient } from '../../lib/api/client';
import type { EvaluationRunRead } from '../../lib/api/types';
import EvaluationPage from './page';

const RUN: EvaluationRunRead = {
  id: 'run-a', project_id: 'proj-a', evaluation_type: 'classification_metrics',
  stage: 'classification', status: 'partial_failed', dataset_name: 'demo_feedback',
  dataset_version: 'v1', dataset_content_hash: 'hash', dataset_provenance: 'demo',
  dataset_split: 'full',
  dataset_label_status: 'synthetic_reference_labels_unreviewed_no_human_review',
  model: 'MiniMax-M3', prompt_version: 'feedback-classification-v1',
  pipeline_version: 'v1', calculation_version: 'v1', input_count: 12,
  processed_count: 12, success_count: 12, failed_count: 0,
  input_tokens: 100, output_tokens: 100, unknown_token_calls: 0,
  latency_ms: 1000, total_cost_usd: null, attempt_count: 1, retry_count: 0,
  started_at: '2026-10-14T01:00:00Z', completed_at: '2026-10-14T01:01:00Z',
  created_at: '2026-10-14T01:00:00Z', error_message: null, notes: null,
};

function setup(projectId = 'proj-a') {
  const client = new MockInsightFlowClient();
  client.projects = [
    { id: 'proj-a', name: 'A', product_name: 'A' },
    { id: 'proj-b', name: 'B', product_name: 'B' },
  ];
  client.evaluationRuns = [structuredClone(RUN)];
  client.evaluationMetrics = [
    { id: 'm0', run_id: RUN.id, project_id: RUN.project_id, stage: 'classification',
      metric_name: 'feedback_type_accuracy', metric_kind: 'classification_metric',
      sample_count: 12, value: 0, value_display: '0.000000', details: {},
      created_at: '2026-10-14T01:00:00Z' },
    { id: 'mn', run_id: RUN.id, project_id: RUN.project_id, stage: 'classification',
      metric_name: 'intent_exact_match', metric_kind: 'classification_metric',
      sample_count: 0, value: null, value_display: 'NOT MEASURED', details: {},
      created_at: '2026-10-14T01:00:01Z' },
  ];
  client.evaluationCases = [
    { id: 'c1', run_id: RUN.id, project_id: RUN.project_id, case_id: 'F001',
      stage: 'classification', input_hash: 'hash', expected_hash: 'hash',
      actual_hash: 'hash', passed: false, error_type: 'wrong_label',
      eval_source: 'demo', notes: null, created_at: '2026-10-14T01:00:00Z',
      failed_stage: 'classification' },
  ];
  setApiClient(client as unknown as InsightFlowClient);
  render(
    <ProjectContextProvider client={client as unknown as InsightFlowClient}
      initialProjects={client.projects} initialActiveProjectId={projectId}>
      <EvaluationPage />
    </ProjectContextProvider>,
  );
  return client;
}

afterEach(() => setApiClient(null));

describe('Evaluation dashboard', () => {
  it('reads the selected project run and separates actual from documented targets', async () => {
    const client = setup();
    const row = await screen.findByTestId('eval-row-button');
    fireEvent.click(row);
    await waitFor(() => expect(screen.getAllByTestId('eval-metric-row')).toHaveLength(2));
    expect(screen.getByTestId('eval-dataset-name').textContent).toBe('demo_feedback');
    expect(screen.getByTestId('eval-sample-count').textContent).toBe('12');
    expect(screen.getByTestId('eval-label-status-unreviewed').textContent).toContain('未经人工复核');
    const metricRows = screen.getAllByTestId('eval-metric-row');
    expect(metricRows[0]?.querySelector('[data-testid="eval-metric-actual"]')?.textContent).toBe('0.000000');
    expect(metricRows[0]?.querySelector('[data-testid="eval-metric-target"]')?.textContent).toContain('90%');
    expect(metricRows[1]?.querySelector('[data-testid="eval-metric-actual"]')?.textContent).toBe('未测量');
    expect(metricRows[1]?.querySelector('[data-testid="eval-metric-target"]')?.textContent).toContain('85%');
    expect((await screen.findByTestId('eval-case-row')).textContent).toContain('F001');
    expect(client.callLog.some((c) => c.path === '/api/evaluations/run-a')).toBe(true);
    expect(client.callLog.some((c) => c.path === '/api/evaluations/run-a/metrics')).toBe(true);
    expect(client.callLog.some((c) => c.path === '/api/evaluations/run-a/cases')).toBe(true);
  });

  it('shows no run from another project', async () => {
    const client = setup('proj-b');
    expect(await screen.findByText('该项目下尚无评测')).toBeTruthy();
    expect(screen.queryByTestId('eval-row')).toBeNull();
    expect(client.callLog.some((c) => c.path === '/api/projects/proj-b/evaluations')).toBe(true);
    expect(client.callLog.some((c) => c.path.includes('/api/evaluations/run-a'))).toBe(false);
  });

  it('pages beyond 100 cases and finds a failure on the final page', async () => {
    const client = setup();
    client.evaluationCases = Array.from({ length: 102 }, (_, i) => ({
      id: `case-${i}`, run_id: RUN.id, project_id: RUN.project_id,
      case_id: `F${String(i + 1).padStart(3, '0')}`,
      stage: 'classification', input_hash: 'hash', expected_hash: 'hash',
      actual_hash: 'hash', passed: i !== 101,
      error_type: i === 101 ? 'wrong_label' : null,
      eval_source: 'demo', notes: null, created_at: '2026-10-14T01:00:00Z',
      failed_stage: i === 101 ? 'classification' : null,
    }));
    fireEvent.click(await screen.findByTestId('eval-row-button'));
    await waitFor(() => expect(screen.getByTestId('eval-cases-page-info').textContent).toContain('第 1 / 5 页'));
    for (let page = 2; page <= 5; page++) {
      fireEvent.click(screen.getByTestId('eval-cases-next'));
      await waitFor(() => expect(screen.getByTestId('eval-cases-page-info').textContent).toContain(`第 ${page} / 5 页`));
    }
    expect((await screen.findByTestId('eval-case-row')).textContent).toContain('F102');
    expect(client.callLog.filter((c) => c.path === '/api/evaluations/run-a/cases').length).toBeGreaterThanOrEqual(5);
  });
});
