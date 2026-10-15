import { afterEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ProjectContextProvider, useProjectContext } from '../../contexts/ProjectContext';
import { MockInsightFlowClient } from '../../lib/api/mock';
import { setApiClient, type InsightFlowClient } from '../../lib/api/client';
import FeedbackListPage from './page';

function SwitchProject() {
  const { selectProject } = useProjectContext();
  return <button onClick={() => selectProject('project-b')}>切换项目</button>;
}

function setup(withAnalysis: boolean) {
  const client = new MockInsightFlowClient();
  client.projects = [
    { id: 'project-a', name: 'A', product_name: 'A' },
    { id: 'project-b', name: 'B', product_name: 'B' },
  ];
  client.feedbackByProject.set('project-a', [
    { id: 'feedback-a', project_id: 'project-a', external_id: 'F-1', content: 'RAW-PROOF-DO-NOT-REWRITE' },
  ]);
  client.feedbackByProject.set('project-b', [
    { id: 'feedback-b', project_id: 'project-b', external_id: 'F-2', content: 'Other raw' },
  ]);
  client.analysisRuns = [
    { id: 'run-a', project_id: 'project-a', run_type: 'classification', prompt_version: 'v1',
      status: 'completed', input_count: 1, processed_count: 1, success_count: 1, failed_count: 0,
      created_at: '2026-10-15T00:00:00Z' },
  ];
  if (withAnalysis) client.feedbackAnalyses = [{
    id: 'analysis-a', feedback_id: 'feedback-a', run_id: 'run-a', feedback_type: 'pain_point',
    module: 'upload', intent: 'bulk_upload', pain_point: 'Repeated work', sentiment: 'negative',
    severity: 4, confidence: 0.9, semantic_text: 'Batch upload friction',
    created_at: '2026-10-15T00:01:00Z',
  }];
  setApiClient(client as unknown as InsightFlowClient);
  render(
    <ProjectContextProvider client={client as unknown as InsightFlowClient}
      initialProjects={client.projects} initialActiveProjectId="project-a">
      <SwitchProject />
      <FeedbackListPage />
    </ProjectContextProvider>,
  );
  return client;
}

afterEach(() => setApiClient(null));

describe('Feedback detail AI analysis', () => {
  it('loads full AI fields while preserving raw text and supports keyboard close', async () => {
    const client = setup(true);
    const row = await screen.findByRole('button', { name: '打开反馈 F-1 的详情' });
    await act(async () => fireEvent.keyDown(row, { key: 'Enter' }));
    await waitFor(() => expect(screen.getByText('Batch upload friction')).toBeTruthy());
    expect(within(screen.getByRole('dialog', { name: '反馈详情' })).getByText('RAW-PROOF-DO-NOT-REWRITE')).toBeTruthy();
    expect(screen.getByText('Repeated work')).toBeTruthy();
    expect(screen.getByText('bulk_upload')).toBeTruthy();
    expect(screen.getAllByText('run-a').length).toBeGreaterThan(0);
    expect(client.callLog.some((c) => c.path === '/api/projects/project-a/feedback/feedback-a/analysis')).toBe(true);
    await act(async () => fireEvent.keyDown(window, { key: 'Escape' }));
    expect(screen.queryByRole('dialog', { name: '反馈详情' })).toBeNull();
  });

  it('shows N/A without analysis and clears drawer when switching projects', async () => {
    setup(false);
    const row = await screen.findByRole('button', { name: '打开反馈 F-1 的详情' });
    await act(async () => fireEvent.click(row));
    await waitFor(() => expect(screen.getByText(/AI 分析 N\/A/)).toBeTruthy());
    await act(async () => fireEvent.click(screen.getByText('切换项目')));
    await waitFor(() => expect(screen.getByRole('button', { name: '打开反馈 F-2 的详情' })).toBeTruthy());
    expect(screen.queryByRole('dialog', { name: '反馈详情' })).toBeNull();
    expect(screen.queryByText('RAW-PROOF-DO-NOT-REWRITE')).toBeNull();
  });
});
