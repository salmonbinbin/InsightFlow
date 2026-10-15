'use client';

/**
 * InsightFlow — Analysis page (Phase 7 D package).
 *
 * Three-phase AI workflow (classification → clustering → insight)
 * now reads history from the project-scoped backend endpoints
 * (``GET /api/projects/{id}/analysis-runs`` and
 * ``GET /api/projects/{id}/insight-runs``) instead of sessionStorage.
 * The page still owns the POST create / GET by-id / POST retry
 * round-trips that drive each stage; each round-trip
 * (create / get / retry) re-validates the response's
 * ``project_id`` against the active project so a foreign row
 * never reaches the UI, never seeds a downstream stage, and
 * never gets reloaded as a history entry.
 *
 * Cross-project defense is enforced in two layers:
 *   1. Backend 404 (the backend's ``_enforce_project_match``
 *      rule; for the list endpoints the project is encoded in
 *      the path so the row is filtered at the SQL ``WHERE``).
 *   2. Frontend ``res.data.project_id === activeProjectId``
 *      check on every create / get / retry response. A foreign
 *      row is converted to an error and the panel stays in its
 *      previous state so the user does not see a leaked row
 *      land on a different project's screen.
 *
 * Race-condition guard: every fetch captures the project-scoped
 * ``signal`` from ``useProjectScopedFetch()`` and re-checks
 * ``signal.aborted`` and ``activeProjectId`` after each await so
 * a project switch can never let a stale response overwrite the
 * new project's panels. Project switch is the single trigger
 * that wipes the three single-run panels AND the three history
 * lists, so the screen can never display a cross-tenant row that
 * raced the switch.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react';
import { useProjectContext } from '../../contexts/ProjectContext';
import { getApiClient } from '../../lib/api/client';
import { useProjectScopedFetch } from '../../lib/useProjectScopedFetch';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  StatusPill,
} from '../../components/StateViews';
import { RunCard } from '../../components/RunCard';
import {
  classifyError,
  formatRelative,
  normaliseRunStatus,
} from '../../lib/format';
import type {
  AnalysisRun,
  ClusterRun,
  InsightRun,
  Pagination,
} from '../../lib/api/types';

const HISTORY_PAGE_SIZE = 10;

interface RunPanelState<T> {
  loading: boolean;
  data: T | null;
  error: string | null;
  submitting: boolean;
}

interface HistoryState<T> {
  items: T[];
  total: number;
  loading: boolean;
  error: string | null;
  page: Pagination;
}

const initialRunPanel = <T,>(): RunPanelState<T> => ({
  loading: false,
  data: null,
  error: null,
  submitting: false,
});

const initialHistory = <T,>(): HistoryState<T> => ({
  items: [],
  total: 0,
  loading: false,
  error: null,
  page: { limit: HISTORY_PAGE_SIZE, offset: 0 },
});

/* Module-level cross-project guard.
 *
 * Pure function (no React closure) so the page's nine
 * ``useCallback``s that call it pass the
 * ``react-hooks/exhaustive-deps`` lint without each one
 * pinning ``guardProject`` (and thus the fetcher identity)
 * on the active project. Taking the active project as a
 * parameter keeps the function deterministic and
 * dependency-free; the caller still threads
 * ``activeProjectId`` in at call time so a stale closure
 * can never pass a foreign row through. */
function guardProject(
  run: { project_id: string } | null | undefined,
  activeProjectId: string | null,
): string | null {
  if (!run) return '响应缺少 project_id。';
  if (!activeProjectId) return '当前项目未选择。';
  if (run.project_id !== activeProjectId) {
    return '该运行不属于当前项目，已拒绝显示。';
  }
  return null;
}

