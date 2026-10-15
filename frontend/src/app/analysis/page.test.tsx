import { afterEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider, useProjectContext } from '../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../lib/api/mock';
import { setApiClient, type InsightFlowClient } from '../../lib/api/client';
import type { AnalysisRun } from '../../lib/api/types';
import AnalysisPage from './page';

function SwitchProject() {
  const { selectProject } = useProjectContext();
  return <button onClick={() => selectProject('project-b')}>切换项目</button>;
}

function run(id: string, project_id: string, run_type: string): AnalysisRun {
  return {
    id, project_id, run_type, prompt_version: 'test', status: 'completed',
    input_count: 2, processed_count: 2, success_count: 2, failed_count: 0,
    created_at: '2026-10-15T01:00:00Z',
  };
}

function setup() {
  const client = new MockInsightFlowClient();
  client.projects = [
    { id: 'project-a', name: 'A', product_name: 'A' },
    { id: 'project-b', name: 'B', product_name: 'B' },
  ];
  client.analysisRuns = [
    run('a-class', 'project-a', 'classification'),
    run('b-class', 'project-b', 'classification'),
  ];
  client.clusterRuns = [run('a-cluster', 'project-a', 'clustering')];
  setApiClient(client as unknown as InsightFlowClient);
  render(
    <ProjectContextProvider client={client as unknown as InsightFlowClient}
      initialProjects={client.projects} initialActiveProjectId="project-a">
      <SwitchProject />
      <AnalysisPage />
    </ProjectContextProvider>,
  );
  return client;
}

afterEach(() => setApiClient(null));

describe('Analysis persisted history and project isolation', () => {
  it('loads classification and clustering from separate project-scoped server lists', async () => {
    const client = setup();
    await waitFor(() => expect(screen.getByTestId('analysis-history-row').getAttribute('data-run-id')).toBe('a-class'));
    await waitFor(() => expect(screen.getByTestId('clustering-history-row').getAttribute('data-run-id')).toBe('a-cluster'));
    expect(screen.queryByText('b-class')).toBeNull();
    const calls = client.callLog.filter((c) => c.path === '/api/projects/project-a/analysis-runs');
    expect(calls.some((c) => (c.body as { run_type?: string })?.run_type === 'classification')).toBe(true);
    expect(calls.some((c) => (c.body as { run_type?: string })?.run_type === 'clustering')).toBe(true);
  });

  it('rejects a foreign run ID and clears history on project switch', async () => {
    setup();
    await waitFor(() => screen.getByTestId('analysis-history-row'));
    const input = screen.getByPlaceholderText('例如 7b4d...');
    fireEvent.change(input, { target: { value: 'b-class' } });
    await act(async () => fireEvent.click(screen.getByText('加载')));
    await waitFor(() => expect(screen.getByText('该运行不属于当前项目，已拒绝显示。')).toBeTruthy());
    expect(screen.queryByText('分类 Run · b-class')).toBeNull();
    await act(async () => fireEvent.click(screen.getByText('切换项目')));
    await waitFor(() => expect(screen.getByTestId('analysis-history-row').getAttribute('data-run-id')).toBe('b-class'));
    expect(screen.queryByText('a-class')).toBeNull();
  });
});
