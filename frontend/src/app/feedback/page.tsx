'use client';

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { useProjectContext } from '../../contexts/ProjectContext';
import { getApiClient } from '../../lib/api/client';
import { useProjectScopedFetch } from '../../lib/useProjectScopedFetch';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  NotMeasured,
} from '../../components/StateViews';
import { FeedbackDrawer } from '../../components/FeedbackDrawer';
import {
  classifyError,
  formatDateTime,
  formatRelative,
  truncate,
} from '../../lib/format';
import type {
  AnalysisRunId,
  Feedback,
  FeedbackAnalysisEnvelope,
  FeedbackAnalysisRead,
  FeedbackAnalysisSelection,
} from '../../lib/api/types';

interface PageQuery {
  limit: number;
  offset: number;
}

interface LocalView {
  search: string;
  sort: 'server' | 'external_id_asc' | 'external_id_desc';
}

interface DrawerState {
  analysis: FeedbackAnalysisRead | null;
  selection: FeedbackAnalysisSelection | null;
}

const DEFAULT_PAGE: PageQuery = { limit: 25, offset: 0 };
const DEFAULT_VIEW: LocalView = { search: '', sort: 'server' };
const EMPTY_DRAWER: DrawerState = { analysis: null, selection: null };

/**
 * Phase 7 work package A — feedback list.
 *
 * Contract note: the backend's GET /projects/{id}/feedback only honours
 * `limit` and `offset`. The search box, sort dropdown and per-row filtering
 * here therefore operate on the **currently loaded page** only. We surface
 * that fact explicitly in a banner above the table so the user is never
 * misled into thinking their search spans the full dataset.
 *
 * Phase 7 E (Feedback Detail) — opening a feedback row triggers a
 * dedicated request to
 * ``GET /api/projects/{project_id}/feedback/{feedback_id}/analysis``
 * (or its ``?run_id=`` variant) so the AI analysis shown in the
 * drawer is always sourced from a real backend round-trip. The
 * response contract is verified against the row the user opened
 * (``envelope.feedback_id`` AND ``analysis.feedback_id`` when
 * present must both match) and a foreign / stale response is
 * discarded rather than rendered.
 *
 * Race protection: every analysis fetch captures the project
 * scoped ``signal`` from ``useProjectScopedFetch()`` and re-checks
 * ``mySignal.aborted`` and the active project / feedback id
 * after every await. Project switch wipes drawer state so a
 * stale foreign response cannot bleed across tenants.
 */