export default function AnalysisPage() {
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();

  /* ---------- per-stage single-run panels ---------- */
  const [analysis, setAnalysis] = useState<RunPanelState<AnalysisRun>>(
    initialRunPanel<AnalysisRun>(),
  );
  const [cluster, setCluster] = useState<RunPanelState<ClusterRun>>(
    initialRunPanel<ClusterRun>(),
  );
  const [insight, setInsight] = useState<RunPanelState<InsightRun>>(
    initialRunPanel<InsightRun>(),
  );

  /* ---------- per-stage history (project-scoped, paginated) ---------- */
  const [analysisHistory, setAnalysisHistory] = useState<HistoryState<AnalysisRun>>(
    initialHistory<AnalysisRun>(),
  );
  const [clusterHistory, setClusterHistory] = useState<HistoryState<ClusterRun>>(
    initialHistory<ClusterRun>(),
  );
  const [insightHistory, setInsightHistory] = useState<HistoryState<InsightRun>>(
    initialHistory<InsightRun>(),
  );

  /* pageRef mirrors the latest ``history.page`` so the history
   * fetchers can be ``useCallback``-stable across pagination and
   * still read the freshest offset/limit. Without the ref the
   * fetcher would either re-create on every pagination click
   * (lints clean but pins the active project through a stale
   * page) or pin the page in the closure (lints dirty AND lets
   * ``runAnalysis`` / ``runClustering`` / ``runInsight`` race
   * the state update with the very same closure they just
   * wrote to). The ref makes the create-after-setState path
   * deterministic: update the ref synchronously, then call the
   * fetcher, then let React reconcile the state. */
  const analysisPageRef = useRef<Pagination>(analysisHistory.page);
  const clusterPageRef = useRef<Pagination>(clusterHistory.page);
  const insightPageRef = useRef<Pagination>(insightHistory.page);
  useEffect(() => {
    analysisPageRef.current = analysisHistory.page;
  }, [analysisHistory.page]);
  useEffect(() => {
    clusterPageRef.current = clusterHistory.page;
  }, [clusterHistory.page]);
  useEffect(() => {
    insightPageRef.current = insightHistory.page;
  }, [insightHistory.page]);

  const [analysisInput, setAnalysisInput] = useState('');
  const [clusteringAnalysisRunId, setClusteringAnalysisRunId] = useState('');

  /* Cross-project defense: module-level ``guardProject`` is the
   * single source of truth. It is intentionally not declared
   * inside the component (see the function above) so the nine
   * ``useCallback``s that call it do not pin a per-render
   * closure identity. The active project is threaded in at
   * call time so a stale closure can never pass a foreign
   * row through. */

  /* ---------- History fetchers ----------
   *
   * Each fetcher is ``useCallback``-stable across pagination
   * because it reads the page from the matching ``pageRef`` and
   * ``pageRef`` is not in the deps. Deps are
   * ``[activeProjectId, client, signal]`` — the only values
   * that, when they change, should re-establish the fetch. The
   * ``pageRef`` is updated synchronously by the create /
   * pagination / retry handlers so a refetch immediately after
   * a state write always reads the new offset.
   *
   * List-level cross-project defense: after the API responds,
   * the page scans every item for a foreign ``project_id`` and
   * rejects the ENTIRE batch with an isolation error. A single
   * leaked row is enough to refuse the slice — we never render
   * a partially-foreign list. */

  const fetchAnalysisHistory = useCallback(async () => {
    if (!activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    const myPage = analysisPageRef.current;
    setAnalysisHistory((s) => ({ ...s, loading: true, error: null }));
    const res = await client.listProjectAnalysisRuns(
      myProject,
      { run_type: 'classification', limit: myPage.limit, offset: myPage.offset },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setAnalysisHistory((s) => ({
        ...s,
        loading: false,
        error: res.error ? classifyError(res.error) : '加载分类历史失败。',
      }));
      return;
    }
    const foreign = res.data.items.find((it) => it.project_id !== myProject);
    if (foreign) {
      setAnalysisHistory((s) => ({
        ...s,
        loading: false,
        items: [],
        total: 0,
        error: '历史包含外部项目数据，已拒绝整批显示。',
      }));
      return;
    }
    setAnalysisHistory({
      items: res.data.items,
      total: res.data.total,
      loading: false,
      error: null,
      page: { limit: res.data.limit, offset: res.data.offset },
    });
  }, [activeProjectId, client, signal]);

  const fetchClusterHistory = useCallback(async () => {
    if (!activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    const myPage = clusterPageRef.current;
    setClusterHistory((s) => ({ ...s, loading: true, error: null }));
    const res = await client.listProjectAnalysisRuns(
      myProject,
      { run_type: 'clustering', limit: myPage.limit, offset: myPage.offset },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setClusterHistory((s) => ({
        ...s,
        loading: false,
        error: res.error ? classifyError(res.error) : '加载聚类历史失败。',
      }));
      return;
    }
    const foreign = res.data.items.find((it) => it.project_id !== myProject);
    if (foreign) {
      setClusterHistory((s) => ({
        ...s,
        loading: false,
        items: [],
        total: 0,
        error: '历史包含外部项目数据，已拒绝整批显示。',
      }));
      return;
    }
    setClusterHistory({
      items: res.data.items,
      total: res.data.total,
      loading: false,
      error: null,
      page: { limit: res.data.limit, offset: res.data.offset },
    });
  }, [activeProjectId, client, signal]);

  const fetchInsightHistory = useCallback(async () => {
    if (!activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    const myPage = insightPageRef.current;
    setInsightHistory((s) => ({ ...s, loading: true, error: null }));
    const res = await client.listProjectInsightRuns(
      myProject,
      { limit: myPage.limit, offset: myPage.offset },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setInsightHistory((s) => ({
        ...s,
        loading: false,
        error: res.error ? classifyError(res.error) : '加载洞察历史失败。',
      }));
      return;
    }
    const foreign = res.data.items.find((it) => it.project_id !== myProject);
    if (foreign) {
      setInsightHistory((s) => ({
        ...s,
        loading: false,
        items: [],
        total: 0,
        error: '历史包含外部项目数据，已拒绝整批显示。',
      }));
      return;
    }
    setInsightHistory({
      items: res.data.items,
      total: res.data.total,
      loading: false,
      error: null,
      page: { limit: res.data.limit, offset: res.data.offset },
    });
  }, [activeProjectId, client, signal]);

  /* ---------- Reset everything on project change ----------
   *
   * The single key here is ``activeProjectId``: a project switch
   * must wipe both the three single-run panels AND the three
   * history lists so a stale foreign row from project A can
   * never be re-rendered on project B's screen. The reset is
   * intentionally aggressive — leaving any pre-switch data
   * visible would be a UX bug AND a multi-tenant audit bug. */
  useEffect(() => {
    setAnalysis(initialRunPanel<AnalysisRun>());
    setCluster(initialRunPanel<ClusterRun>());
    setInsight(initialRunPanel<InsightRun>());
    const ah = initialHistory<AnalysisRun>();
    const ch = initialHistory<ClusterRun>();
    const ih = initialHistory<InsightRun>();
    setAnalysisHistory(ah);
    setClusterHistory(ch);
    setInsightHistory(ih);
    /* Keep the pageRef in lockstep with the reset state so a
     * late-arriving fetch from the previous project (defence
     * in depth alongside the abort signal) reads the new
     * project's offset=0 page rather than the old one. */
    analysisPageRef.current = ah.page;
    clusterPageRef.current = ch.page;
    insightPageRef.current = ih.page;
    setAnalysisInput('');
    setClusteringAnalysisRunId('');
  }, [activeProjectId]);

  useEffect(() => {
    void fetchAnalysisHistory();
  }, [fetchAnalysisHistory]);

  useEffect(() => {
    void fetchClusterHistory();
  }, [fetchClusterHistory]);

  useEffect(() => {
    void fetchInsightHistory();
  }, [fetchInsightHistory]);

  /* ---------- Classification: create / load / retry ---------- */

  const runAnalysis = useCallback(async () => {
    if (!activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    setAnalysis((s) => ({ ...s, submitting: true, error: null }));
    const res = await client.createAnalysis(myProject, mySignal);
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setAnalysis((s) => ({
        ...s,
        submitting: false,
        error: res.error ? classifyError(res.error) : '启动分类失败。',
      }));
      return;
    }
    const guard = guardProject(res.data, myProject);
    if (guard) {
      setAnalysis({ ...initialRunPanel<AnalysisRun>(), error: guard });
      return;
    }
    setAnalysis({ ...initialRunPanel<AnalysisRun>(), data: res.data });
    // Refetch the project-scoped history so the new row surfaces
    // at the top of the list. Update the pageRef BEFORE calling
    // the fetcher so the new request uses offset=0 rather than
    // racing the React state commit (the setState below is
    // async; the ref read in the fetcher closure would
    // otherwise observe the stale offset until the next render).
    const newAnalysisPage = { ...analysisPageRef.current, offset: 0 };
    analysisPageRef.current = newAnalysisPage;
    setAnalysisHistory((s) => ({ ...s, page: newAnalysisPage }));
    void fetchAnalysisHistory();
  }, [activeProjectId, client, fetchAnalysisHistory, signal]);

  const loadAnalysisById = useCallback(
    async (runId: string) => {
      if (!runId || !activeProjectId || !signal) return;
      const mySignal = signal;
      const myProject = activeProjectId;
      setAnalysis((s) => ({ ...s, loading: true, error: null }));
      const res = await client.getAnalysis(runId, mySignal);
      if (mySignal.aborted) return;
      if (myProject !== activeProjectId) return;
      if (res.error || !res.data) {
        setAnalysis((s) => ({
          ...s,
          loading: false,
          error: res.error ? classifyError(res.error) : '加载失败。',
        }));
        return;
      }
      const guard = guardProject(res.data, myProject);
      if (guard) {
        setAnalysis({ ...initialRunPanel<AnalysisRun>(), error: guard });
        return;
      }
      setAnalysis({ ...initialRunPanel<AnalysisRun>(), data: res.data });
    },
    [activeProjectId, client, signal],
  );

  const handleAnalysisInput = useCallback(
    (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      void loadAnalysisById(analysisInput.trim());
    },
    [analysisInput, loadAnalysisById],
  );

  const retryAnalysis = useCallback(async () => {
    if (!analysis.data || !activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    setAnalysis((s) => ({ ...s, submitting: true, error: null }));
    const res = await client.retryAnalysis(analysis.data.id, mySignal);
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setAnalysis((s) => ({
        ...s,
        submitting: false,
        error: res.error ? classifyError(res.error) : '重试失败。',
      }));
      return;
    }
    const guard = guardProject(res.data, myProject);
    if (guard) {
      setAnalysis((s) => ({ ...s, submitting: false, error: guard }));
      return;
    }
    setAnalysis((s) => ({ ...s, submitting: false, data: res.data }));
    void fetchAnalysisHistory();
  }, [activeProjectId, analysis.data, client, fetchAnalysisHistory, signal]);

  /* ---------- Clustering: create / load / retry ---------- */

  const runClustering = useCallback(async () => {
    if (!activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    if (!clusteringAnalysisRunId.trim()) {
      setCluster((s) => ({ ...s, error: '请先填写上游分类 Run ID。' }));
      return;
    }
    setCluster((s) => ({ ...s, submitting: true, error: null }));
    const res = await client.createClustering(
      myProject,
      clusteringAnalysisRunId.trim(),
      mySignal,
    );
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setCluster((s) => ({
        ...s,
        submitting: false,
        error: res.error ? classifyError(res.error) : '启动聚类失败。',
      }));
      return;
    }
    const guard = guardProject(res.data, myProject);
    if (guard) {
      setCluster({ ...initialRunPanel<ClusterRun>(), error: guard });
      return;
    }
    setCluster({ ...initialRunPanel<ClusterRun>(), data: res.data });
    // Update the pageRef synchronously so the in-line
    // refetch uses offset=0 instead of the stale closure
    // (see runAnalysis for the full rationale).
    const newClusterPage = { ...clusterPageRef.current, offset: 0 };
    clusterPageRef.current = newClusterPage;
    setClusterHistory((s) => ({ ...s, page: newClusterPage }));
    void fetchClusterHistory();
  }, [activeProjectId, client, clusteringAnalysisRunId, fetchClusterHistory, signal]);

  const loadClusteringById = useCallback(
    async (runId: string) => {
      if (!runId || !activeProjectId || !signal) return;
      const mySignal = signal;
      const myProject = activeProjectId;
      setCluster((s) => ({ ...s, loading: true, error: null }));
      const res = await client.getClustering(runId, mySignal);
      if (mySignal.aborted) return;
      if (myProject !== activeProjectId) return;
      if (res.error || !res.data) {
        setCluster((s) => ({
          ...s,
          loading: false,
          error: res.error ? classifyError(res.error) : '加载失败。',
        }));
        return;
      }
      const guard = guardProject(res.data, myProject);
      if (guard) {
        setCluster({ ...initialRunPanel<ClusterRun>(), error: guard });
        return;
      }
      setCluster({ ...initialRunPanel<ClusterRun>(), data: res.data });
    },
    [activeProjectId, client, signal],
  );

  const retryClustering = useCallback(async () => {
    if (!cluster.data || !activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    setCluster((s) => ({ ...s, submitting: true, error: null }));
    const res = await client.retryClustering(cluster.data.id, mySignal);
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setCluster((s) => ({
        ...s,
        submitting: false,
        error: res.error ? classifyError(res.error) : '重试失败。',
      }));
      return;
    }
    const guard = guardProject(res.data, myProject);
    if (guard) {
      setCluster((s) => ({ ...s, submitting: false, error: guard }));
      return;
    }
    setCluster((s) => ({ ...s, submitting: false, data: res.data }));
    void fetchClusterHistory();
  }, [activeProjectId, cluster.data, client, fetchClusterHistory, signal]);

  /* ---------- Insight: create / load / retry ---------- */

  const runInsight = useCallback(async () => {
    if (!activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    const clusteringId = cluster.data?.id ?? '';
    if (!clusteringId) {
      setInsight((s) => ({ ...s, error: '请先获取一个聚类 Run。' }));
      return;
    }
    setInsight((s) => ({ ...s, submitting: true, error: null }));
    const res = await client.generateInsights(myProject, clusteringId, mySignal);
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setInsight((s) => ({
        ...s,
        submitting: false,
        error: res.error ? classifyError(res.error) : '启动洞察失败。',
      }));
      return;
    }
    const guard = guardProject(res.data, myProject);
    if (guard) {
      setInsight({ ...initialRunPanel<InsightRun>(), error: guard });
      return;
    }
    setInsight({ ...initialRunPanel<InsightRun>(), data: res.data });
    // Update the pageRef synchronously so the in-line
    // refetch uses offset=0 instead of the stale closure
    // (see runAnalysis for the full rationale).
    const newInsightPage = { ...insightPageRef.current, offset: 0 };
    insightPageRef.current = newInsightPage;
    setInsightHistory((s) => ({ ...s, page: newInsightPage }));
    void fetchInsightHistory();
  }, [activeProjectId, client, cluster.data?.id, fetchInsightHistory, signal]);

  const loadInsightById = useCallback(
    async (runId: string) => {
      if (!runId || !activeProjectId || !signal) return;
      const mySignal = signal;
      const myProject = activeProjectId;
      setInsight((s) => ({ ...s, loading: true, error: null }));
      const res = await client.getInsightRun(runId, mySignal);
      if (mySignal.aborted) return;
      if (myProject !== activeProjectId) return;
      if (res.error || !res.data) {
        setInsight((s) => ({
          ...s,
          loading: false,
          error: res.error ? classifyError(res.error) : '加载失败。',
        }));
        return;
      }
      const guard = guardProject(res.data, myProject);
      if (guard) {
        setInsight({ ...initialRunPanel<InsightRun>(), error: guard });
        return;
      }
      setInsight({ ...initialRunPanel<InsightRun>(), data: res.data });
    },
    [activeProjectId, client, signal],
  );

  const retryInsight = useCallback(async () => {
    if (!insight.data || !activeProjectId || !signal) return;
    const mySignal = signal;
    const myProject = activeProjectId;
    setInsight((s) => ({ ...s, submitting: true, error: null }));
    const res = await client.retryInsightRun(insight.data.id, mySignal);
    if (mySignal.aborted) return;
    if (myProject !== activeProjectId) return;
    if (res.error || !res.data) {
      setInsight((s) => ({
        ...s,
        submitting: false,
        error: res.error ? classifyError(res.error) : '重试失败。',
      }));
      return;
    }
    const guard = guardProject(res.data, myProject);
    if (guard) {
      setInsight((s) => ({ ...s, submitting: false, error: guard }));
      return;
    }
    setInsight((s) => ({ ...s, submitting: false, data: res.data }));
    void fetchInsightHistory();
  }, [activeProjectId, client, fetchInsightHistory, insight.data, signal]);

  /* ---------- Top-level guard ---------- */
  if (!activeProjectId) {
    return (
      <EmptyState
        title="请先选择项目"
        description="在右上角选择或创建一个项目后再进行 AI 分析。"
      />
    );
  }

  return (
    <div className="page page-analysis">
      <header className="page-header">
        <span className="eyebrow">分析</span>
        <h1 className="page-title">三阶段 AI 分析</h1>
        <p className="page-subtitle">
          历史来自后端项目级持久化（按 <code className="mono">created_at DESC, id DESC</code> 稳定排序），不写入 sessionStorage；
          三个 Run 互相独立，当前 POST 均为同步执行。
        </p>
      </header>

      <div className="analysis-stepper" role="list">
        <StepperStep index={1} active={!analysis.data} done={Boolean(analysis.data)}>
          分类
        </StepperStep>
        <StepperStep
          index={2}
          active={Boolean(analysis.data) && !cluster.data}
          done={Boolean(cluster.data)}
        >
          聚类
        </StepperStep>
        <StepperStep
          index={3}
          active={Boolean(cluster.data) && !insight.data}
          done={Boolean(insight.data)}
        >
          洞察
        </StepperStep>
      </div>

      {/* ---- Classification ---- */}
      <section className="panel">
        <header className="panel-header">
          <h2 className="section-title">1. 分类（Classification）</h2>
          <span className="panel-hint">POST /projects/&#123;id&#125;/analysis</span>
        </header>
        {analysis.error ? (
          <ErrorState
            message={analysis.error}
            onRetry={() => setAnalysis((s) => ({ ...s, error: null }))}
          />
        ) : null}
        {analysis.data ? (
          <RunCard
            title={`分类 Run · ${analysis.data.id}`}
            status={analysis.data.status}
            startedAt={analysis.data.started_at ?? null}
            finishedAt={analysis.data.completed_at ?? null}
            processedCount={analysis.data.processed_count}
            inputCount={analysis.data.input_count}
            successCount={analysis.data.success_count}
            failedCount={analysis.data.failed_count}
            errorMessage={analysis.data.error_message ?? null}
            actions={
              <>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => void loadAnalysisById(analysis.data!.id)}
                  disabled={analysis.loading}
                >
                  刷新
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void retryAnalysis()}
                  disabled={analysis.submitting}
                >
                  重试
                </button>
              </>
            }
          />
        ) : (
          <div className="run-launch">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void runAnalysis()}
              disabled={analysis.submitting}
            >
              {analysis.submitting ? '启动中…' : '启动分类 Run'}
            </button>
            <span className="run-launch-hint">将对本项目所有反馈运行一次分类</span>
          </div>
        )}
        <form className="run-by-id" onSubmit={handleAnalysisInput}>
          <label className="form-field form-field--inline">
            <span className="form-label">按 Run ID 加载</span>
            <input
              className="form-input"
              type="text"
              value={analysisInput}
              onChange={(e) => setAnalysisInput(e.target.value)}
              placeholder="例如 7b4d..."
            />
          </label>
          <button
            type="submit"
            className="btn btn-secondary btn-sm"
            disabled={!analysisInput.trim() || analysis.loading}
          >
            加载
          </button>
        </form>
        <RunHistoryList<AnalysisRun>
          kind="analysis"
          testIdPrefix="analysis-history"
          history={analysisHistory}
          onPick={(id) => void loadAnalysisById(id)}
          onPageChange={(offset) => {
            /* Update the pageRef synchronously so the inline
             * refetch reads the new offset rather than the
             * pre-click value. The setState below is async
             * and would otherwise leave the fetcher with a
             * stale page for one render. */
            const newPage = { ...analysisPageRef.current, offset };
            analysisPageRef.current = newPage;
            setAnalysisHistory((s) => ({ ...s, page: newPage }));
            void fetchAnalysisHistory();
          }}
          onRetry={() => void fetchAnalysisHistory()}
        />
      </section>

      {/* ---- Clustering ---- */}
      <section className="panel">
        <header className="panel-header">
          <h2 className="section-title">2. 聚类（Clustering）</h2>
          <span className="panel-hint">POST /projects/&#123;id&#125;/clustering</span>
        </header>
        {cluster.error ? (
          <ErrorState
            message={cluster.error}
            onRetry={() => setCluster((s) => ({ ...s, error: null }))}
          />
        ) : null}
        <label className="form-field">
          <span className="form-label">上游分类 Run ID</span>
          <input
            className="form-input"
            type="text"
            value={clusteringAnalysisRunId}
            onChange={(e) => setClusteringAnalysisRunId(e.target.value)}
            placeholder={analysis.data?.id ?? '需要分类 Run ID'}
          />
        </label>
        {cluster.data ? (
          <RunCard
            title={`聚类 Run · ${cluster.data.id}`}
            status={cluster.data.status}
            startedAt={cluster.data.started_at ?? null}
            finishedAt={cluster.data.completed_at ?? null}
            processedCount={cluster.data.processed_count}
            inputCount={cluster.data.input_count}
            successCount={cluster.data.success_count}
            failedCount={cluster.data.failed_count}
            extraCounters={[
              { label: '提示词版本', value: cluster.data.prompt_version },
            ]}
            errorMessage={cluster.data.error_message ?? null}
            actions={
              <>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => void loadClusteringById(cluster.data!.id)}
                  disabled={cluster.loading}
                >
                  刷新
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void retryClustering()}
                  disabled={cluster.submitting}
                >
                  重试
                </button>
              </>
            }
          />
        ) : (
          <div className="run-launch">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void runClustering()}
              disabled={cluster.submitting || !clusteringAnalysisRunId.trim()}
            >
              {cluster.submitting ? '启动中…' : '启动聚类 Run'}
            </button>
          </div>
        )}
        <RunHistoryList<ClusterRun>
          kind="clustering"
          testIdPrefix="clustering-history"
          history={clusterHistory}
          onPick={(id) => void loadClusteringById(id)}
          onPageChange={(offset) => {
            /* See analysis list for the pageRef rationale. */
            const newPage = { ...clusterPageRef.current, offset };
            clusterPageRef.current = newPage;
            setClusterHistory((s) => ({ ...s, page: newPage }));
            void fetchClusterHistory();
          }}
          onRetry={() => void fetchClusterHistory()}
        />
      </section>

      {/* ---- Insight ---- */}
      <section className="panel">
        <header className="panel-header">
          <h2 className="section-title">3. 洞察（Insight）</h2>
          <span className="panel-hint">POST /projects/&#123;id&#125;/insights/generate</span>
        </header>
        {insight.error ? (
          <ErrorState
            message={insight.error}
            onRetry={() => setInsight((s) => ({ ...s, error: null }))}
          />
        ) : null}
        {insight.data ? (
          <RunCard
            title={`洞察 Run · ${insight.data.id}`}
            status={normaliseRunStatus(insight.data.status)}
            startedAt={insight.data.started_at ?? null}
            finishedAt={insight.data.completed_at ?? null}
            processedCount={insight.data.processed_count}
            inputCount={insight.data.input_count}
            successCount={insight.data.success_count}
            failedCount={insight.data.failed_count}
            extraCounters={[
              { label: '需人工复核', value: insight.data.needs_validation_count },
            ]}
            errorMessage={insight.data.error_message ?? null}
            actions={
              <>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => void loadInsightById(insight.data!.id)}
                  disabled={insight.loading}
                >
                  刷新
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void retryInsight()}
                  disabled={insight.submitting}
                >
                  重试
                </button>
              </>
            }
          />
        ) : (
          <div className="run-launch">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void runInsight()}
              disabled={insight.submitting || !cluster.data}
            >
              {insight.submitting ? '启动中…' : '基于当前聚类启动洞察 Run'}
            </button>
            <span className="run-launch-hint">
              需要先获得一个聚类 Run；当前未选择时按钮被禁用。
            </span>
          </div>
        )}
        <RunHistoryList<InsightRun>
          kind="insight"
          testIdPrefix="insight-history"
          history={insightHistory}
          onPick={(id) => void loadInsightById(id)}
          onPageChange={(offset) => {
            /* See analysis list for the pageRef rationale. */
            const newPage = { ...insightPageRef.current, offset };
            insightPageRef.current = newPage;
            setInsightHistory((s) => ({ ...s, page: newPage }));
            void fetchInsightHistory();
          }}
          onRetry={() => void fetchInsightHistory()}
        />
      </section>
    </div>
  );
}

