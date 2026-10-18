'use client';

import Link from 'next/link';
import { displayLabel, sourceLabel } from '../lib/presentation';
import { useEffect, useState } from 'react';
import { useProjectContext } from '../contexts/ProjectContext';
import { getApiClient } from '../lib/api/client';
import { useProjectScopedFetch } from '../lib/useProjectScopedFetch';
import { EmptyState, ErrorState, LoadingState, NotMeasured, StatusPill } from '../components/StateViews';
import { formatNumber, formatRelative } from '../lib/format';
import type {
  FeedbackListResponse,
  Feedback,
  Insight,
  AnalysisRun,
  ProjectId,
} from '../lib/api/types';

interface OverviewState {
  /**
   * Project this snapshot belongs to. The render layer MUST gate
   * on ``state.projectId === activeProjectId`` before showing
   * ``state``; otherwise a slow re-fetch after a project switch
   * flashes the previous project's data under the new project's
   * title. The check is the contract that backs the
   * ``setState(null)`` reset below.
   */
  projectId: ProjectId;
  feedbackTotal: number | null;
  feedbackRecent: Feedback[];
  /**
   * ``insightCount`` mirrors the paginated response ``total`` field, not
   * ``items.length`` — a project with >25 insights must report the real
   * backend count, not "25 because that is our page size".
   * ``null`` means the request has not finished or failed; rendering
   * falls back to NotMeasured so a transport error never masquerades
   * as "0 已识别洞察".
   */
  insightCount: number | null;
  insightsError: string | null;
  latestInsights: Insight[];
  /**
   * ``latestClassificationRun`` is the newest
   * ``GET /projects/{id}/analysis-runs?run_type=classification`` row,
   * or ``null`` when the project has never run a classification. A
   * load failure also collapses to ``null`` and is reflected in
   * ``analysisRunsError`` so the UI can distinguish "no run yet" from
   * "could not load run history".
   */
  latestClassificationRun: AnalysisRun | null;
  analysisRunsError: string | null;
}

/**
 * Phase 7 work package A — overview.
 *
 * Calls project-scoped endpoints that exist in the backend today:
 *   - GET /projects/{id}/feedback                       (paginated; total)
 *   - GET /projects/{id}/insights                       (paginated; total)
 *   - GET /projects/{id}/analysis-runs?run_type=classification
 *                                                  (paginated; latest one)
 *
 * The "opportunity score" tile has no project-level aggregation
 * endpoint in the current backend — each insight owns its own score
 * via ``GET /insights/{id}/opportunity-score``. The card is rendered
 * as a factual "no project-level summary available" hint that links
 * to the per-insight detail surface, not as a missing-data stub and
 * not as a "待 B 包" promise.
 */