export default function FeedbackListPage() {
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();

  const [page, setPage] = useState<PageQuery>(DEFAULT_PAGE);
  const [view, setView] = useState<LocalView>(DEFAULT_VIEW);
  const [data, setData] = useState<{ items: Feedback[]; total: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Feedback | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawer, setDrawer] = useState<DrawerState>(EMPTY_DRAWER);
  const [drawerLoading, setDrawerLoading] = useState(false);
  const [drawerError, setDrawerError] = useState<string | null>(null);
  /* The element that triggered the open. Restored to focus on
   * close so keyboard users land back where they were. */
  const lastTriggerRef = useRef<HTMLElement | null>(null);
  /* Cached run_id to pass to the explicit branch — currently
   * always null (the page does not expose a "show this run"
   * picker yet), but the wire shape supports it. */
  const explicitRunIdRef = useRef<AnalysisRunId | null>(null);
  /* Mirror of the currently open feedback id so the fetcher
   * (whose closure has no live read of ``selectedId``) can
   * tell, AFTER the await, whether the user has switched to
   * a different row. Without this mirror a slow A response
   * could overwrite a B drawer. */
  const selectedIdRef = useRef<string | null>(null);
  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  const fetchPage = useCallback(async () => {
    if (!activeProjectId) return;
    if (!signal) return;
    const mySignal = signal;
    setLoading(true);
    setError(null);
    const res = await client.listFeedback(
      activeProjectId,
      { limit: page.limit, offset: page.offset },
      mySignal,
    );
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

  /* Phase 7 E — per-feedback analysis fetch. Stable across the
   * render cycle so the project-switch reset effect can call it
   * without churn; the only deps that should ever re-create it
   * are the active project, the client, and the project scoped
   * signal. */
  const fetchAnalysis = useCallback(
    async (feedbackId: string, runId: AnalysisRunId | null) => {
      if (!activeProjectId || !signal) return;
      const mySignal = signal;
      const myProject = activeProjectId;
      const myFeedbackId = feedbackId;
      setDrawerLoading(true);
      setDrawerError(null);
      setDrawer(EMPTY_DRAWER);
      const res = await client.getFeedbackAnalysis(
        myProject,
        myFeedbackId,
        runId ? { run_id: runId } : {},
        mySignal,
      );
      if (mySignal.aborted) return;
      if (myProject !== activeProjectId) return;
      /* The user may have moved to a different row while this
       * fetch was in flight. The ref captures the latest
       * ``selectedId`` (it is updated synchronously in
       * ``openDrawer``) so we can drop the response rather
       * than let a stale foreign row backfill the new drawer. */
      if (selectedIdRef.current !== myFeedbackId) return;
      if (res.error || !res.data) {
        setDrawerLoading(false);
        setDrawerError(res.error ? classifyError(res.error) : '加载分析失败。');
        return;
      }
      const env: FeedbackAnalysisEnvelope = res.data;
      /* Contract check: the response must be about the
       * feedback the user just opened, AND any nested
       * analysis row must reference the same feedback. A
       * mismatch means the backend / mock has a bug or
       * someone tampered with the URL — we refuse to
       * render rather than show a foreign row. */
      if (env.feedback_id !== myFeedbackId) {
        setDrawerLoading(false);
        setDrawerError('响应与当前反馈不匹配，已拒绝显示。');
        return;
      }
      if (env.analysis && env.analysis.feedback_id !== myFeedbackId) {
        setDrawerLoading(false);
        setDrawerError('响应分析行不属于当前反馈，已拒绝显示。');
        return;
      }
      setDrawer({ analysis: env.analysis, selection: env.selection });
      setDrawerLoading(false);
    },
    [activeProjectId, client, signal],
  );

  /* Project switch reset: wipe every piece of drawer state
   * (selection + analysis + error + trigger) so a stale
   * foreign response can never render on a different project.
   * The signal abort from useProjectScopedFetch is the
   * primary guard; this is the belt-and-suspenders cleanup
   * for the rare case where a fetch is committed between
   * the abort firing and a new one starting. */
  useEffect(() => {
    setSelectedId(null);
    selectedIdRef.current = null;
    setSelected(null);
    setDrawerOpen(false);
    setDrawer(EMPTY_DRAWER);
    setDrawerError(null);
    setDrawerLoading(false);
    lastTriggerRef.current = null;
    explicitRunIdRef.current = null;
  }, [activeProjectId]);

  const totalPages = useMemo(() => {
    if (!data) return 1;
    return Math.max(1, Math.ceil(data.total / page.limit));
  }, [data, page.limit]);

  const currentPage = Math.floor(page.offset / page.limit) + 1;

  const displayedItems = useMemo(() => {
    if (!data) return [];
    let rows = data.items;
    if (view.search.trim()) {
      const needle = view.search.trim().toLowerCase();
      rows = rows.filter(
        (f) =>
          f.content.toLowerCase().includes(needle) ||
          f.external_id.toLowerCase().includes(needle),
      );
    }
    if (view.sort === 'external_id_asc') {
      rows = [...rows].sort((a, b) => a.external_id.localeCompare(b.external_id));
    } else if (view.sort === 'external_id_desc') {
      rows = [...rows].sort((a, b) => b.external_id.localeCompare(a.external_id));
    }
    return rows;
  }, [data, view.search, view.sort]);

  const openDrawer = useCallback(
    (fb: Feedback, trigger?: HTMLElement | null) => {
      setSelectedId(fb.id);
      /* Sync update the ref BEFORE the await kicks off so a
       * late-returning fetch for the previous row can compare
       * to the freshly opened id and refuse to backfill. */
      selectedIdRef.current = fb.id;
      setSelected(fb);
      setDrawerOpen(true);
      setDrawer(EMPTY_DRAWER);
      setDrawerError(null);
      setDrawerLoading(true);
      lastTriggerRef.current = trigger ?? null;
      void fetchAnalysis(fb.id, explicitRunIdRef.current);
    },
    [fetchAnalysis],
  );

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    const trigger = lastTriggerRef.current;
    lastTriggerRef.current = null;
    /* Defer focus to the next tick so the drawer's own
     * unmount does not steal focus back to <body>. */
    if (trigger && typeof trigger.focus === 'function') {
      setTimeout(() => trigger.focus(), 0);
    }
  }, []);

  const handleRowKey = useCallback(
    (e: ReactKeyboardEvent<HTMLTableRowElement>, fb: Feedback) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openDrawer(fb, e.currentTarget);
      }
    },
    [openDrawer],
  );

  if (!activeProjectId) {
    return <EmptyState title="请先选择项目" description="在右上角选择或创建一个项目。" />;
  }

  return (
    <div className="page page-feedback">
      <header className="page-header">
        <span className="eyebrow">反馈列表</span>
        <h1 className="page-title">原始反馈数据</h1>
        <p className="page-subtitle">
          来自后端 /projects/&#123;id&#125;/feedback 的真实分页数据。
        </p>
      </header>

      <section className="toolbar">
        <label className="form-field form-field--inline">
          <span className="form-label">搜索（仅当前页）</span>
          <input
            type="search"
            className="form-input"
            placeholder="按反馈 ID 或内容关键字"
            value={view.search}
            onChange={(e) => setView((v) => ({ ...v, search: e.target.value }))}
          />
        </label>
        <label className="form-field form-field--inline">
          <span className="form-label">排序（仅当前页）</span>
          <select
            className="form-select"
            value={view.sort}
            onChange={(e) =>
              setView((v) => ({ ...v, sort: e.target.value as LocalView['sort'] }))
            }
          >
            <option value="server">后端默认（按原始时间）</option>
            <option value="external_id_asc">反馈 ID A→Z</option>
            <option value="external_id_desc">反馈 ID Z→A</option>
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
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
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
        后端当前只支持按页加载（limit / offset），搜索与排序只对当前页生效；切页可看到完整数据。
        跨页/全量搜索将在工作包 B 接入。
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
          title="该项目中没有反馈"
          description="先到「上传 CSV」页签导入原始反馈。"
        />
      ) : data ? (
        <>
          <div className="data-table-wrapper">
            <table className="data-table" role="table">
              <thead>
                <tr>
                  <th scope="col" className="col-id">反馈 ID</th>
                  <th scope="col">内容</th>
                  <th scope="col">来源</th>
                  <th scope="col">用户</th>
                  <th scope="col" className="col-time">原始时间</th>
                  <th scope="col" className="col-time">入库时间</th>
                </tr>
              </thead>
              <tbody>
                {displayedItems.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="data-table-empty">
                      当前页内无匹配记录（搜索 {view.search ? `“${view.search}”` : ''}）。
                    </td>
                  </tr>
                ) : (
                  displayedItems.map((fb) => (
                    <tr
                      key={fb.id}
                      className={`data-table-row${selectedId === fb.id ? ' is-selected' : ''}`}
                      onClick={(e) => openDrawer(fb, e.currentTarget)}
                      onKeyDown={(e) => handleRowKey(e, fb)}
                      tabIndex={0}
                      role="button"
                      aria-label={`打开反馈 ${fb.external_id} 的详情`}
                    >
                      <td className="col-id">{fb.external_id}</td>
                      <td className="col-content">{truncate(fb.content, 240)}</td>
                      <td>{fb.source || <NotMeasured />}</td>
                      <td>{fb.user_id || <NotMeasured />}</td>
                      <td className="col-time">{formatRelative(fb.feedback_created_at ?? null)}</td>
                      <td className="col-time">{formatDateTime(fb.created_at ?? null)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <footer className="pagination">
            <span className="pagination-meta">
              共 {data.total} 条 · 第 {currentPage} / {totalPages} 页 ·
              当前页显示 {displayedItems.length} / {data.items.length} 条
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

      <FeedbackDrawer
        open={drawerOpen}
        onClose={closeDrawer}
        feedback={selected}
        loading={drawerLoading}
        analysis={drawer.analysis}
        selection={drawer.selection}
        error={drawerError}
        onRetry={() => {
          if (selectedId) void fetchAnalysis(selectedId, explicitRunIdRef.current);
        }}
      />
    </div>
  );
}
