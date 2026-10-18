'use client';

/**
 * InsightFlow — Evaluation page (Phase 7 work package D2b + F).
 *
 * Real backend contract (D2a typed accessors):
 *   GET /api/projects/{project_id}/evaluations?limit&offset
 *     → ProjectEvaluationRunPage = { items, total, limit, offset }
 *   GET /api/evaluations/{run_id}?project_id=…
 *     → EvaluationRunRead
 *   GET /api/evaluations/{run_id}/metrics?project_id=&limit=&offset=
 *     → EvaluationMetricPage
 *   GET /api/evaluations/{run_id}/cases?project_id=&limit=&offset=
 *     → EvaluationCasePage
 *
 * F package — metrics/cases pagination:
 *   The previous implementation hard-coded
 *   ``limit: 100, offset: 0`` and treated the response as the
 *   full dataset, silently dropping anything past 100. That
 *   truncation hid failure cases sitting on later pages, which
 *   is unacceptable for the failure-case dashboard. We now
 *   drive pagination off the backend's ``total / limit /
 *   offset`` envelope, render proper 上一页 / 下一页 controls,
 *   and surface a "本页 X / 共 Y" counter so the user knows
 *   how many cases exist beyond the current slice.
 *
 * Cross-project isolation (enforced in two layers):
 *   1. The list endpoint encodes the project in the path; the
 *      backend filters by project_id (mirrored by
 *      ``MockInsightFlowClient``).
 *   2. Per-run endpoints ALWAYS carry ``project_id`` in the
 *      query so a row owned by another project returns 404
 *      instead of 403.
 *   3. Defence in depth: the page validates
 *      ``every item.project_id === activeProjectId`` on the
 *      list / metrics / cases responses. ANY foreign row in
 *      the batch is enough to refuse the entire slice with an
 *      isolation error — the page never renders a partial
 *      cross-tenant list.
 *
 * "未经人工复核" badge:
 *   The demo dataset labels are always marked
 *   ``synthetic_reference_labels_unreviewed_no_human_review``.
 *   The page surfaces that fact as a prominent "未经人工复核"
 *   badge rather than rendering the literal raw string, so a PM
 *   cannot silently treat demo numbers as ground-truth.
 *
 * Actual vs Target:
 *   - Actual comes from ``value`` / ``value_display``: a real
 *     zero is preserved as "0.000000", a missing measurement is
 *     "NOT MEASURED". The two states are NOT interchangeable.
 *   - Target is a documented contract independent of the API:
 *     ``Feedback Type Accuracy ≥ 90%`` and ``Intent Accuracy
 *     ≥ 85%``. Anything else shows "尚未设定" — we never invent
 *     a target the doc didn't authorise.
 *
 * Cases view is intentionally narrow:
 *   We show only ``case_id`` / ``stage`` / ``passed`` /
 *   ``error_type`` / ``failed_stage``. The backend's
 *   ``input_hash`` / ``expected_hash`` / ``actual_hash`` /
 *   ``notes`` are NEVER rendered — the audit trail is preserved
 *   for log readers but the page must not pretend a fingerprint
 *   is the original feedback.
 *
 * Loading / Empty / Error / Retry:
 *   Every fetched surface (list, run header, metrics, cases) has
 *   its own ``loading`` / ``error`` state and Retry control, so
 *   a 5xx on one endpoint does not poison the whole page.
 *
 * Race protection:
 *   Each fetcher captures the project-scoped ``signal`` and
 *   re-checks ``mySignal.aborted`` / ``myProject`` / ``myRunId``
 *   after every await. Metrics and cases additionally check
 *   the page ref so a stale response from a previous page
 *   cannot overwrite a freshly-loaded one when the user
 *   rapidly clicks 上一页 / 下一页.
 *
 * Reset surface:
 *   - Project switch wipes list + selection + run + metrics +
 *     cases + their pages (the signal abort from
 *     ``useProjectScopedFetch`` is the primary guard; the
 *     state reset is belt-and-suspenders for any commit that
 *     races the abort).
 *   - Run switch wipes run + metrics + cases + their pages.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { TechnicalDetails } from '../../components/TechnicalDetails';
import { displayLabel, metricLabel } from '../../lib/presentation';
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
  EvaluationCaseRead,
  EvaluationMetricRead,
  EvaluationRunRead,
  Pagination,
} from '../../lib/api/types';

const DEFAULT_PAGE: Pagination = { limit: 25, offset: 0 };
/* Phase 7 F — per-run metrics / cases pagination. The previous
 * implementation hard-coded 100 which silently dropped failure
 * cases on later pages. 25 is small enough that the table
 * stays readable and the user can navigate quickly to find
 * failures anywhere in the run. */
