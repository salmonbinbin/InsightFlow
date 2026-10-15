import { afterEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider, useProjectContext } from '../contexts/ProjectContext';
import { MockInsightFlowClient } from '../lib/api/mock';
import { setApiClient, type InsightFlowClient } from '../lib/api/client';
import type { AnalysisRun, Insight } from '../lib/api/types';
import OverviewPage from './page';

function SwitchProject() {
  const { selectProject } = useProjectContext();
  return <button onClick={() => selectProject('project-b')}>切换项目 B</button>;
}

const run = (id: string, runType: string, createdAt: string): AnalysisRun => ({
  id, project_id: 'project-a', run_type: runType, prompt_version: 'v1',
  status: 'completed', input_count: 12, processed_count: 12,
  success_count: 12, failed_count: 0, created_at: createdAt,
});

const insight = (index: number): Insight => ({
  id: `insight-${index}`, run_id: 'insight-run-a', project_id: 'project-a',
  cluster_id: `cluster-${index}`, clustering_run_id: 'cluster-run-a',
  title: `A 项目洞察 ${index}`, status: 'draft', feedback_count: 1,
  evidence_count: 1, source_cluster_external_id: `C${index}`,
  source_member_external_ids: ['F001'], prompt_version: 'v1',
});

afterEach(() => setApiClient(null));

describe('概览真实指标与项目隔离', () => {
  it('uses paginated totals and newest classification run, then hides A data on switch to B', async () => {
    const client = new MockInsightFlowClient();
    client.projects = [
      { id: 'project-a', name: '项目 A', product_name: 'A' },
      { id: 'project-b', name: '项目 B', product_name: 'B' },
    ];
    client.feedbackByProject.set('project-a', [{
      id: 'feedback-a', project_id: 'project-a', external_id: 'F001',
      content: 'A 项目原始反馈',
    }]);
    client.insights = Array.from({ length: 30 }, (_, i) => insight(i + 1));
    client.analysisRuns = [
      { ...run('old-classification', 'classification', '2026-10-14T00:00:00Z'),
        input_count: 3, processed_count: 3, success_count: 3 },
      run('new-classification', 'classification', '2026-10-15T00:00:00Z'),
      run('newer-clustering', 'clustering', '2026-10-16T00:00:00Z'),
    ];
    setApiClient(client as unknown as InsightFlowClient);
    render(
      <ProjectContextProvider client={client as unknown as InsightFlowClient}
        initialProjects={client.projects} initialActiveProjectId="project-a">
        <SwitchProject />
        <OverviewPage />
      </ProjectContextProvider>,
    );

    await waitFor(() => expect(screen.getByText('A 项目原始反馈')).toBeTruthy());
    expect(screen.getByText('30')).toBeTruthy();
    expect(screen.getByText('成功 12 / 输入 12')).toBeTruthy();
    expect(client.callLog.some((call) => call.path === '/api/projects/project-a/analysis-runs'
      && (call.body as { run_type?: string }).run_type === 'classification')).toBe(true);
    expect(client.callLog.some((call) => call.path === '/api/projects/project-a/insights'
      && (call.body as { limit?: number }).limit === 25)).toBe(true);

    await act(async () => fireEvent.click(screen.getByText('切换项目 B')));
    expect(screen.queryByText('A 项目原始反馈')).toBeNull();
    expect(screen.queryByText('A 项目洞察 1')).toBeNull();
    await waitFor(() => expect(screen.getByText('尚无反馈')).toBeTruthy());
    expect(screen.getByText('尚无洞察')).toBeTruthy();
    expect(screen.queryByText('30')).toBeNull();
    expect(client.callLog.some((call) => call.path === '/api/projects/project-b/feedback')).toBe(true);
  });
});
