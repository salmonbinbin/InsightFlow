'use client';

/**
 * InsightFlow — Insights list page (Phase 7 work package B1).
 *
 * Data contract: the only authoritative endpoint today is
 *   GET /projects/{id}/insights
 * which returns a paginated insight list (defaults to the latest
 * InsightRun for the project; pass ?run_id to pin a specific run).
 *
 * No backend endpoint today lists opportunity scores or decisions for
 * many insights at once; the page deliberately does NOT issue N+1
 * fetches. Opportunity Score / PM Decision live on the detail page
 * (/insights/[id]) where the route-layer endpoint accepts an insight
 * id and returns the snapshot directly.
 *
 * The filter chips and sort dropdowns in the prototype operate on the
 * currently-loaded page only (the Feedback page does the same). A
 * banner makes that explicit so the user is never misled into
 * believing a selection spans the full dataset.
 */

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useProjectContext } from '../../contexts/ProjectContext';
import { getApiClient } from '../../lib/api/client';
import { useProjectScopedFetch } from '../../lib/useProjectScopedFetch';
import {
  EmptyState,
  ErrorState,
  LoadingState,
} from '../../components/StateViews';
import { classifyError, formatNumber, formatRelative } from '../../lib/format';
import type { Insight, InsightStatus } from '../../lib/api/types';

interface PageQuery {
  limit: number;
  offset: number;
}

interface LocalView {
  statusFilter: '' | InsightStatus;
  search: string;
}

const DEFAULT_PAGE: PageQuery = { limit: 25, offset: 0 };
const DEFAULT_VIEW: LocalView = { statusFilter: '', search: '' };