const DETAIL_PAGE_LIMIT = 25;

/* Documented metric → target mapping (Phase 7 D2b brief).
 *
 * Only the two targets the brief authorised are listed. ``metric_name``
 * is the backend's stable key — pinned by the runner — and the
 * displayed ``label`` is the human-readable form the page renders.
 * The Intent metric used to be inferred from a different name; the
 * runner actually emits ``intent_exact_match`` so the lookup key MUST
 * match that — a fabricated name would just fall through to
 * "尚未设定". Any metric_name outside this table surfaces
 * "尚未设定" so the page never shows a target the doc didn't
 * authorise. */
const TARGETS: Record<string, { label: string; threshold: number }> = {
  feedback_type_accuracy: { label: '反馈类型准确率', threshold: 0.9 },
  intent_exact_match: { label: '意图匹配率', threshold: 0.85 },
};

const UNREVIEWED_LABEL =
  'synthetic_reference_labels_unreviewed_no_human_review';

export default function EvaluationPage() {
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();

  /* ---------- list state ---------- */

  const [page, setPage] = useState<Pagination>(DEFAULT_PAGE);
  const [list, setList] = useState<{
    items: EvaluationRunRead[];
    total: number;
  } | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  /* ---------- selection + detail state ---------- */

  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [run, setRun] = useState<EvaluationRunRead | null>(null);
  const [runLoading, setRunLoading] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  /* Phase 7 F — per-run metrics pagination. The fetcher
   * captures the page at call time; ``metricsPageRef`` mirrors
   * the latest page so the post-await race guard reads the
   * CURRENT value, not the stale closure. */
  const [metrics, setMetrics] = useState<EvaluationMetricRead[] | null>(null);
  const [metricsTotal, setMetricsTotal] = useState(0);
  const [metricsPage, setMetricsPage] = useState<Pagination>(DEFAULT_PAGE);
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const metricsPageRef = useRef<Pagination>(metricsPage);
  useEffect(() => {
    metricsPageRef.current = metricsPage;
  }, [metricsPage]);

  /* Phase 7 F — per-run cases pagination. The "失败案例" view
   * shows failures from the CURRENT page; the user navigates
   * to subsequent pages to find failures sitting later in the
   * run. Cases are NOT silently truncated to 100. */
  const [cases, setCases] = useState<EvaluationCaseRead[] | null>(null);
  const [casesTotal, setCasesTotal] = useState(0);
  const [casesPage, setCasesPage] = useState<Pagination>(DEFAULT_PAGE);
  const [casesLoading, setCasesLoading] = useState(false);
  const [casesError, setCasesError] = useState<string | null>(null);
  const casesPageRef = useRef<Pagination>(casesPage);
  useEffect(() => {
    casesPageRef.current = casesPage;
  }, [casesPage]);

  /* ---------- Reset every surface on project change ----------
   *
   * ``activeProjectId`` is the only key that matters here — a
   * project switch must clear both the list AND the selected run
   * so a stale run id from project A cannot be re-fetched with
   * project B's ``project_id`` (which the backend then refuses
   * with 404). The reset is intentionally aggressive: leaving
   * any pre-switch data on screen would be a UX bug AND an audit
   * bug. Pages for metrics / cases reset to offset 0 so the new
   * project lands on its first slice.
   */
  useEffect(() => {
    setPage(DEFAULT_PAGE);
    setList(null);
    setListError(null);
    setListLoading(false);
    setSelectedRunId(null);
    setRun(null);
    setRunError(null);
    setRunLoading(false);
    setMetrics(null);
    setMetricsError(null);
    setMetricsLoading(false);
    setMetricsTotal(0);
    setMetricsPage(DEFAULT_PAGE);
    metricsPageRef.current = DEFAULT_PAGE;
    setCases(null);
    setCasesError(null);
    setCasesLoading(false);
    setCasesTotal(0);
    setCasesPage(DEFAULT_PAGE);
    casesPageRef.current = DEFAULT_PAGE;
  }, [activeProjectId]);

  /* ---------- list fetcher ----------
   *
   * Batch-level project_id validation: if ANY returned item
   * belongs to a different project, the entire slice is
   * rejected with an isolation error so a partial foreign
   * list never lands on screen. */
  const fetchList = useCallback(async () => {
    if (!activeProjectId) return;
    if (!signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    setListLoading(true);
    setListError(null);
    const res = await client.listProjectEvaluations(
      myProject,
      { limit: page.limit, offset: page.offset },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setListError(res.error ? classifyError(res.error) : '加载评测列表失败。');
      setList(null);
      setListLoading(false);
      return;
    }
    const foreign = res.data.items.find(
      (it) => it.project_id !== myProject,
    );
    if (foreign) {
      setListError('运行列表包含外部项目数据，已拒绝整批显示。');
      setList(null);
      setListLoading(false);
      return;
    }
    setList({ items: res.data.items, total: res.data.total });
    setListLoading(false);
  }, [activeProjectId, client, page.limit, page.offset, signal]);

  useEffect(() => {
    void fetchList();
  }, [fetchList]);

  /* ---------- per-run fetcher ----------
   *
   * Sequential dependencies: the run row is fetched FIRST and
   * gates the metric / case fetches via the ``run`` boolean.
   * The frontend refuses to render a row whose ``project_id``
   * does not match the active project — defence in depth on top
   * of the backend's ``_enforce_project_match`` rule. */
  const fetchRun = useCallback(async () => {
    if (!selectedRunId || !activeProjectId) return;
    if (!signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    const myRunId = selectedRunId;
    setRunLoading(true);
    setRunError(null);
    setRun(null);
    setMetrics(null);
    setMetricsError(null);
    setMetricsTotal(0);
    setCases(null);
    setCasesError(null);
    setCasesTotal(0);
    const res = await client.getEvaluation(
      myRunId,
      { project_id: myProject },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (myRunId !== selectedRunId) return;
    if (res.error || !res.data) {
      setRunError(res.error ? classifyError(res.error) : '加载评测运行失败。');
      setRunLoading(false);
      return;
    }
    if (res.data.project_id !== activeProjectId) {
      setRunError('该项目不匹配，拒绝显示。');
      setRun(null);
      setRunLoading(false);
      return;
    }
    setRun(res.data);
    setRunLoading(false);
  }, [activeProjectId, client, selectedRunId, signal]);

  useEffect(() => {
    void fetchRun();
  }, [fetchRun]);

  /* ---------- metrics fetcher (paginated) ----------
   *
   * Phase 7 F — driven by ``metricsPage``. ``metricsPage`` is
   * in the deps so changing the page re-creates this callback
   * which re-fires the useEffect below. The post-await check
   * against ``metricsPageRef.current`` guards against a
   * previous-page response committing after the user has
   * already navigated forward. */
  const fetchMetrics = useCallback(async () => {
    if (!selectedRunId || !activeProjectId || !run) return;
    if (!signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    const myRunId = selectedRunId;
    const myPage = metricsPage;
    setMetricsLoading(true);
    setMetricsError(null);
    const res = await client.listEvaluationMetrics(
      myRunId,
      {
        project_id: myProject,
        limit: myPage.limit,
        offset: myPage.offset,
      },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (myRunId !== selectedRunId) return;
    if (
      metricsPageRef.current.limit !== myPage.limit ||
      metricsPageRef.current.offset !== myPage.offset
    ) {
      return;
    }
    if (res.error || !res.data) {
      setMetricsError(res.error ? classifyError(res.error) : '加载指标失败。');
      setMetrics(null);
      setMetricsTotal(0);
      setMetricsLoading(false);
      return;
    }
    const foreign = res.data.items.find(
      (it) => it.project_id !== myProject,
    );
    if (foreign) {
      setMetricsError('指标包含外部项目数据，已拒绝整批显示。');
      setMetrics(null);
      setMetricsTotal(0);
      setMetricsLoading(false);
      return;
    }
    setMetrics(res.data.items);
    setMetricsTotal(res.data.total);
    /* Only sync ``metricsPage`` back from the response when the
     * backend actually normalised the values. Writing a fresh
     * object on every response — even when ``limit/offset``
     * matched the request — would change the ``metricsPage``
     * reference, re-create this callback, re-fire the
     * ``useEffect`` below, and produce an infinite refetch
     * loop. */
    if (
      res.data.limit !== myPage.limit ||
      res.data.offset !== myPage.offset
    ) {
      setMetricsPage({ limit: res.data.limit, offset: res.data.offset });
    }
    setMetricsLoading(false);
  }, [activeProjectId, client, metricsPage, run, selectedRunId, signal]);

  useEffect(() => {
    void fetchMetrics();
  }, [fetchMetrics]);

  /* ---------- cases fetcher (paginated) ----------
   *
   * Phase 7 F — same shape as metrics. The "失败案例" view
   * shows failures from the CURRENT page; the pagination
   * controls let the user find failures on later pages. The
   * previous truncation at 100 is gone. */
  const fetchCases = useCallback(async () => {
    if (!selectedRunId || !activeProjectId || !run) return;
    if (!signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    const myRunId = selectedRunId;
    const myPage = casesPage;
    setCasesLoading(true);
    setCasesError(null);
    const res = await client.listEvaluationCases(
      myRunId,
      {
        project_id: myProject,
        limit: myPage.limit,
        offset: myPage.offset,
      },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (myRunId !== selectedRunId) return;
    if (
      casesPageRef.current.limit !== myPage.limit ||
      casesPageRef.current.offset !== myPage.offset
    ) {
      return;
    }
    if (res.error || !res.data) {
      setCasesError(res.error ? classifyError(res.error) : '加载失败案例失败。');
      setCases(null);
      setCasesTotal(0);
      setCasesLoading(false);
      return;
    }
    const foreign = res.data.items.find(
      (it) => it.project_id !== myProject,
    );
    if (foreign) {
      setCasesError('案例包含外部项目数据，已拒绝整批显示。');
      setCases(null);
      setCasesTotal(0);
      setCasesLoading(false);
      return;
    }
    setCases(res.data.items);
    setCasesTotal(res.data.total);
    /* See the matching comment in ``fetchMetrics``: a fresh
     * ``setCasesPage`` object on every response would flip the
     * ``casesPage`` reference and re-fire the effect, looping
     * forever. Only commit when the backend actually changed
     * the values. */
    if (
      res.data.limit !== myPage.limit ||
      res.data.offset !== myPage.offset
    ) {
      setCasesPage({ limit: res.data.limit, offset: res.data.offset });
    }
    setCasesLoading(false);
  }, [activeProjectId, client, casesPage, run, selectedRunId, signal]);

  useEffect(() => {
    void fetchCases();
  }, [fetchCases]);

  /* ---------- Run switch handler ----------
   *
   * Resetting the metrics / cases pages on run switch is
   * essential — otherwise clicking run B while sitting on run
   * A's metrics page 2 would briefly request run B's data at
   * page 2, which makes no sense and would surface a
   * misleading empty state. */
  const selectRun = useCallback((id: string) => {
    setMetricsPage(DEFAULT_PAGE);
    setCasesPage(DEFAULT_PAGE);
    metricsPageRef.current = DEFAULT_PAGE;
    casesPageRef.current = DEFAULT_PAGE;
    setSelectedRunId(id);
  }, []);

  /* ---------- Derived ---------- */

  const pageCount = useMemo(() => {
    if (!list || list.total === 0) return 1;
    return Math.max(1, Math.ceil(list.total / page.limit));
  }, [list, page.limit]);
  const currentPage = Math.floor(page.offset / page.limit) + 1;

  const metricsPageCount = useMemo(() => {
    if (metricsTotal === 0) return 1;
    return Math.max(1, Math.ceil(metricsTotal / metricsPage.limit));
  }, [metricsTotal, metricsPage.limit]);
  const metricsCurrentPage = Math.floor(metricsPage.offset / metricsPage.limit) + 1;

  const casesPageCount = useMemo(() => {
    if (casesTotal === 0) return 1;
    return Math.max(1, Math.ceil(casesTotal / casesPage.limit));
  }, [casesTotal, casesPage.limit]);
  const casesCurrentPage = Math.floor(casesPage.offset / casesPage.limit) + 1;

  const failureCases = useMemo(
    () => (cases ?? []).filter((c) => c.passed === false),
    [cases],
  );

  /* ---------- Render helpers ---------- */

  function targetFor(metricName: string): { label: string; threshold: number } | null {
    return TARGETS[metricName] ?? null;
  }

  function renderActual(metric: EvaluationMetricRead): string {
    /* ``value`` is REQUIRED (number | null). ``value_display`` is
     * the backend's stable rendering:
     *   value: 0      → '0.000000'
     *   value: null   → 'NOT MEASURED'
     * The UI MUST branch on ``value === null`` (NOT on falsiness)
     * so a real zero is never collapsed to "Not Measured". */
    if (metric.value === null) {
      return '未测量';
    }
    return metric.value_display || String(metric.value);
  }

  /* ---------- Top-level guards ---------- */

  if (!activeProjectId) {
    return (
      <EmptyState
        title="请先选择项目"
        description="在右上角选择或创建一个项目，以查看对应的评测历史。"
      />
    );
  }

  if (listLoading && !list) return <LoadingState />;
  if (listError && !list) {
    return (
      <ErrorState
        message={listError}
        onRetry={() => {
          setListError(null);
          void fetchList();
        }}
      />
    );
  }

  const listItems = list?.items ?? [];
  const listTotal = list?.total ?? 0;
  const listEmpty = !listLoading && listItems.length === 0;

  /* ---------- Render ---------- */

  return (
    <div className="page page-evaluation">
      <header className="page-header">
        <span className="eyebrow">质量评测</span>
        <h1 className="page-title" data-testid="evaluation-page-title">
          质量评测
        </h1>
        <p className="page-subtitle">查看分析质量、参考目标与失败案例。</p>
      </header>

      {/* ---- Runs list ---- */}
      <section className="section" data-testid="evaluation-list-section">
        <h2 className="section-title">运行历史</h2>
        {listEmpty ? (
          <EmptyState
            title="该项目下尚无评测"
            description="当前项目还没有评测记录。"
          />
        ) : listItems.length > 0 ? (
          <>
            <ul className="eval-list" data-testid="eval-list">
              {listItems.map((r) => {
                const isSelected = selectedRunId === r.id;
                return (
                  <li
                    key={r.id}
                    className={`eval-row${isSelected ? ' is-active' : ''}`}
                    data-testid="eval-row"
                    data-eval-id={r.id}
                  >
                    <button
                      type="button"
                      className="eval-row-btn"
                      data-testid="eval-row-button"
                      data-eval-button-id={r.id}
                      aria-pressed={isSelected}
                      onClick={() => selectRun(r.id)}
                    >
                      <div className="eval-row-head">
                        <span className="eval-row-id">{displayLabel(r.stage, "质量评测")}</span>
                        <span className="badge badge-outline">{displayLabel(r.evaluation_type, "质量评测")}</span>
                      </div>
                      <p className="eval-row-meta">
                        {displayLabel(r.stage)} · {displayLabel(r.status)} ·{' '}
                        {formatRelative(r.created_at)}
                      </p>
                    </button>
                  </li>
                );
              })}
            </ul>
            {listTotal > page.limit ? (
              <footer
                className="pagination"
                data-testid="eval-list-pagination"
              >
                <span className="pagination-meta">
                  共 {listTotal} 条运行 · 第 {currentPage} / {pageCount} 页
                </span>
                <div className="pagination-controls">
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    data-testid="eval-list-prev"
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
                    data-testid="eval-list-next"
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
          </>
        ) : null}
      </section>

      {/* ---- Detail ---- */}
      <section className="section" data-testid="evaluation-detail-section">
        <h2 className="section-title">运行详情</h2>
        {!selectedRunId ? (
          <EmptyState
            title="选择左侧任一评测运行"
            description="查看样本、实测指标和失败案例。"
          />
        ) : runLoading ? (
          <LoadingState />
        ) : runError ? (
          <ErrorState
            message={runError}
            onRetry={() => {
              setRunError(null);
              void fetchRun();
            }}
          />
        ) : !run ? (
          <EmptyState title="无可显示内容" />
        ) : (
          <>
            <header
              className="eval-detail-header"
              data-testid="eval-detail-header"
            >
              <span className="eyebrow">{displayLabel(run.evaluation_type, "质量评测")}</span>
              <h3 className="section-title" data-testid="eval-detail-title">
                {displayLabel(run.stage, "评测结果")}
              </h3>
              <dl className="meta-grid">
                <dt>评测数据</dt>
                <dd data-testid="eval-dataset-name">{run.dataset_name}</dd>
                <dt>版本</dt>
                <dd data-testid="eval-dataset-version">{run.dataset_version}</dd>
                <dt>样本分组</dt>
                <dd>{displayLabel(run.dataset_split)}</dd>
                <dt>样本数</dt>
                <dd data-testid="eval-sample-count">{run.input_count}</dd>
                <dt>模型</dt>
                <dd data-testid="eval-model">{run.model ?? '—'}</dd>
                <dt>样本复核状态</dt>
                <dd data-testid="eval-label-status">
                  {run.dataset_label_status === UNREVIEWED_LABEL ? (
                    <span
                      className="badge badge-danger"
                      data-testid="eval-label-status-unreviewed"
                    >
                      未经人工复核
                    </span>
                  ) : (
                    <span>{displayLabel(run.dataset_label_status, "待人工复核")}</span>
                  )}
                </dd>
              </dl>
              <TechnicalDetails label="查看评测配置"><dl className="meta-grid">                <dt>提示词版本</dt>
                <dd data-testid="eval-prompt-version">
                  {run.prompt_version ?? '—'}
                </dd>
                <dt>流程版本</dt>
                <dd>{run.pipeline_version ?? '—'}</dd>
                <dt>评分规则</dt>
                <dd>{run.calculation_version}</dd>
</dl></TechnicalDetails>
            </header>

            {/* Metrics */}
            <section data-testid="eval-metrics-section">
              <h3 className="section-title">指标</h3>
              {metricsLoading ? (
                <LoadingState />
              ) : metricsError ? (
                <ErrorState
                  message={metricsError}
                  onRetry={() => {
                    setMetricsError(null);
                    void fetchMetrics();
                  }}
                />
              ) : !metrics || metrics.length === 0 ? (
                <EmptyState
                  title={
                    metricsTotal > 0
                      ? '本页无指标（请翻页查找）'
                      : '该运行未产出指标'
                  }
                />
              ) : (
                <>
                  <table
                    className="metric-table"
                    data-testid="eval-metrics-table"
                  >
                    <thead>
                      <tr>
                        <th>指标</th>
                        <th>环节</th>
                        <th>样本数</th>
                        <th>实测结果</th>
                        <th>参考目标</th>
                      </tr>
                    </thead>
                    <tbody>
                      {metrics.map((m) => {
                        const target = targetFor(m.metric_name);
                        return (
                          <tr
                            key={m.id}
                            data-testid="eval-metric-row"
                            data-metric-name={m.metric_name}
                          >
                            <td title={m.metric_name}>{metricLabel(m.metric_name)}</td>
                            <td>{displayLabel(m.stage)}</td>
                            <td>{m.sample_count}</td>
                            <td data-testid="eval-metric-actual">
                              {renderActual(m)}
                            </td>
                            <td data-testid="eval-metric-target">
                              {target
                                ? `${target.label} ≥ ${(target.threshold * 100).toFixed(0)}%`
                                : '尚未设定'}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {metricsTotal > metricsPage.limit ? (
                    <footer
                      className="pagination"
                      data-testid="eval-metrics-pagination"
                    >
                      <span
                        className="pagination-meta"
                        data-testid="eval-metrics-page-info"
                      >
                        共 {metricsTotal} 条指标 · 第 {metricsCurrentPage} /{' '}
                        {metricsPageCount} 页
                      </span>
                      <div className="pagination-controls">
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          data-testid="eval-metrics-prev"
                          disabled={metricsCurrentPage <= 1}
                          onClick={() =>
                            setMetricsPage((p) => ({
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
                          data-testid="eval-metrics-next"
                          disabled={metricsCurrentPage >= metricsPageCount}
                          onClick={() =>
                            setMetricsPage((p) => ({
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
                </>
              )}
            </section>

            {/* Cases */}
            <section data-testid="eval-cases-section">
              <h3 className="section-title">失败案例</h3>
              {casesLoading ? (
                <LoadingState />
              ) : casesError ? (
                <ErrorState
                  message={casesError}
                  onRetry={() => {
                    setCasesError(null);
                    void fetchCases();
                  }}
                />
              ) : !cases ? (
                <EmptyState title="本次评测无失败案例" />
              ) : (
                <>
                  <p
                    className="pagination-meta"
                    data-testid="eval-cases-page-info"
                  >
                    {failureCases.length === 0
                      ? `本页 0 条失败 · 共 ${casesTotal} 条案例 · 第 ${casesCurrentPage} / ${casesPageCount} 页`
                      : `本页 ${failureCases.length} 条失败 · 共 ${casesTotal} 条案例 · 第 ${casesCurrentPage} / ${casesPageCount} 页`}
                    {casesTotal > casesPage.limit
                      ? '（后续页可能含其它失败，翻页查找）'
                      : ''}
                  </p>
                  {failureCases.length === 0 ? (
                    <EmptyState
                      title={
                        casesTotal === 0
                          ? '本次评测无失败案例'
                          : casesTotal > casesPage.limit
                            ? '本页无失败 — 翻页查找其它页'
                            : '本次评测无失败案例'
                      }
                    />
                  ) : (
                    <table
                      className="case-table"
                      data-testid="eval-cases-table"
                    >
                      <thead>
                        <tr>
                          <th>案例编号</th>
                          <th>环节</th>
                          <th>是否通过</th>
                          <th>失败原因</th>
                          <th>失败环节</th>
                        </tr>
                      </thead>
                      <tbody>
                        {failureCases.map((c) => (
                          <tr
                            key={c.id}
                            data-testid="eval-case-row"
                            data-case-id={c.case_id}
                          >
                            <td className="mono">{c.case_id}</td>
                            <td>{displayLabel(c.stage)}</td>
                            <td>{c.passed === false ? '失败' : '未知'}</td>
                            <td>{displayLabel(c.error_type, "处理失败")}</td>
                            <td>{displayLabel(c.failed_stage, "处理失败")}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {casesTotal > casesPage.limit ? (
                    <footer
                      className="pagination"
                      data-testid="eval-cases-pagination"
                    >
                      <span className="pagination-meta">
                        共 {casesTotal} 条 · 第 {casesCurrentPage} /{' '}
                        {casesPageCount} 页
                      </span>
                      <div className="pagination-controls">
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          data-testid="eval-cases-prev"
                          disabled={casesCurrentPage <= 1}
                          onClick={() =>
                            setCasesPage((p) => ({
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
                          data-testid="eval-cases-next"
                          disabled={casesCurrentPage >= casesPageCount}
                          onClick={() =>
                            setCasesPage((p) => ({
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
                </>
              )}
            </section>
          </>
        )}
      </section>
    </div>
  );
}
