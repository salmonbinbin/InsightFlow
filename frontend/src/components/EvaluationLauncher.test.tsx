import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EvaluationLauncher } from './EvaluationLauncher';
import { MockInsightFlowClient } from '../lib/api/mock';
import { setApiClient, type InsightFlowClient } from '../lib/api/client';

function setup() {
  const client = new MockInsightFlowClient();
  client.analysisRuns = [{ id: 'a', project_id: 'p', run_type: 'classification', status: 'completed', prompt_version: 'v1', input_count: 2, processed_count: 2, success_count: 2, failed_count: 0 }];
  const create = vi.fn();
  Object.assign(client, { createEvaluation: create });
  setApiClient(client as unknown as InsightFlowClient);
  const onCreated = vi.fn();
  const view = render(<EvaluationLauncher key="p" projectId="p" onCreated={onCreated} />);
  return { client, create, onCreated, view };
}
afterEach(() => setApiClient(null));

it('blocks empty selection and active runs', async () => {
  const { client } = setup();
  client.analysisRuns[0]!.status = 'running';
  await waitFor(() => expect(screen.getByLabelText('选择分析记录').querySelectorAll('option')).toHaveLength(2));
  expect((screen.getByRole('button', { name: '生成检查报告' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText('选择分析记录').querySelector('option[value="a"]') as HTMLOptionElement).disabled).toBe(true);
});

it('prevents duplicate submission and ignores completion after a project switch', async () => {
  const { create, onCreated, view } = setup();
  let resolve!: (value: unknown) => void;
  create.mockImplementation(() => new Promise(r => { resolve = r; }));
  await waitFor(() => expect(screen.getByLabelText('选择分析记录').querySelectorAll('option')).toHaveLength(2));
  fireEvent.change(screen.getByLabelText('选择分析记录'), { target: { value: 'a' } });
  const button = screen.getByRole('button', { name: '生成检查报告' });
  fireEvent.click(button); fireEvent.click(button);
  expect(create).toHaveBeenCalledTimes(1);
  const signal = create.mock.calls[0]![2] as AbortSignal;
  view.rerender(<EvaluationLauncher key="other" projectId="other" onCreated={onCreated} />);
  expect(signal.aborted).toBe(true);
  await act(async () => resolve({ data: { id: 'new', project_id: 'p' }, error: null }));
  expect(onCreated).not.toHaveBeenCalled();
  expect((screen.getByLabelText('选择分析记录') as HTMLSelectElement).value).toBe('');
});

it('explains reference dataset mismatch without creating a displayed report', async () => {
  const { create, onCreated } = setup();
  create.mockResolvedValue({ data: null, error: { status: 409, message: 'mismatch' } });
  await waitFor(() => expect(screen.getByLabelText('选择分析记录').querySelectorAll('option')).toHaveLength(2));
  fireEvent.change(screen.getByLabelText('评测方式'), { target: { value: 'reference' } });
  fireEvent.change(screen.getByLabelText('选择分析记录'), { target: { value: 'a' } });
  fireEvent.click(screen.getByRole('button', { name: '运行参考样本评测' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('自定义反馈请选择');
  expect(onCreated).not.toHaveBeenCalled();
});
