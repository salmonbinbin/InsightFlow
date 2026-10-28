'use client';

import { useEffect, useRef, useState } from 'react';
import { getApiClient } from '../lib/api/client';
import type { AnalysisRun, EvaluationCreateRequest } from '../lib/api/types';
import type { ReferenceReviewPage } from '../lib/api/reference-types';
import { displayLabel } from '../lib/presentation';

const MAX_BYTES = 2 * 1024 * 1024;
const TERMINAL = new Set(['completed', 'partial_failed', 'failed']);

/** Parent keys this component by project so file reads and requests die on project changes. */
export function ReferenceReviewLauncher({ projectId, onCreated }: { projectId: string; onCreated: (id: string) => void }) {
  const client = getApiClient();
  const [kind, setKind] = useState<'classification' | 'clustering'>('classification');
  const [rows, setRows] = useState<AnalysisRun[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState('');
  const [csvText, setCsvText] = useState('');
  const [fileName, setFileName] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const input = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; request.current?.abort(); }; }, []);
  function clearFile() {
    generation.current++; setCsvText(''); setFileName(''); setReading(false);
    if (input.current) input.current.value = '';
  }
  useEffect(() => {
    const c = new AbortController();
    setRows([]); setSelected(''); setLoading(true); setError(null);
    generation.current++; setCsvText(''); setFileName(''); setReading(false);
    if (input.current) input.current.value = '';
    void client.listProjectAnalysisRuns(projectId, { run_type: kind, limit: 25, offset }, c.signal).then(res => {
      if (c.signal.aborted) return;
      setLoading(false);
      if (res.error || !res.data) { setError(res.error?.message || '无法加载分析记录。'); return; }
      if (res.data.items.some(r => r.project_id !== projectId || r.run_type !== kind)) { setError('记录归属不一致，已拒绝显示。'); return; }
      setRows(res.data.items); setTotal(res.data.total);
    });
    return () => c.abort();
  }, [client, projectId, kind, offset, reload]);
  const source = rows.find(r => r.id === selected && TERMINAL.has(r.status));
  function body(): EvaluationCreateRequest {
    return { project_id: projectId, source_run_id: selected, evaluation_type: kind === 'classification' ? 'classification_metrics' : 'cluster_metrics' };
  }
  async function readFile(file?: File) {
    clearFile(); setError(null);
    if (!file) return;
    const version = generation.current;
    if (file.size > MAX_BYTES) { setError('CSV文件最大为2MiB。'); return; }
    setReading(true);
    try {
      const text = await file.text();
      if (!alive.current || generation.current !== version) return;
      if (!text.trim()) { setError('CSV文件为空。'); return; }
      setCsvText(text); setFileName(file.name);
    } catch { if (alive.current && generation.current === version) setError('文件读取失败，请重新选择UTF-8 CSV。'); }
    finally { if (alive.current && generation.current === version) setReading(false); }
  }
  async function act(download: boolean) {
    if (!source || request.current || (!download && !csvText)) return;
    const c = new AbortController(); request.current = c; setBusy(true); setError(null);
    try {
      if (download) {
        const res = await client.getReferenceTemplate(body(), c.signal);
        if (c.signal.aborted) return;
        if (res.error || !res.data) { setError(res.error?.message || '下载失败。'); return; }
        if (res.data.project_id !== projectId || res.data.source_run_id !== selected) { setError('模板归属不一致。'); return; }
        const url = URL.createObjectURL(new Blob(['\uFEFF', res.data.csv_text], { type: 'text/csv;charset=utf-8' }));
        const a = document.createElement('a'); a.href = url; a.download = res.data.filename;
        document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      } else {
        const res = await client.createReferenceReview({ ...body(), csv_text: csvText }, c.signal);
        if (c.signal.aborted) return;
        if (res.error || !res.data) { setError(res.error?.message || '评测失败。'); return; }
        if (res.data.project_id !== projectId) { setError('报告归属不一致。'); return; }
        onCreated(res.data.id);
      }
    } catch { if (!c.signal.aborted) setError('操作未完成，请重试。'); }
    finally { if (!c.signal.aborted) { request.current = null; setBusy(false); } }
  }
  return <section className="panel" aria-labelledby="reference-review-title">
    <h2 id="reference-review-title" className="section-title">人工参考答案评测</h2>
    <p>下载空白标签模板 → 人工复核 → 上传CSV。只对已有结果评分，不调用模型。上传会保存标签和审核人供报告追溯，不代表正式项目验收。</p>
    <label className="form-field"><span className="form-label">人工评测阶段</span><select className="form-input" value={kind} disabled={busy} onChange={e => { clearFile(); setSelected(''); setKind(e.target.value as typeof kind); setOffset(0); }}>
      <option value="classification">反馈分类</option><option value="clustering">聚类</option>
    </select></label>
    <label className="form-field"><span className="form-label">人工评测分析记录</span><select className="form-input" value={selected} disabled={busy || loading} onChange={e => { clearFile(); setError(null); setSelected(e.target.value); }}>
      <option value="">{loading ? '正在加载…' : '请选择已结束的运行'}</option>
      {rows.map(r => <option key={r.id} value={r.id} disabled={!TERMINAL.has(r.status)}>{displayLabel(r.status)} · {r.input_count}条 · {r.id}</option>)}
    </select></label>
    {total > 25 && <div className="pagination"><button className="btn btn-secondary" disabled={busy || loading || offset === 0} onClick={() => setOffset(offset - 25)}>上一批记录</button><span>共{total}条</span><button className="btn btn-secondary" disabled={busy || loading || offset + 25 >= total} onClick={() => setOffset(offset + 25)}>下一批记录</button></div>}
    <button className="btn btn-secondary" disabled={busy || loading} onClick={() => setReload(reload + 1)}>刷新人工评测记录</button>{' '}
    <button className="btn btn-secondary" disabled={busy || !source} onClick={() => void act(true)}>下载标注模板</button>
    <p>保留项目、运行、反馈ID、原文及指纹列。每条完成复核后填写审核人（reviewer），将review_status改为reviewed。可删除未复核行，只评估保留下来的样本。</p>
    {kind === 'classification' ? <p>填写feedback_type：bug、feature_request、ux_issue、performance_issue、pricing_issue、content_issue、positive_feedback、other之一；severity填写1—5，cluster_label留空。意图为自由文本，本轮不评估意图准确率。</p> : <p>只填写cluster_label：相同需求使用相同标签；feedback_type和severity留空。聚类逐条“通过”仅表示结果关联完整，语义分组质量请查看纯度、覆盖与碎片化。测试嵌入不等于真实语义能力。</p>}
    <label className="form-field"><span className="form-label">上传已复核CSV</span><input ref={input} type="file" accept=".csv,text/csv" disabled={busy || !source} onChange={e => void readFile(e.target.files?.[0])}/></label>
    {fileName && <p>已读取：{fileName}</p>}
    {error && <p role="alert">{error}</p>}
    <button className="btn btn-primary" disabled={busy || reading || !source || !csvText} onClick={() => void act(false)}>{busy ? '正在处理…' : reading ? '正在读取文件…' : '生成参考答案评测'}</button>
  </section>;
}