/* ---------- Subcomponents ---------- */

function StepperStep({
  index,
  active,
  done,
  children,
}: {
  index: number;
  active: boolean;
  done: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`stepper-step${active ? ' is-active' : ''}${done ? ' is-done' : ''}`}
      role="listitem"
    >
      <span className="stepper-step-number">{index}</span>
      <span className="stepper-step-label">{children}</span>
    </div>
  );
}

function labelForRunKind(kind: 'analysis' | 'clustering' | 'insight'): string {
  switch (kind) {
    case 'analysis':
      return '分类';
    case 'clustering':
      return '聚类';
    case 'insight':
      return '洞察';
  }
}

/* Generic project-scoped history list. Renders loading / empty /
 * error / retry, plus pagination controls. The page owns the
 * ``history`` state and the ``onPick`` (load-by-id) handler; this
 * component is purely presentational so the three per-kind lists
 * stay in lockstep on the page without duplicating the layout. */
function RunHistoryList<T extends { id: string; status: string; created_at?: string }>({
  kind,
  testIdPrefix,
  history,
  onPick,
  onPageChange,
  onRetry,
}: {
  kind: 'analysis' | 'clustering' | 'insight';
  testIdPrefix: string;
  history: HistoryState<T>;
  onPick: (id: string) => void;
  onPageChange: (offset: number) => void;
  /* Real refetch callback. The previous placeholder
   * ``() => onPageChange(0)`` only worked when the user
   * happened to be on a non-first page; when the page was
   * already at offset=0 the click was a no-op. The page now
   * passes the live fetcher so retry triggers an actual
   * request regardless of the current offset. */
  onRetry: () => void;
}) {
  const { items, total, loading, error, page } = history;
  const pageCount = Math.max(1, Math.ceil(total / page.limit));
  const currentPage = Math.floor(page.offset / page.limit) + 1;
  const listTestId = `${testIdPrefix}-list`;
  const rowTestId = `${testIdPrefix}-row`;
  const paginationTestId = `${testIdPrefix}-pagination`;
  const prevTestId = `${testIdPrefix}-prev`;
  const nextTestId = `${testIdPrefix}-next`;

  return (
    <div className="run-history-block" data-testid={listTestId}>
      <div className="run-history-block-head">
        <h3 className="run-history-block-title">
          {labelForRunKind(kind)}历史 · 共 {total} 条
        </h3>
      </div>
      {loading && items.length === 0 ? (
        <LoadingState />
      ) : error ? (
        <ErrorState
          message={error}
          onRetry={onRetry}
        />
      ) : items.length === 0 ? (
        <p className="run-history-empty">
          该项目下尚无 {labelForRunKind(kind)} Run。
        </p>
      ) : (
        <>
          <ul className="run-history" data-testid={`${listTestId}-items`}>
            {items.map((r) => (
              <li
                key={r.id}
                className="run-history-item"
                data-testid={rowTestId}
                data-run-id={r.id}
              >
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => onPick(r.id)}
                >
                  <code>{r.id}</code>
                  <StatusPill status={normaliseRunStatus(r.status)} />
                  {r.created_at ? (
                    <span className="run-history-time">
                      {formatRelative(r.created_at)}
                    </span>
                  ) : null}
                </button>
              </li>
            ))}
          </ul>
          {total > page.limit ? (
            <footer
              className="pagination"
              data-testid={paginationTestId}
            >
              <span className="pagination-meta">
                第 {currentPage} / {pageCount} 页
              </span>
              <div className="pagination-controls">
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={prevTestId}
                  disabled={currentPage <= 1}
                  onClick={() =>
                    onPageChange(Math.max(0, page.offset - page.limit))
                  }
                >
                  上一页
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={nextTestId}
                  disabled={currentPage >= pageCount}
                  onClick={() => onPageChange(page.offset + page.limit)}
                >
                  下一页
                </button>
              </div>
            </footer>
          ) : null}
        </>
      )}
    </div>
  );
}
