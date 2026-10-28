import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ReferenceReviewLauncher } from './ReferenceReview';
import { MockInsightFlowClient } from '../lib/api/mock';
import { setApiClient, type InsightFlowClient } from '../lib/api/client';

function setup() {
  const client = new MockInsightFlowClient();
  client.analysisRuns = [{ id: 'a', project_id: 'p', run_type: 'classification', status: 'completed', prompt_version: 'v1', input_count: 2, processed_count: 2, success_count: 2, failed_count: 0 }];
  const create = vi.fn().mockResolvedValue({data:{id:'report',project_id:'p'},error:null});
  const download = vi.fn().mockResolvedValue({data:{project_id:'p',source_run_id:'a',csv_text:'header',filename:'review.csv'},error:null});
  Object.assign(client, {createReferenceReview:create, getReferenceTemplate:download});
  setApiClient(client as unknown as InsightFlowClient);
  const onCreated = vi.fn();
  const view = render(<ReferenceReviewLauncher key="p" projectId="p" onCreated={onCreated}/>);
  return {client,create,download,onCreated,view};
}
afterEach(()=>setApiClient(null));
async function select() {
  await waitFor(()=>expect(screen.getByLabelText('人工评测分析记录').querySelectorAll('option')).toHaveLength(2));
  fireEvent.change(screen.getByLabelText('人工评测分析记录'),{target:{value:'a'}});
}
function file(text: () => Promise<string> = async()=> 'labels') {
  const f = new File(['labels'],'review.csv',{type:'text/csv'});
  Object.defineProperty(f,'text',{value:text});
  fireEvent.change(screen.getByLabelText('上传已复核CSV'),{target:{files:[f]}});
}
it('requires selection and a file, submits labels and opens report',async()=>{
  const {create,onCreated}=setup();
  expect((screen.getByRole('button',{name:'生成参考答案评测'}) as HTMLButtonElement).disabled).toBe(true);
  await select(); file();
  await waitFor(()=>expect((screen.getByRole('button',{name:'生成参考答案评测'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button',{name:'生成参考答案评测'}));
  await waitFor(()=>expect(onCreated).toHaveBeenCalledWith('report'));
  expect(create.mock.calls[0]![0]).toMatchObject({project_id:'p',source_run_id:'a',csv_text:'labels'});
});
it('ignores a pending file read after changing selection',async()=>{
  const {create}=setup(); await select();
  let resolve!:(text:string)=>void;
  file(()=>new Promise(r=>{resolve=r;}));
  fireEvent.change(screen.getByLabelText('人工评测分析记录'),{target:{value:''}});
  await act(async()=>resolve('late labels'));
  expect((screen.getByRole('button',{name:'生成参考答案评测'}) as HTMLButtonElement).disabled).toBe(true);
  expect(create).not.toHaveBeenCalled();
});
it('prevents duplicate submission and ignores old-project completion',async()=>{
  const {create,onCreated,view}=setup(); await select(); file();
  let resolve!:(value:unknown)=>void;
  create.mockImplementation(()=>new Promise(r=>{resolve=r;}));
  const button=await screen.findByRole('button',{name:'生成参考答案评测'});
  await waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);fireEvent.click(button);
  expect(create).toHaveBeenCalledTimes(1);
  const signal=create.mock.calls[0]![1] as AbortSignal;
  view.rerender(<ReferenceReviewLauncher key="other" projectId="other" onCreated={onCreated}/>);
  expect(signal.aborted).toBe(true);
  await act(async()=>resolve({data:{id:'late',project_id:'p'},error:null}));
  expect(onCreated).not.toHaveBeenCalled();
});
it('surfaces upload validation failures and keeps report closed',async()=>{
  const {create,onCreated}=setup();await select();file();
  create.mockResolvedValue({data:null,error:{status:409,message:'原文已改变'}});
  const button=await screen.findByRole('button',{name:'生成参考答案评测'});
  await waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false));fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('原文已改变');
  expect(onCreated).not.toHaveBeenCalled();
});
