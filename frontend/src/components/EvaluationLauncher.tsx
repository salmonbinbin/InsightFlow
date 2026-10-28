'use client';

import { useEffect, useRef, useState } from 'react';
import { getApiClient } from '../lib/api/client';
import type { AnalysisRun, EvaluationCreateRequest } from '../lib/api/types';
import { classifyError, formatRelative } from '../lib/format';
import { displayLabel, userMessage } from '../lib/presentation';

const PAGE_SIZE = 25;
const TERMINAL = new Set(['completed', 'partial_failed', 'failed']);

/** Mounted with key=projectId so old requests/selections cannot leak between projects. */
export function EvaluationLauncher({ projectId, onCreated }: {
  projectId: string;
  onCreated: (id: string) => void;
}) {
  const client = getApiClient();
  const [kind, setKind] = useState<'classification' | 'clustering'>('classification');
  const [mode, setMode] = useState<'run-check' | 'reference'>('run-check');
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<AnalysisRun[]>([]);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const submission = useRef<AbortController | null>(null);
  useEffect(() => () => { submission.current?.abort(); }, []);

  useEffect(() => {
    const controller = new AbortController();
    setRows([]); setSelected(''); setLoading(true); setError(null);
    void client.listProjectAnalysisRuns(projectId, { run_type: kind, limit: PAGE_SIZE, offset }, controller.signal)
      .then(res => {
        if (controller.signal.aborted) return;
        setLoading(false);
        if (res.error || !res.data) { setError(classifyError(res.error)); return; }
        if (res.data.items.some(r => r.project_id !== projectId || r.run_type !== kind)) {
          setError('分析记录不属于当前项目或阶段，已拒绝显示。'); return;
        }
        setRows(res.data.items); setTotal(res.data.total);
      });
    return () => controller.abort();
  }, [client, projectId, kind, offset, reload]);

  async function submit() {
    const source = rows.find(r => r.id === selected);
    if (!source || !TERMINAL.has(source.status) || submitting || submission.current) return;
    const controller = new AbortController();
    submission.current = controller;
    setSubmitting(true); setError(null);
    const body: EvaluationCreateRequest = {
      project_id: projectId, source_run_id: source.id,
      evaluation_type: kind === 'classification' ? 'classification_metrics' : 'cluster_metrics',
    };
    const res = await client.createEvaluation(body, mode, controller.signal);
    if (controller.signal.aborted) return;
    submission.current = null;
    setSubmitting(false);
    if (res.error || !res.data) {
      setError(res.error?.status === 409
        ? (mode === 'reference' ? '该记录不满足内置参考样本要求。自定义反馈请选择“当前项目运行检查”。' : userMessage(res.error.message))
        : classifyError(res.error));
      return;
    }
    if (res.data.project_id !== projectId) { setError('报告项目不匹配，已拒绝显示。'); return; }
    onCreated(res.data.id);
  }

  return <section className="panel" aria-labelledby="evaluation-launch-title">
    <h2 id="evaluation-launch-title" className="section-title">发起评测</h2>
    <p>检查已有结果，不重新运行分析，也不会调用模型。报告完成不代表内容质量通过。</p>
    <label className="form-field"><span className="form-label">评测方式</span>
      <select className="form-input" value={mode} disabled={submitting} onChange={e => { setMode(e.target.value as typeof mode); setError(null); }}>
        <option value="run-check">当前项目运行检查（支持自定义反馈）</option>
        <option value="reference">内置参考样本评测（仅限配套演示数据）</option>
      </select>
    </label>
    <p className="run-launch-hint">{mode === 'run-check'
      ? '检查运行统计、结果关联完整性及缺失项。未提供人工参考答案，准确率和聚类纯度显示“未测量”；不会把模型输出当成标准答案。'
      : '仅适用于与内置演示数据完全匹配的记录；参考标签未经人工复核，不代表正式质量验收。'}</p>
    <label className="form-field"><span className="form-label">分析阶段</span>
      <select className="form-input" value={kind} disabled={submitting} onChange={e => { setKind(e.target.value as typeof kind); setOffset(0); setSelected(''); }}>
        <option value="classification">反馈分类</option><option value="clustering">整理反馈主题（聚类）</option>
      </select>
    </label>
    <label className="form-field"><span className="form-label">选择分析记录</span>
      <select className="form-input" value={selected} disabled={loading || submitting} onChange={e => setSelected(e.target.value)}>
        <option value="">{loading ? '正在加载…' : '请选择一次已结束的运行'}</option>
        {rows.map(r => <option key={r.id} value={r.id} disabled={!TERMINAL.has(r.status)}>
          {r.created_at ? formatRelative(r.created_at) : '已有记录'} · {displayLabel(r.status)} · {r.input_count} 条 · {r.id}
        </option>)}
      </select>
    </label>
    {!loading && rows.length === 0 && <p>暂无该阶段记录，请先到“分析”页面运行。</p>}
    {total > PAGE_SIZE && <div className="pagination">
      <button className="btn btn-secondary" disabled={loading || submitting || offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>上一页记录</button>
      <span>共 {total} 条记录</span>
      <button className="btn btn-secondary" disabled={loading || submitting || offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页记录</button>
    </div>}
    {error && <p role="alert">{error}</p>}
    <div className="run-launch">
      <button className="btn btn-primary" disabled={loading || submitting || !selected} onClick={() => void submit()}>
        {submitting ? '正在生成…' : mode === 'run-check' ? '生成检查报告' : '运行参考样本评测'}
      </button>
      <button className="btn btn-secondary" disabled={submitting || loading} onClick={() => setReload(reload + 1)}>刷新记录</button>
    </div>
  </section>;
}
