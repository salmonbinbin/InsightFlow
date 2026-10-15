'use client';

/**
 * InsightFlow — Project PRD list (Phase 7 work package C1).
 *
 * Real backend contract: GET /api/projects/{project_id}/prds
 *   ?limit&offset  → ProjectPRDPage = { items, total, limit, offset }
 *
 * Returns EVERY PRD version across the project (no latest-per-
 * requirement projection), ordered ``version DESC, id DESC``. The
 * page groups by ``requirement_id`` to render a version timeline;
 * collapsing to one row per requirement would silently drop earlier
 * drafts and accepted versions, breaking the audit trail.
 *
 * The page must NOT treat the current slice as the entire history:
 *   - If ``offset > 0`` we still show ``total`` in the meta line
 *     so the user knows there are more rows behind us.
 *   - The empty state is reserved for ``total === 0`` only; a
 *     non-empty project that just happens to have no rows on the
 *     current page shows the pagination footer with "上一页" still
 *     usable.
 *
 * Project switch resets every piece of state (page, data, error)
 * via the ``useEffect`` keyed on ``activeProjectId``; cross-
 * project data never bleeds across. ``useProjectScopedFetch``'s
 * signal aborts the in-flight request on switch so a stale PRD
 * page cannot write to the new project's view.
 *
 * This package is LIST ONLY. The /prds/{id} workspace lives in a
 * later work package; this page renders each row with its version
 * + status + parent requirement pointer but does NOT link into a
 * detail route.
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
  PrdRead,
  PrdStatus,
} from '../../lib/api/types';

const DEFAULT_PAGE: Pagination = { limit: 25, offset: 0 };

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  reviewing: '复核中',
  accepted: '已采纳',
  archived: '已归档',
};

const STATUS_BADGE: Record<string, string> = {
  draft: 'badge-outline',
  reviewing: 'badge-warn',
  accepted: 'badge-success',
  archived: 'badge-muted',
};

export default function PrdsListPage() {
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();

  const [page, setPage] = useState<Pagination>(DEFAULT_PAGE);
  const [data, setData] = useState<{
    items: PrdRead[];
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
    const res = await client.listProjectPrds(
      myProject,
      { limit: page.limit, offset: page.offset },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setError(res.error ? classifyError(res.error) : '加载 PRD 列表失败。');
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
        description="在右上角选择或创建一个项目，以查看对应 PRD 列表。"
      />
    );
  }

  if (loading && !data) return <LoadingState />;
  if (error && !data) {
    return (
      <ErrorState
        message={error}
        onRetry={() => { setError(null); void fetchPage(); }}
      />
    );
  }

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const empty = !loading && total === 0;

  return (
    <div className="page page-prds-list">
      <header className="page-header">
        <span className="eyebrow">PRD · C1</span>
        <h1 className="page-title">PRD 列表</h1>
        <p className="page-subtitle">
          来自后端的真实 PRDRead 数据（项目
          <code className="mono"> {activeProjectId.slice(0, 8)}…</code>
          ），按 <code className="mono">version DESC, id DESC</code> 稳定排序；同一 Requirement 的多个版本都保留，不折叠为最新一行。
        </p>
      </header>

      <section className="section" data-od-id="prds-list">
        {loading && data ? <LoadingState /> : null}
        {empty ? (
          <EmptyState
            title="该项目下尚无 PRD"
            description="先在需求详情页生成 PRD；该项目所有版本会在此显示。"
            action={
              <Link href="/requirements" className="btn btn-secondary btn-sm">
                前往需求列表
              </Link>
            }
          />
        ) : items.length > 0 ? (
          <ul className="prd-list" data-testid="prd-list">
            {items.map((prd) => (
              <li
                key={prd.id}
                className="prd-row"
                data-testid="prd-row"
                data-prd-id={prd.id}
              >
                <div className="prd-row-main">
                  <div className="prd-row-head">
                    <Link
                      href={`/prds/${prd.id}`}
                      className="prd-row-version"
                      data-testid="prd-row-link"
                      title={`打开 v${prd.version} 详情`}
                    >
                      v{prd.version}
                    </Link>
                    <span
                      className={`badge ${STATUS_BADGE[prd.status as PrdStatus] ?? 'badge-outline'}`}
                      data-testid="prd-status-badge"
                    >
                      {STATUS_LABEL[prd.status as PrdStatus] ?? prd.status}
                    </span>
                    {prd.is_ai_generated_draft ? (
                      <span
                        className="badge badge-accent"
                        data-testid="prd-ai-draft"
                      >
                        AI 草稿
                      </span>
                    ) : null}
                  </div>
                  <p className="prd-row-meta">
                    关联 Requirement{' '}
                    <Link
                      href={`/requirements/${prd.requirement_id}`}
                      className="mono"
                      data-testid="prd-requirement-link"
                    >
                      {prd.requirement_id.slice(0, 8)}
                    </Link>
                    {' '}· 项目 <code className="mono">{prd.project_id.slice(0, 8)}</code>
                    {' '}· 更新 <b className="mono">{formatRelative(prd.updated_at ?? null)}</b>
                  </p>
                  <p className="prd-row-snippet">
                    来源 Insight{' '}
                    <code className="mono">{prd.insight_id.slice(0, 8)}</code>
                    {' · 模型 '}
                    <code className="mono">{prd.model ?? '—'}</code>
                    {' · prompt '}
                    <code className="mono">{prd.prompt_version}</code>
                  </p>
                </div>
                <div className="prd-row-actions">
                  <Link
                    href={`/prds/${prd.id}`}
                    className="btn btn-ghost btn-sm"
                    data-testid="prd-open-detail"
                  >
                    详情
                  </Link>
                </div>
              </li>
            ))}
          </ul>
        ) : null}

        {items.length > 0 && total > page.limit ? (
          <footer className="pagination">
            <span className="pagination-meta">
              共 {total} 条 PRD · 第 {currentPage} / {pageCount} 页
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

        {!empty && total > 0 && items.length > 0 && total <= page.limit ? (
          <footer className="pagination">
            <span className="pagination-meta">
              共 {total} 条 PRD · 单页全量
            </span>
          </footer>
        ) : null}
      </section>
    </div>
  );
}