export default function OverviewPage() {
  const { activeProject, activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const [state, setState] = useState<OverviewState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!activeProjectId) {
      setState(null);
      setError(null);
      setLoading(false);
      return;
    }
    if (!signal) return;
    const mySignal = signal;
    // Capture the project id at effect-entry. Even if ``activeProjectId``
    // changes mid-flight, the IIFE's early-return guards (``cancelled``
    // and ``mySignal.aborted``) prevent the stale closure from writing
    // anything; the new effect for the new project will setState under
    // its own captured id.
    const myProjectId = activeProjectId;
    const client = getApiClient();
    let cancelled = false;
    setLoading(true);
    setError(null);
    // Drop any previous project's snapshot BEFORE the new fetch is
    // even issued. Without this reset, render #N+1 (project just
    // switched, fetches in flight) would still see the old ``state``
    // object in memory and flash it under the new project's title.
    // The render-layer ``state.projectId === activeProjectId`` gate
    // is a defense-in-depth, but resetting here is what actually
    // keeps the user's screen honest.
    setState(null);
    void (async () => {
      const fbRes = await client.listFeedback(
        myProjectId,
        { limit: 5, offset: 0 },
        mySignal,
      );
      if (cancelled || mySignal.aborted) return;
      if (fbRes.error) {
        setError(fbRes.error.message);
        setLoading(false);
        return;
      }
      const fb = fbRes.data as FeedbackListResponse | null;

      // Each non-fatal dependency is captured independently so a
      // transport failure on /insights or /analysis-runs never
      // silently downgrades to "0" or "no run yet". Only the
      // blocking feedback fetch raises a page-level error.
      const insightsRes = await client.listProjectInsights(
        myProjectId,
        { limit: 25, offset: 0 },
        mySignal,
      );
      if (cancelled || mySignal.aborted) return;
      const insightsError = insightsRes.error ? insightsRes.error.message : null;
      const insightsTotal = insightsRes.data ? insightsRes.data.total : null;
      const insightsItems = insightsRes.data?.items ?? [];

      const runsRes = await client.listProjectAnalysisRuns(
        myProjectId,
        { run_type: 'classification', limit: 1, offset: 0 },
        mySignal,
      );
      if (cancelled || mySignal.aborted) return;
      const analysisRunsError = runsRes.error ? runsRes.error.message : null;
      const latestClassificationRun =
        !runsRes.error && runsRes.data && runsRes.data.items.length > 0
          ? (runsRes.data.items[0] ?? null)
          : null;

      setState({
        projectId: myProjectId,
        feedbackTotal: fb?.total ?? 0,
        feedbackRecent: fb?.items ?? [],
        insightCount: insightsTotal,
        insightsError,
        latestInsights: insightsItems.slice(0, 5),
        latestClassificationRun,
        analysisRunsError,
      });
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [activeProjectId, signal]);

  if (!activeProject) {
    return (
      <EmptyState
        title="尚未选择项目"
        description="点击右上角“当前项目”按钮创建一个项目或选择一个已有项目。"
        action={
          <Link className="btn btn-primary btn-sm" href="/upload">
            立即导入反馈
          </Link>
        }
      />
    );
  }

  // Visible state is strictly the snapshot whose projectId matches
  // the current active project. Without this gate, the
  // ``loading && !state`` check below would let a previous project's
  // snapshot slip through during the brief window before the new
  // project's setState(null) flushes a re-render, flashing old data
  // under the new project's title. This gate is the single source
  // of truth for "is the snapshot still mine to show?".
  const visible = state && state.projectId === activeProjectId ? state : null;

  if (loading && !visible) return <LoadingState />;
  if (error) return <ErrorState message={error} />;
  // Safety net: if no usable snapshot is available for the current
  // project (effect reset is in flight, or projectId mismatch),
  // refuse to render any snapshot rather than risk showing another
  // project's data.
  if (!visible) return <LoadingState />;

  return (
    <div className="page page-overview">
      <header className="page-header">
        <span className="eyebrow">概览</span>
        <h1 className="page-title">{activeProject.name}</h1>

      </header>

      <section className="metric-grid">
        <MetricCard
          label="反馈总量"
          value={visible.feedbackTotal}
          hint={visible.feedbackTotal ? '条目数' : undefined}
        />
        <MetricCard
          label="最近一次分类运行"
          custom={
            visible.latestClassificationRun ? (
              <LatestClassificationRunView run={visible.latestClassificationRun} />
            ) : visible.analysisRunsError ? (
              <NotMeasured label="加载失败" />
            ) : (
              <NotMeasured label="尚未运行" />
            )
          }
          hint={
            visible.analysisRunsError
              ? visible.analysisRunsError
              : visible.latestClassificationRun
              ? '最近一次反馈分类结果'
              : '在「分析」页运行分类后会显示'
          }
        />
        <MetricCard
          label="已识别洞察"
          value={visible.insightCount}
          hint={
            visible.insightsError
              ? `加载失败：${visible.insightsError}`
              : '当前项目的洞察总数'
          }
        />
        <MetricCard
          label="机会评分"
          custom={
            <Link className="metric-card-link" href="/insights">
              查看机会评分
            </Link>
          }
          hint="进入洞察查看各项机会评分"
        />
      </section>

      <section className="panel">
        <header className="panel-header">
          <h2 className="section-title">最近反馈</h2>
          <Link className="btn btn-ghost btn-sm" href="/feedback">
            查看全部
          </Link>
        </header>
        {visible.feedbackRecent.length === 0 ? (
          <EmptyState
            title="尚无反馈"
            description="还没有导入任何反馈。"
            action={
              <Link className="btn btn-primary btn-sm" href="/upload">
                上传 CSV
              </Link>
            }
          />
        ) : (
          <ul className="recent-list">
            {visible.feedbackRecent.map((f) => (
              <li key={f.id} className="recent-list-item">
                <div className="recent-list-id">{f.external_id}</div>
                <div className="recent-list-content">{f.content}</div>
                <div className="recent-list-meta">
                  <span>{formatRelative(f.created_at ?? null)}</span>
                  <span>{f.source ? sourceLabel(f.source) : '—'}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel">
        <header className="panel-header">
          <h2 className="section-title">最近洞察</h2>
          <Link className="btn btn-ghost btn-sm" href="/analysis">
            管理分析
          </Link>
        </header>
        {visible.latestInsights.length === 0 ? (
          <EmptyState
            title="尚无洞察"
            description="请到分析页签运行「分类 → 聚类 → 洞察」流程。"
            action={
              <Link className="btn btn-primary btn-sm" href="/analysis">
                前往分析
              </Link>
            }
          />
        ) : (
          <ul className="opportunity-list">
            {visible.latestInsights.map((i) => (
              <li key={i.id} className="opportunity-item">
                <div className="opportunity-item-head">
                  <span className="opportunity-item-title">{i.title}</span>
                </div>
                <div className="opportunity-item-meta">
                  状态：{displayLabel(i.status)} · 反馈 {formatNumber(i.feedback_count)} · 证据{' '}
                  {formatNumber(i.evidence_count)} · 置信度 {formatNumber(i.confidence)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function LatestClassificationRunView({ run }: { run: AnalysisRun }) {
  // Prefer completed_at, fall back through started_at / created_at
  // so the displayed timestamp matches whatever the backend has
  // persisted for the chosen run.
  const ts = run.completed_at ?? run.started_at ?? run.created_at ?? null;
  return (
    <div className="metric-card-run">
      <div className="metric-card-run-row">
        <StatusPill status={run.status} />
        <span className="metric-card-run-time">{formatRelative(ts)}</span>
      </div>
      <div className="metric-card-run-row metric-card-run-counts">
        成功 {formatNumber(run.success_count)} / 输入 {formatNumber(run.input_count)}
      </div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  custom,
  hint,
}: {
  label: string;
  value?: number | null;
  custom?: React.ReactNode;
  hint?: string;
}) {
  return (
    <article className="metric-card">
      <div className="metric-card-label">{label}</div>
      <div className="metric-card-value">
        {custom ?? (value === null || value === undefined ? <NotMeasured /> : formatNumber(value))}
      </div>
      {hint ? <div className="metric-card-hint">{hint}</div> : null}
    </article>
  );
}
