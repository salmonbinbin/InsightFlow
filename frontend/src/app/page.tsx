'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { useProjectContext } from '../contexts/ProjectContext';
import { getApiClient } from '../lib/api/client';
import { useProjectScopedFetch } from '../lib/useProjectScopedFetch';
import { EmptyState, ErrorState, LoadingState, NotMeasured } from '../components/StateViews';
import { formatNumber, formatRelative } from '../lib/format';
import type {
  FeedbackListResponse,
  Feedback,
  Insight,
} from '../lib/api/types';

interface OverviewState {
  feedbackTotal: number | null;
  feedbackRecent: Feedback[];
  insightCount: number | null;
  latestInsights: Insight[];
}

/**
 * Phase 7 work package A — overview.
 *
 * Only calls endpoints that exist in the backend today:
 *   - GET /projects/{id}/feedback    (paginated)
 *   - GET /projects/{id}/insights    (paginated)
 *
 * Things that would 404 if requested are explicitly NOT requested: there is
 * no "list analysis runs for project" and no "list opportunity scores for
 * project" endpoint in work package A scope. Anything that needs them is
 * shown as N/A with a forward-looking hint to the matching future page.
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
      return;
    }
    if (!signal) return;
    const mySignal = signal;
    const client = getApiClient();
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      const fbRes = await client.listFeedback(
        activeProjectId,
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
      const insightsRes = await client.listProjectInsights(
        activeProjectId,
        { limit: 25, offset: 0 },
        mySignal,
      );
      if (cancelled || mySignal.aborted) return;
      const insights = insightsRes.data?.items ?? [];
      setState({
        feedbackTotal: fb?.total ?? 0,
        feedbackRecent: fb?.items ?? [],
        insightCount: insights.length,
        latestInsights: insights.slice(0, 5),
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

  if (loading && !state) return <LoadingState />;
  if (error) return <ErrorState message={error} />;

  return (
    <div className="page page-overview">
      <header className="page-header">
        <span className="eyebrow">概览</span>
        <h1 className="page-title">{activeProject.name}</h1>
        <p className="page-subtitle">
          基于后端实时统计：反馈总量、最近入库与已识别洞察。
          缺失的指标以 N/A 显示，绝不替以估算。
        </p>
      </header>

      <section className="metric-grid">
        <MetricCard
          label="反馈总量"
          value={state?.feedbackTotal ?? null}
          hint={state?.feedbackTotal ? '条目数' : undefined}
        />
        <MetricCard
          label="最近一次分类运行"
          custom={<NotMeasured label="未接入（待 B 包）" />}
          hint="需 list-analysis-runs"
        />
        <MetricCard
          label="已识别洞察"
          value={state?.insightCount ?? null}
          hint="条"
        />
        <MetricCard
          label="机会评分"
          custom={<NotMeasured label="未接入（待 B 包）" />}
          hint="需 list-scores-for-project"
        />
      </section>

      <section className="panel">
        <header className="panel-header">
          <h2 className="section-title">最近反馈</h2>
          <Link className="btn btn-ghost btn-sm" href="/feedback">
            查看全部
          </Link>
        </header>
        {state && state.feedbackRecent.length === 0 ? (
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
            {(state?.feedbackRecent ?? []).map((f) => (
              <li key={f.id} className="recent-list-item">
                <div className="recent-list-id">{f.external_id}</div>
                <div className="recent-list-content">{f.content}</div>
                <div className="recent-list-meta">
                  <span>{formatRelative(f.created_at ?? null)}</span>
                  <span>{f.source || '—'}</span>
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
        {state && state.latestInsights.length === 0 ? (
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
            {(state?.latestInsights ?? []).map((i) => (
              <li key={i.id} className="opportunity-item">
                <div className="opportunity-item-head">
                  <span className="opportunity-item-title">{i.title}</span>
                </div>
                <div className="opportunity-item-meta">
                  状态：{i.status} · 反馈 {formatNumber(i.feedback_count)} · 证据{' '}
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