export function ReferenceReviewDetails({ projectId, runId }: { projectId: string; runId: string }) {
  const client = getApiClient();
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ReferenceReviewPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const c = new AbortController(); setData(null); setError(null);
    void client.getReferenceReview(runId, projectId, offset, c.signal).then(res => {
      if (c.signal.aborted) return;
      if (res.error || !res.data) { setError(res.error?.message || '无法读取人工标签。'); return; }
      if (res.data.project_id !== projectId || res.data.run_id !== runId) { setError('报告归属不一致。'); return; }
      setData(res.data);
    });
    return () => c.abort();
  }, [client, projectId, runId, offset]);
  return <section className="panel"><h3>人工标签与结果对照</h3>
    <p>标签由上传者声明已人工复核，系统未核实审核人身份；报告完成不代表正式验收。指标以本报告样本为准，流程统计仍对应整次原运行。</p>
    {error && <p role="alert">{error}</p>}
    {!data && !error && <p>正在读取…</p>}
    {data && <p>本次复核 {data.reviewed_count} 条 / 原运行输入 {data.source_input_count ?? '未记录'} 条；子样本成绩不代表全部数据。</p>}
    {data?.items.map(r => <div key={r.feedback_id} className="panel" style={{overflowWrap:'anywhere'}}>
      <strong>{r.feedback_id}</strong> · 审核人：{r.reviewer}
      {data.stage === 'classification' ? <>
        <p>人工参考：{displayLabel(r.expected.feedback_type || '')}，严重度 {r.expected.severity}</p>
        <p>已有结果：{r.actual ? `${displayLabel(r.actual.feedback_type || '')}，严重度 ${r.actual.severity}` : '缺失结果'}</p>
        <p>{r.passed ? '分类与严重度一致' : '存在差异或缺失'}</p>
      </> : <><p>人工主题：{r.expected.cluster_label}</p><p>分组编号：{r.actual?.cluster_ids?.join('、') || '无分组'}</p><p>{r.passed ? '结果关联完整；语义质量见汇总指标' : '关联缺失或重复'}</p></>}
    </div>)}
    {data && data.total > 25 && <div className="pagination"><button className="btn btn-secondary" disabled={offset === 0} onClick={() => setOffset(offset - 25)}>上一页标签</button><span>共{data.total}条</span><button className="btn btn-secondary" disabled={offset + 25 >= data.total} onClick={() => setOffset(offset + 25)}>下一页标签</button></div>}
  </section>;
}
