'use client';

/**
 * InsightFlow — Requirements list (Phase 7 work package B2).
 *
 * Real backend contract: GET /api/projects/{project_id}/requirements
 *   ?limit&offset  → RequirementPage = { items, total, limit, offset }
 *
 * Mirrors the list page in feedback/page.tsx so pagination Loading /
 * Empty / Error / Retry semantics stay identical across pages. We do NOT
 * expose a search box here yet: the backend accepts only limit/offset +
 * the optional `insight_id` / `status` query params, which the rest of
 * the app surfaces via deep links from the Insight detail page.
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
import {
  classifyError,
  formatRelative,
} from '../../lib/format';
import type {
  Pagination,
  Requirement,
  RequirementStatus,
} from '../../lib/api/types';

const DEFAULT_PAGE: Pagination = { limit: 25, offset: 0 };

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  reviewing: '复核中',
  approved: '已批准',
  rejected: '已驳回',
  archived: '已归档',
};

const STATUS_BADGE: Record<string, string> = {
  draft: 'badge-outline',
  reviewing: 'badge-warn',
  approved: 'badge-success',
  rejected: 'badge-danger',
  archived: 'badge-muted',
};

export default function RequirementsListPage() {
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();

  const [page, setPage] = useState<Pagination>(DEFAULT_PAGE);
  const [data, setData] = useState<{
    items: Requirement[];
    total: number;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /* ---------- Reset on project change ---------- */

  useEffect(() => {
    setData(null);
    setError(null);
    setLoading(false);
    setPage(DEFAULT_PAGE);
  }, [activeProjectId]);

  /* ---------- Fetcher ---------- */

  const fetchPage = useCallback(async () => {
    if (!activeProjectId) return;
    if (!signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    setLoading(true);
    setError(null);
    const res = await client.listProjectRequirements(
      myProject,
      { limit: page.limit, offset: page.offset },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (res.error || !res.data) {
      setError(res.error ? classifyError(res.error) : '加载 Requirement 列表失败。');
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

  /* ---------- Derived ---------- */

  const pageCount = useMemo(() => {
    if (!data || data.total === 0) return 1;
    return Math.max(1, Math.ceil(data.total / page.limit));
  }, [data, page.limit]);

  const currentPage = Math.floor(page.offset / page.limit) + 1;

  /* ---------- Render ---------- */

  if (!activeProjectId) {
    return (
      <EmptyState
        title="请先选择项目"
        description="在右上角选择或创建一个项目，以查看对应的 Requirement 列表。"
      />
    );
  }

  if (loading && !data) return <LoadingState />;
  if (error && !data) {
    return <ErrorState message={error} onRetry={() => { setError(null); void fetchPage(); }} />;
  }

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const empty = !loading && items.length === 0;

  return (
    <div className="page page-requirements-list">
      <header className="page-header">
        <span className="eyebrow">Requirements · B2</span>
        <h1 className="page-title">需求列表</h1>
        <p className="page-subtitle">
          来自后端的真实 RequirementRead 数据（项目
          <code className="mono"> {activeProjectId.slice(0, 8)}…</code>
          ），状态在 PATCH 时变更；前端不会重算 AI snapshot。
        </p>
      </header>

      <section className="section" data-od-id="requirements-list">
        {loading && data ? <LoadingState /> : null}
        {empty ? (
          <EmptyState
            title="该项目下尚无 Requirement"
            description="请在 Insight 详情页完成 PM Decision 后，由 Create Requirement 创建。"
            action={
              <Link href="/insights" className="btn btn-secondary btn-sm">
                前往 Insight 列表
              </Link>
            }
          />
        ) : items.length > 0 ? (
          <ul className="requirement-list" data-testid="requirement-list">
            {items.map((req) => (
              <li key={req.id} className="requirement-row" data-testid="requirement-row">
                <div className="requirement-row-main">
                  <div className="requirement-row-head">
                    <Link
                      href={`/requirements/${req.id}`}
                      className="requirement-row-title"
                      data-testid="requirement-link"
                    >
                      {req.name || `Requirement ${req.id.slice(0, 8)}`}
                    </Link>
                    <span className={`badge ${STATUS_BADGE[req.status as RequirementStatus] ?? 'badge-outline'}`}>
                      {STATUS_LABEL[req.status as RequirementStatus] ?? req.status}
                    </span>
                    <span className="badge badge-accent">{req.pm_priority}</span>
                  </div>
                  <p className="requirement-row-meta">
                    来源 Insight <code className="mono">{req.insight_id.slice(0, 8)}</code> ·
                    {' '}Decision <code className="mono">{req.decision_id.slice(0, 8)}</code> ·
                    {' '}更新 <b className="mono">{formatRelative(req.updated_at ?? null)}</b>
                  </p>
                  <p className="requirement-row-snippet">
                    {req.problem || req.user_story || '（无问题摘要）'}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        ) : null}

        {items.length > 0 && total > page.limit ? (
          <footer className="pagination">
            <span className="pagination-meta">
              共 {total} 条 Requirement · 第 {currentPage} / {pageCount} 页
            </span>
            <div className="pagination-controls">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={currentPage <= 1}
                onClick={() =>
                  setPage((p) => ({
                    ...p,
                    offset: Math.max(0, p.offset - p.limit),
                  }))
                }
              >
                上一页
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={currentPage >= pageCount}
                onClick={() =>
                  setPage((p) => ({
                    ...p,
                    offset: p.offset + p.limit,
                  }))
                }
              >
                下一页
              </button>
            </div>
          </footer>
        ) : null}
      </section>
    </div>
  );
}