export default function InsightsListPage() {
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();

  const [page, setPage] = useState<PageQuery>(DEFAULT_PAGE);
  const [view, setView] = useState<LocalView>(DEFAULT_VIEW);
  const [data, setData] = useState<{ items: Insight[]; total: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPage = useCallback(async () => {
    if (!activeProjectId) return;
    if (!signal) return;
    const mySignal = signal;
    setLoading(true);
    setError(null);
    const res = await client.listProjectInsights(
      activeProjectId,
      { limit: page.limit, offset: page.offset },
      mySignal,
    );
    // useProjectScopedFetch aborts the controller on project switch; a
    // late-arriving response from the previous project must NOT overwrite
    // the new project's view.
    if (mySignal.aborted) return;
    if (res.error || !res.data) {
      setError(res.error ? classifyError(res.error) : '加载失败。');
      setData(null);
      setLoading(false);
      return;
    }
    setData({ items: res.data.items, total: res.data.total });
    setLoading(false);
  }, [activeProjectId, client, page.limit, page.offset, signal]);

  useEffect(() => {
    void fetchPage();
  }, [fetchPage]);

  const totalPages = useMemo(() => {
    if (!data) return 1;
    return Math.max(1, Math.ceil(data.total / page.limit));
  }, [data, page.limit]);

  const currentPage = Math.floor(page.offset / page.limit) + 1;

  const displayedItems = useMemo(() => {
    if (!data) return [];
    let rows = data.items;
    if (view.statusFilter) {
      rows = rows.filter((i) => i.status === view.statusFilter);
    }
    if (view.search.trim()) {
      const needle = view.search.trim().toLowerCase();
      rows = rows.filter(
        (i) =>
          (i.title ?? '').toLowerCase().includes(needle) ||
          (i.pain_point ?? '').toLowerCase().includes(needle),
      );
    }
    return rows;
  }, [data, view.statusFilter, view.search]);

  if (!activeProjectId) {
    return <EmptyState title="请先选择项目" description="在右上角选择或创建一个项目。" />;
  }

  return (
    <div className="page page-insights">
      <header className="page-header">
        <span className="eyebrow">洞察列表</span>
        <h1 className="page-title">Product Insights</h1>
        <p className="page-subtitle">
          AI 生成的洞察。来自
          <code className="mono">GET /projects/&#123;id&#125;/insights</code> 的真实分页数据；
          每条都附原始证据，可在详情页追溯。
        </p>
      </header>

      <section className="toolbar">
        <label className="form-field form-field--inline">
          <span className="form-label">搜索（仅当前页）</span>
          <input
            type="search"
            className="form-input"
            placeholder="按标题或痛点关键字"
            value={view.search}
            onChange={(e) => setView((v) => ({ ...v, search: e.target.value }))}
          />
        </label>
        <label className="form-field form-field--inline">
          <span className="form-label">状态（仅当前页）</span>
          <select
            className="form-select"
            value={view.statusFilter}
            onChange={(e) =>
              setView((v) => ({
                ...v,
                statusFilter: e.target.value as LocalView['statusFilter'],
              }))
            }
          >
            <option value="">全部</option>
            <option value="draft">Draft</option>
            <option value="needs_validation">Needs Validation</option>
          </select>
        </label>
        <label className="form-field form-field--inline">
          <span className="form-label">每页</span>
          <select
            className="form-select"
            value={page.limit}
            onChange={(e) =>
              setPage((p) => ({ offset: 0, limit: Math.max(1, Number(e.target.value)) }))
            }
          >
            <option value={10}>10</option>
            <option value={25}>25</option>
            <option value={50}>50</option>
          </select>
        </label>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={() => void fetchPage()}
          disabled={loading}
        >
          刷新
        </button>
      </section>

      <div className="alert alert-info" role="status">
        后端的 Insights 列表端点不接受 status / sort / priority 等筛选参数；
        上方筛选仅作用于当前页（与 Feedback 页同理）。Opportunity Score / PM Decision
        详情请到「详情页」查看。
      </div>

      {loading && !data ? (
        <LoadingState />
      ) : error ? (
        <ErrorState
          message={error}
          onRetry={() => {
            setError(null);
            void fetchPage();
          }}
        />
      ) : data && data.items.length === 0 ? (
        <EmptyState
          title="该项目暂无 Insight"
          description="先到「分析」页签完成 Classification → Clustering → Insight Generate 三阶段。"
          action={
            <Link className="btn btn-primary btn-sm" href="/analysis">
              前往分析
            </Link>
          }
        />
      ) : data ? (
        <>
          <div className="insight-grid">
            {displayedItems.length === 0 ? (
              <EmptyState
                title="当前页内无匹配"
                description={
                  view.search ? `搜索 “${view.search}” / ${view.statusFilter || '全部状态'} 未命中`
                    : `状态 = ${view.statusFilter || '全部'} 当前页内无匹配`
                }
              />
            ) : (
              displayedItems.map((ins) => <InsightCardItem key={ins.id} insight={ins} />)
            )}
          </div>
          <footer className="pagination">
            <span className="pagination-meta">
              共 {data.total} 条 · 第 {currentPage} / {totalPages} 页 · 当前页显示
              {' '}
              {displayedItems.length} / {data.items.length} 条
            </span>
            <div className="pagination-controls">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={currentPage <= 1}
                onClick={() =>
                  setPage((p) => ({ ...p, offset: Math.max(0, p.offset - p.limit) }))
                }
              >
                上一页
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={currentPage >= totalPages}
                onClick={() => setPage((p) => ({ ...p, offset: p.offset + p.limit }))}
              >
                下一页
              </button>
            </div>
          </footer>
        </>
      ) : null}
    </div>
  );
}

interface InsightCardProps {
  insight: Insight;
}

function InsightCardItem({ insight }: InsightCardProps) {
  // Status normalisation: the backend reports ``draft`` or ``needs_validation``
  // for raw insight rows; the prototype's "Pending Review / Accepted / ..."
  // status is decided downstream. We deliberately do NOT synthesise a PM
  // decision status here — the user must open the detail page to act.
  const hasAiBody = insight.title !== null && insight.pain_point !== null;
  return (
    <article className="insight-card" data-testid={`insight-card-${insight.id}`}>
      <div className="body">
        <div className="head">
          <span
            className={`badge badge-${
              insight.status === 'needs_validation' ? 'warn' : 'outline'
            }`}
          >
            {insight.status === 'needs_validation' ? 'Needs Validation' : 'Draft'}
          </span>
          <span className="muted mono" style={{ fontSize: 'var(--t-caption)' }}>
            {insight.id.slice(0, 8)}
          </span>
          <span className="badge badge-outline mono">
            cluster {insight.source_cluster_external_id.slice(0, 8)}
          </span>
          <span className="badge badge-ai">AI-generated</span>
        </div>
        <h3>{insight.title ?? '尚无标题'}</h3>
        <p className="summary">
          {insight.pain_point ?? '痛点尚未生成（needs_validation）。请等待重试或扩充证据。'}
        </p>
        <div className="stats">
          <span>
            反馈 <b>{formatNumber(insight.feedback_count)}</b>
          </span>
          <span>
            证据 <b>{formatNumber(insight.evidence_count)}</b>
          </span>
          <span>
            置信度 <b>{insight.confidence !== null ? formatNumber(insight.confidence) : '—'}</b>
          </span>
          <span>
            状态 <b>{insight.status}</b>
          </span>
        </div>
        <div className="actions">
          <span className="muted" style={{ fontSize: 'var(--t-caption)' }}>
            更新 {formatRelative(insight.created_at ?? null)} ·
            模型 <span className="mono">{insight.model ?? '—'}</span>
          </span>
          <Link
            className="btn btn-primary btn-sm"
            href={`/insights/${insight.id}`}
            aria-disabled={hasAiBody ? undefined : 'true'}
          >
            查看详情 →
          </Link>
        </div>
      </div>
    </article>
  );
}
