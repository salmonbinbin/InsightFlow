'use client';

/**
 * InsightFlow — Insight Detail page (Phase 7 work package B1).
 *
 * Information hierarchy mirrors the v2 prototype (insight-detail.html):
 *   1. Hero (priority / id / title / lead / cluster members count)
 *   2. Product Insight body (pain point / affected segment / confidence)
 *   3. Evidence (paginated raw feedback — never an AI rewrite)
 *   4. Opportunity Score (snapshot — five-dimension breakdown, configured
 *      vs effective weights, thresholds, AI reasoning; missing dimensions
 *      render as "Pending PM Input" / "Not Available")
 *   5. PM Decision (Accept / Override / Defer / Reject with idempotency
 *      UUID; 409 conflict path refetches and asks the PM to retry)
 *   6. Decision history (drawer; append-only event log)
 *   7. Create-Requirement CTA — gated by all three rules from the brief:
 *        evidence_count > 0 AND status ∈ {accepted, overridden} AND
 *        pm_priority ≠ null. Otherwise the page explains why the CTA is
 *        disabled rather than silently offering it.
 *
 * The page never recomputes Opportunity Score weights, AI priority, or
 * PM priority. Every value surfaced here comes straight from the backend
 * (Phase 4 snapshot).
 */

import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
} from 'react';
import { useProjectContext } from '../../../contexts/ProjectContext';
import { getApiClient } from '../../../lib/api/client';
import { useProjectScopedFetch } from '../../../lib/useProjectScopedFetch';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  NotMeasured,
} from '../../../components/StateViews';
import {
  classifyError,
  formatNumber,
  formatPercent,
  formatRelative,
} from '../../../lib/format';
import type {
  DecisionAction,
  DecisionHistoryEntry,
  DecisionOverrideReason,
  DecisionPatchRequest,
  DecisionPriority,
  Insight,
  InsightDecision,
  InsightEvidenceEntry,
  InsightEvidenceResponse,
  OpportunityScore,
} from '../../../lib/api/types';

const OVERRIDE_REASONS: ReadonlyArray<{ value: DecisionOverrideReason; label: string }> = [
  { value: 'strategic_priority', label: '战略优先级' },
  { value: 'core_user_experience', label: '核心用户体验' },
  { value: 'revenue_impact', label: '收入影响' },
  { value: 'development_cost', label: '开发成本' },
  { value: 'management_decision', label: '管理决策' },
  { value: 'data_quality', label: '数据质量' },
  { value: 'other', label: '其他' },
];

const PRIORITY_OPTIONS: ReadonlyArray<{ value: DecisionPriority; label: string }> = [
  { value: 'P0', label: 'P0 · Critical' },
  { value: 'P1', label: 'P1 · High' },
  { value: 'P2', label: 'P2 · Medium' },
];

type DecisionStatusLabel =
  | 'pending_review'
  | 'accepted'
  | 'overridden'
  | 'deferred'
  | 'rejected';

function normaliseDecisionStatus(raw: string | null | undefined): DecisionStatusLabel {
  if (raw === 'accepted' || raw === 'overridden' || raw === 'deferred' || raw === 'rejected') {
    return raw;
  }
  // Backend's Phase 4 default uses 'pending_review'; older rows / mocks may
  // surface 'pending'. Both map to the same label below.
  return 'pending_review';
}

export default function InsightDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();

  const insightId = params?.id ?? '';

  const [insight, setInsight] = useState<Insight | null>(null);
  const [insightError, setInsightError] = useState<string | null>(null);
  const [insightLoading, setInsightLoading] = useState(false);
  /**
   * B1 fix: when ``getInsight`` returns a row whose ``project_id`` differs
   * from the active project, we surface the cross-project refusal as a
   * distinct state rather than a generic error. This allows the page to
   * render the EmptyState-shaped refusal and prevents the network-level
   * error from clobbering that signal.
   */
  const [insightForeignProject, setInsightForeignProject] = useState<string | null>(null);

  const [evidencePage, setEvidencePage] = useState({
    limit: 25,
    offset: 0,
  });
  const [evidence, setEvidence] = useState<InsightEvidenceResponse | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);

  const [score, setScore] = useState<OpportunityScore | null>(null);
  const [scoreError, setScoreError] = useState<string | null>(null);
  const [scoreLoading, setScoreLoading] = useState(false);
  const [scoreMissing, setScoreMissing] = useState(false);

  const [decision, setDecision] = useState<InsightDecision | null>(null);
  const [decisionMissing, setDecisionMissing] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [decisionBusy, setDecisionBusy] = useState(false);

  /* Phase 7 B1 fix — P0 unblock. The backend PATCH
   * /insights/{id}/decision with action='set_business_value' is
   * the ONLY way to break out of the ``total_score=null``
   * deadlock (accept/override/defer/reject all require a complete
   * score snapshot). ``bvInput`` is the local controlled value of
   * the input; ``bvInputError`` is the inline validation message
   * surfaced before we even touch the network. We keep BV as a
   * string so the user can clear the field without forcing a
   * non-numeric placeholder. */
  const [bvInput, setBvInput] = useState<string>('');
  const [bvInputError, setBvInputError] = useState<string | null>(null);

  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<DecisionHistoryEntry[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overridePriority, setOverridePriority] =
    useState<DecisionPriority>('P0');
  const [overrideReason, setOverrideReason] =
    useState<DecisionOverrideReason>('strategic_priority');
  const [overrideNote, setOverrideNote] = useState('');

  const [conflictModal, setConflictModal] = useState<{
    message: string;
  } | null>(null);

  const [requirementBusy, setRequirementBusy] = useState(false);
  const [requirementError, setRequirementError] = useState<string | null>(null);
  const [createdRequirementId, setCreatedRequirementId] = useState<string | null>(null);

  /* ---------- Project isolation ----------
   *
   * Phase 7 B1 fix: when ``getInsight`` returns an Insight whose
   * ``project_id`` differs from the user's active project, we refuse to
   * surface ANY of its data. The previous code happily rendered the
   * response (title, body, evidence, score, decision) for a foreign
   * project, AND kept decision-action buttons enabled — anyone with a
   * shared insight id could mutate a different project's decision row.
   *
   * Two layers of defence:
   *   1. ``fetchInsight`` checks ``res.data.project_id === activeProjectId``
   *      before calling ``setInsight`` and surfaces a clear cross-project
   *      error state.
   *   2. ``sendDecision`` and the requirement CTA read the same flag
   *      directly so they can refuse to fire even if the fetch somehow
   *      raced through (defence in depth against a stale closure).
   */

  const liveInsightProjectMismatch = useMemo(() => {
    if (!insight || !activeProjectId) return false;
    return insight.project_id !== activeProjectId;
  }, [insight, activeProjectId]);

  /* ---------- Fetchers ---------- */

  const fetchInsight = useCallback(async () => {
    if (!insightId || !signal || !activeProjectId) return;
    // Capture the signal for the post-await check below; ``signal`` is
    // the per-project AbortSignal exposed by useProjectScopedFetch. When
    // the user switches projects mid-flight, the OLD controller is
    // aborted before the NEW signal is published — without this guard a
    // late-arriving response from project A would overwrite project B's
    // view (a classic "stale fetch wins" bug).
    const mySignal = signal;
    const myProject = activeProjectId;
    setInsightLoading(true);
    setInsightError(null);
    const res = await client.getInsight(insightId, mySignal);
    if (mySignal.aborted) return;
    if (res.error || !res.data) {
      setInsightError(res.error ? classifyError(res.error) : '未找到洞察。');
      setInsight(null);
      setInsightLoading(false);
      return;
    }
    // Project isolation guard: refuse to surface a foreign-project Insight.
    // We do NOT trust ``setInsight`` here — a network-level race could have
    // delivered project A's row after the user already moved to project B.
    if (res.data.project_id !== myProject) {
      setInsightForeignProject(res.data.project_id ?? '未知项目');
      setInsight(null);
      setInsightError(null);
      setInsightLoading(false);
      return;
    }
    setInsightForeignProject(null);
    setInsight(res.data);
    setInsightLoading(false);
  }, [activeProjectId, client, insightId, signal]);

  const fetchEvidence = useCallback(async () => {
    if (!insightId || !signal) return;
    const mySignal = signal;
    setEvidenceLoading(true);
    setEvidenceError(null);
    const res = await client.getInsightEvidence(insightId, {
      limit: evidencePage.limit,
      offset: evidencePage.offset,
    }, mySignal);
    if (mySignal.aborted) return;
    if (res.error || !res.data) {
      setEvidenceError(res.error ? classifyError(res.error) : '证据加载失败。');
      setEvidence(null);
      setEvidenceLoading(false);
      return;
    }
    setEvidence(res.data);
    setEvidenceLoading(false);
  }, [client, evidencePage.limit, evidencePage.offset, insightId, signal]);

  const fetchOpportunityScore = useCallback(async () => {
    if (!insightId || !signal) return;
    const mySignal = signal;
    setScoreLoading(true);
    setScoreError(null);
    setScoreMissing(false);
    const res = await client.getOpportunityScore(insightId, mySignal);
    if (mySignal.aborted) return;
    if (res.error && res.error.status === 404) {
      // Phase 4 snapshot has not been generated yet. Distinguish from real
      // errors so the UI can show "Pending PM Input" rather than an error.
      setScore(null);
      setScoreMissing(true);
      setScoreError(null);
      setScoreLoading(false);
      return;
    }
    if (res.error || !res.data) {
      setScoreError(res.error ? classifyError(res.error) : '评分加载失败。');
      setScore(null);
      setScoreLoading(false);
      return;
    }
    setScore(res.data);
    setScoreLoading(false);
  }, [client, insightId, signal]);

  const refetchDecision = useCallback(async () => {
    if (!insightId || !signal) return;
    const mySignal = signal;
    const res = await client.getDecision(insightId, mySignal);
    if (mySignal.aborted) return;
    if (res.error && res.error.status === 404) {
      setDecision(null);
      setDecisionMissing(true);
      return;
    }
    if (res.error || !res.data) {
      setDecisionError(res.error ? classifyError(res.error) : '决策加载失败。');
      return;
    }
    setDecision(res.data);
    setDecisionMissing(false);
  }, [client, insightId, signal]);

  const fetchHistory = useCallback(async () => {
    if (!insightId || !signal) return;
    const mySignal = signal;
    setHistoryLoading(true);
    setHistoryError(null);
    const res = await client.getDecisionHistory(insightId, { limit: 50, offset: 0 }, mySignal);
    if (mySignal.aborted) return;
    if (res.error || !res.data) {
      setHistoryError(res.error ? classifyError(res.error) : '历史加载失败。');
      setHistory([]);
      setHistoryLoading(false);
      return;
    }
    // Backend returns the standard paginated envelope
    //   { items: DecisionHistoryEntry[], total, limit, offset }.
    setHistory(res.data.items);
    setHistoryLoading(false);
  }, [client, insightId, signal]);

  /* ---------- Wire up ---------- */

  // B1 fix: when the active project changes (including the very first time
  // a project is selected), clear every piece of state we own so we never
  // render stale rows from the previous project. The hook's signal-based
  // abort already cancels in-flight requests; this effect additionally
  // wipes any data that may have landed BEFORE the abort fired (e.g.
  // JSON already in flight when the controller was aborted). The signal
  // change alone does NOT do that — fetchers only see ``signal.aborted``
  // after the fetch resolves, not at the moment of the project switch.
  useEffect(() => {
    setInsight(null);
    setInsightError(null);
    setInsightLoading(false);
    setInsightForeignProject(null);
    setEvidence(null);
    setEvidenceError(null);
    setEvidenceLoading(false);
    setScore(null);
    setScoreError(null);
    setScoreLoading(false);
    setScoreMissing(false);
    setDecision(null);
    setDecisionMissing(false);
    setDecisionError(null);
    setDecisionBusy(false);
    setHistoryOpen(false);
    setHistory([]);
    setHistoryError(null);
    setHistoryLoading(false);
    setOverrideOpen(false);
    setOverridePriority('P0');
    setOverrideReason('strategic_priority');
    setOverrideNote('');
    setConflictModal(null);
    setRequirementBusy(false);
    setRequirementError(null);
    setCreatedRequirementId(null);
    setBvInput('');
    setBvInputError(null);
    // evidencePage offsets are project-scoped too — reset to first page.
    setEvidencePage({ limit: 25, offset: 0 });
    // We DO list activeProjectId in deps. The hook fires on every project
    // change INCLUDING the initial null→first-project, which is exactly
    // what we want — a fresh project always means a fresh start.
  }, [activeProjectId]);

  useEffect(() => {
    void fetchInsight();
    void fetchEvidence();
    void fetchOpportunityScore();
    void refetchDecision();
  }, [fetchInsight, fetchEvidence, fetchOpportunityScore, refetchDecision]);

  /* ---------- Derived ----------
   *
   * Must be declared BEFORE any useCallback in the Actions section that
   * references them. The previous order put ``Derived`` after
   * ``createRequirementClick`` and caused a TDZ when the click handler's
   * dependency array was evaluated: useCallback evaluates its deps at
   * call time, so referencing ``ctaGate.enable`` before its
   * ``useMemo`` had run produced a ReferenceError that broke every
   * render.
   */

  const decisionStatus = useMemo<DecisionStatusLabel>(() => {
    return normaliseDecisionStatus(decision?.status ?? 'pending_review');
  }, [decision?.status]);

  const ctaGate = useMemo(() => {
    const evidenceOk = (insight?.evidence_count ?? 0) > 0;
    const accepted =
      decisionStatus === 'accepted' || decisionStatus === 'overridden';
    const pmPriority = decision?.pm_priority ?? null;
    return {
      evidenceOk,
      accepted,
      pmPriority,
      enable: evidenceOk && accepted && pmPriority !== null,
    };
  }, [decision?.pm_priority, decisionStatus, insight?.evidence_count]);

  /* ---------- Actions ---------- */

  const sendDecision = useCallback(
    async (
      action: DecisionAction,
      extras: Partial<DecisionPatchRequest> = {},
    ): Promise<boolean> => {
      if (!insightId) return false;
      // B1 fix: refuse to send if the loaded insight does not belong to the
      // active project. Even though ``fetchInsight`` already bails out on
      // mismatch, this is the second line of defence against a stale
      // closure that captured the insight before the user switched.
      if (liveInsightProjectMismatch || !insight) {
        setDecisionError('当前洞察不属于本项目，无法提交决策。');
        return false;
      }
      const mySignal = signal;
      setDecisionBusy(true);
      setDecisionError(null);

      // Idempotency: every PATCH must carry a client-generated UUID. The
      // backend uses (insight_id, request_id) as the dedup key, so retries
      // with the same uuid are safe. We always mint a fresh uuid per click
      // because each click is a distinct user intent.
      //
      // B1 fix: the backend REJECTS any non-UUID request_id (422 from
      // pydantic). The previous ``req_<timestamp>_<rand>`` fallback was a
      // landmine waiting for a browser that lacks ``crypto.randomUUID``
      // (older Safari on iOS < 15.4, for example). We now refuse to send
      // and surface a clear error rather than forge an illegal id.
      let requestId: string;
      try {
        requestId = mintRequestId();
      } catch (e) {
        setDecisionBusy(false);
        setDecisionError(
          e instanceof Error
            ? e.message
            : '当前环境无法生成请求 ID，请刷新或更换浏览器。',
        );
        return false;
      }

      const body: DecisionPatchRequest = {
        request_id: requestId,
        expected_version: decision?.version ?? 0,
        action,
        ...extras,
      };

      const res = await client.patchDecision(insightId, body, mySignal ?? undefined);
      if (mySignal?.aborted) {
        // Project switched mid-flight: drop the response, do not touch state.
        setDecisionBusy(false);
        return false;
      }
      setDecisionBusy(false);

      if (res.error) {
        if (res.error.status === 409) {
          // Optimistic-concurrency conflict. Refetch state, surface a clear
          // message so the user can decide whether to retry.
          setConflictModal({
            message:
              'Decision 被另一端更新，版本不匹配。已刷新当前状态，请重新审阅再提交。',
          });
          await refetchDecision();
          return false;
        }
        setDecisionError(classifyError(res.error));
        return false;
      }
      if (res.data) {
        setDecision(res.data);
        setDecisionMissing(false);
      }
      void refetchDecision();
      return true;
    },
    [
      client,
      decision?.version,
      insight,
      insightId,
      liveInsightProjectMismatch,
      refetchDecision,
      signal,
    ],
  );

  const acceptDecision = useCallback(async () => {
    await sendDecision('accept', {
      pm_priority: (decision?.ai_priority as DecisionPriority | null) ?? null,
    });
  }, [decision?.ai_priority, sendDecision]);

  const rejectDecision = useCallback(async () => {
    await sendDecision('reject', { pm_priority: null });
  }, [sendDecision]);

  const deferDecision = useCallback(async () => {
    await sendDecision('defer', { pm_priority: null });
  }, [sendDecision]);

  const submitOverride = useCallback(async () => {
    const ok = await sendDecision('override', {
      pm_priority: overridePriority,
      override_reason: overrideReason,
      override_note: overrideNote.trim() || null,
    });
    if (ok) {
      setOverrideOpen(false);
      setOverrideNote('');
    }
  }, [overrideNote, overridePriority, overrideReason, sendDecision]);

  /**
   * Phase 7 B1 fix — P0 unblock for PM Business Value.
   *
   * Validates the locally-typed input is a real number in
   * ``[0, 10]`` and then routes through ``sendDecision`` so we
   * inherit its full safety net: project-isolation gate
   * (``liveInsightProjectMismatch``), idempotency
   * (``mintRequestId`` UUID v4), ``expected_version`` snapshot,
   * AbortSignal post-check, 409 conflict refetch + modal, and the
   * shared ``decisionBusy / decisionError`` surface.
   *
   * The backend PATCH with ``action='set_business_value'``
   * atomically recomputes ``total_score`` / ``ai_priority`` /
   * configured + effective weights in the same transaction, so
   * on success we explicitly refetch the Opportunity Score —
   * otherwise the page keeps showing the stale
   * ``total_score=null`` row that triggered this P0 in the first
   * place. Other decision actions (accept / override / defer /
   * reject) deliberately do NOT refetch the score: they don't
   * touch it.
   */
  const submitBusinessValue = useCallback(async () => {
    const trimmed = bvInput.trim();
    if (trimmed === '') {
      setBvInputError('请填写 PM Business Value（0..10）。');
      return;
    }
    const n = Number(trimmed);
    if (!Number.isFinite(n) || n < 0 || n > 10) {
      setBvInputError('Business Value 必须在 0..10 之间。');
      return;
    }
    setBvInputError(null);
    const ok = await sendDecision('set_business_value', {
      business_value: n,
    });
    if (ok) {
      // Backend recomputes the snapshot in the same PATCH — pull
      // the fresh row so the breakdown / ai_priority / configured
      // + effective weights no longer read as ``null``.
      void fetchOpportunityScore();
    }
  }, [bvInput, fetchOpportunityScore, sendDecision]);

  const openHistory = useCallback(async () => {
    setHistoryOpen(true);
    await fetchHistory();
  }, [fetchHistory]);

  /**
   * Cross-project refusal gate. The CTA may ONLY fire when
   *   insight.project_id === activeProjectId.
   * ``liveInsightProjectMismatch`` already holds this truth; we re-read
   * it here so the click handler can refuse synchronously without
   * touching the network.
   */
  const createRequirementClick = useCallback(async () => {
    if (!ctaGate.enable) return;
    if (!insight || liveInsightProjectMismatch || !activeProjectId) {
      setRequirementError('当前洞察不属于本项目，无法创建 Requirement。');
      return;
    }
    const mySignal = signal;
    setRequirementBusy(true);
    setRequirementError(null);
    const res = await client.createRequirement(insight.id, mySignal ?? undefined);
    if (mySignal?.aborted) {
      setRequirementBusy(false);
      return;
    }
    setRequirementBusy(false);
    if (res.error || !res.data) {
      setRequirementError(
        res.error ? classifyError(res.error) : '创建 Requirement 失败。',
      );
      return;
    }
    // B2 will mount the /requirements/{id} detail page; the route
    // 404s today (acceptable per B1 spec). On success we navigate
    // and surface the requirement id for the next step.
    setCreatedRequirementId(res.data.id);
    router.push(`/requirements/${res.data.id}`);
  }, [
    activeProjectId,
    client,
    ctaGate.enable,
    insight,
    liveInsightProjectMismatch,
    router,
    signal,
  ]);

  /* ---------- Derived (render-time helpers) ---------- */

  const evidencePageCount = useMemo(() => {
    if (!evidence) return 1;
    return Math.max(1, Math.ceil(evidence.total / evidencePage.limit));
  }, [evidence, evidencePage.limit]);

  const evidenceCurrentPage =
    Math.floor(evidencePage.offset / evidencePage.limit) + 1;

  /* ---------- Render ---------- */

  if (!activeProjectId) {
    return <EmptyState title="请先选择项目" description="在右上角选择或创建一个项目。" />;
  }

  if (insightLoading && !insight) return <LoadingState />;
  if (insightError && !insight) {
    return (
      <ErrorState
        message={insightError}
        onRetry={() => {
          setInsightError(null);
          void fetchInsight();
        }}
      />
    );
  }
  if (!insight) {
    // Cross-project guard: the page refuses to load a foreign-project row
    // past this point, so we render a labelled EmptyState instead of a
    // plain spinner. The actual loading spinner has already been handled
    // above by the ``insightLoading && !insight`` branch.
    if (insightForeignProject) {
      return (
        <EmptyState
          title="该洞察不属于当前项目"
          description={`该洞察属于项目 ${insightForeignProject}，与当前激活项目不符。请在项目切换器中选择对应项目后再查看。`}
        />
      );
    }
    return <LoadingState />;
  }

  // B1 fix: if the loaded insight belongs to another project, refuse to
  // render the entire detail surface. The state was already nulled in the
  // project-switch effect above; this only fires when the fetch completed
  // AFTER the switch and slipped past the signal abort (a real race that
  // the signal alone cannot close — the response body may have arrived
  // before the controller was aborted server-side).
  if (liveInsightProjectMismatch) {
    return (
      <EmptyState
        title="该洞察不属于当前项目"
        description={`该洞察 (${insight.id.slice(0, 8)}…) 属于项目 ${insight.project_id}，与当前激活项目不符。请在项目切换器中选择对应项目后再查看。`}
      />
    );
  }

  return (
    <div className="page page-insight-detail">
      <header className="page-header">
        <span className="eyebrow">洞察详情</span>
        <h1 className="page-title">{insight.title ?? '尚无标题'}</h1>
        <p className="page-subtitle">
          来源 run <code className="mono">{insight.run_id.slice(0, 8)}</code> · 聚类
          <code className="mono"> {insight.source_cluster_external_id.slice(0, 8)}</code>
        </p>
      </header>

      <section className="section hero" data-od-id="hero">
        <div className="hero-tags">
          <span className="badge badge-accent">
            {insight.status === 'needs_validation' ? 'Needs Validation' : 'Draft'}
          </span>
          <span className="badge badge-outline mono">{insight.id.slice(0, 8)}</span>
          <span className="badge badge-ai">AI-generated · 可追溯</span>
          {decision ? (
            <span
              className={`badge ${
                decisionStatus === 'pending_review'
                  ? 'badge-warn'
                  : decisionStatus === 'rejected'
                    ? 'badge-danger'
                    : decisionStatus === 'accepted' || decisionStatus === 'overridden'
                      ? 'badge-success'
                      : 'badge-outline'
              }`}
            >
              PM {decisionStatus === 'pending_review' ? '待复核' : DECISION_LABEL[decisionStatus]}
            </span>
          ) : null}
        </div>
        <p className="lead">{insight.affected_segment ?? '受影响人群尚未识别'}</p>
        <div className="hero-meta">
          <span>
            置信度 <b className="mono">{formatPercent(insight.confidence)}</b>
          </span>
          <span>反馈 <b className="mono">{formatNumber(insight.feedback_count)}</b></span>
          <span>证据 <b className="mono">{formatNumber(insight.evidence_count)}</b></span>
          <span>更新 <b className="mono">{formatRelative(insight.created_at ?? null)}</b></span>
        </div>
      </section>

      {/* Product Insight body */}
      <section className="section" data-od-id="product-insight">
        <header className="section-head">
          <h3>Product Insight</h3>
          <span className="badge badge-ai">AI-generated</span>
        </header>
        <blockquote
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 18,
            lineHeight: 1.55,
            color: 'var(--fg)',
            padding: '14px 16px',
            background: 'var(--surface-1)',
            borderLeft: '3px solid var(--accent)',
            borderRadius: 'var(--r-md)',
            maxWidth: '80ch',
          }}
        >
          {insight.insight ?? '（尚无 AI 推断正文 — 可能 needs_validation）'}
        </blockquote>
        <div className="meta-grid">
          <div className="meta-item">
            <div className="meta-k">Pain Point</div>
            <div className="meta-v">{insight.pain_point ?? <NotMeasured />}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">Affected Segment</div>
            <div className="meta-v">{insight.affected_segment ?? <NotMeasured />}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">Recommendation</div>
            <div className="meta-v">{insight.recommendation ?? <NotMeasured />}</div>
          </div>
        </div>
      </section>

      {/* Evidence */}
      <section className="section" data-od-id="evidence">
        <header className="section-head">
          <h3>Evidence · {evidence?.total ?? 0}</h3>
          <span className="muted" style={{ fontSize: 'var(--t-caption)' }}>
            直接读自 <code className="mono">feedbacks.content</code>，绝无 AI 改写。
          </span>
        </header>

        {evidenceLoading && !evidence ? (
          <LoadingState />
        ) : evidenceError ? (
          <ErrorState
            message={evidenceError}
            onRetry={() => {
              setEvidenceError(null);
              void fetchEvidence();
            }}
          />
        ) : evidence && evidence.items.length === 0 ? (
          <EmptyState
            title="尚无证据"
            description="本 Insight 尚未生成代表反馈。可在分析页对此 Run 重试。"
          />
        ) : evidence ? (
          <ul className="evidence-list" data-testid="evidence-list">
            {evidence.items.map((ev) => (
              <li key={ev.id} className="evidence-row">
                <div className="evidence-id mono">{ev.feedback_external_id}</div>
                <div className="evidence-body">
                  <div className="evidence-quote">
                    <span className="raw-quote-inline">
                      {ev.feedback_content || <NotMeasured label="原文为空" />}
                    </span>
                  </div>
                  <div className="evidence-meta">
                    <span>来源 {ev.feedback_source ?? <NotMeasured label="—" />}</span>
                    <span>用户 <span className="mono">{ev.feedback_user_id ?? '—'}</span></span>
                    <span>证据类型 <span className="mono">{ev.evidence_type}</span></span>
                    <span>相关度 <span className="mono">{formatNumber(ev.relevance_score)}</span></span>
                    <span>{formatRelative(ev.feedback_created_at ?? null)}</span>
                  </div>
                  {ev.feedback_analysis ? (
                    <div className="evidence-ai" data-testid="evidence-ai">
                      <span className="badge badge-ai">AI Analysis</span>
                      <span>
                        类型 <b className="mono">{ev.feedback_analysis.feedback_type}</b>
                      </span>
                      <span>
                        模块 <b className="mono">{ev.feedback_analysis.module}</b>
                      </span>
                      <span>
                        意图 <b className="mono">{ev.feedback_analysis.intent}</b>
                      </span>
                      <span>
                        情感 <b className="mono">{ev.feedback_analysis.sentiment}</b>
                      </span>
                      <span>
                        严重度 <b className="mono">{ev.feedback_analysis.severity}/5</b>
                      </span>
                      <span>
                        置信度{' '}
                        <b className="mono">
                          {formatNumber(ev.feedback_analysis.confidence)}
                        </b>
                      </span>
                    </div>
                  ) : (
                    <div className="evidence-ai evidence-ai--empty" data-testid="evidence-ai-empty">
                      <span className="badge badge-outline">AI Analysis</span>
                      <span className="muted">未生成（classification_run 无对应 FeedbackAnalysis）</span>
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        ) : null}

        {evidence && evidence.total > evidencePage.limit ? (
          <footer className="pagination">
            <span className="pagination-meta">
              共 {evidence.total} 条证据 · 第 {evidenceCurrentPage} / {evidencePageCount} 页
            </span>
            <div className="pagination-controls">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={evidenceCurrentPage <= 1}
                onClick={() =>
                  setEvidencePage((p) => ({
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
                disabled={evidenceCurrentPage >= evidencePageCount}
                onClick={() =>
                  setEvidencePage((p) => ({
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

      {/* Opportunity Score */}
      <section className="section" data-od-id="opportunity-score">
        <header className="section-head">
          <h3>Opportunity Score · Snapshot</h3>
          {score ? (
            <span className="muted mono" style={{ fontSize: 'var(--t-caption)' }}>
              Calculation {score.calculation_version} ·
              模型 <span className="mono">{score.model ?? '—'}</span>
            </span>
          ) : null}
        </header>
        {/* Phase 7 B1 fix — PM Business Value input. The backend
         * requires a complete score snapshot before
         * accept / override / defer / reject can be applied, and
         * the only path that produces one is
         * ``action='set_business_value'`` (which atomically
         * recomputes ``total_score`` / ``ai_priority`` / weights
         * in the same PATCH). The form is rendered whenever the
         * section isn't in a transient loading / error state, so
         * the user can either create the very first snapshot
         * (``scoreMissing === true``) or edit an existing one. */}
        {!scoreLoading && !scoreError ? (
          <div
            className="bv-form"
            data-testid="bv-form"
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              alignItems: 'flex-end',
              gap: 12,
              marginBottom: 16,
            }}
          >
            <label
              className="form-field form-field--inline"
              htmlFor="bv-input"
              style={{ marginBottom: 0 }}
            >
              <span className="form-label">PM Business Value（0..10）</span>
              <input
                id="bv-input"
                type="number"
                className="form-input"
                min={0}
                max={10}
                step={0.1}
                placeholder="0..10"
                value={bvInput}
                onChange={(e) => {
                  setBvInput(e.target.value);
                  if (bvInputError) setBvInputError(null);
                }}
                disabled={decisionBusy}
                data-testid="bv-input"
              />
            </label>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => void submitBusinessValue()}
              disabled={decisionBusy || bvInput.trim() === ''}
              data-testid="bv-submit"
            >
              保存 Business Value
            </button>
            {bvInputError ? (
              <span
                className="bv-form-error"
                role="alert"
                style={{
                  color: 'var(--danger)',
                  fontSize: 'var(--t-caption)',
                }}
              >
                {bvInputError}
              </span>
            ) : null}
          </div>
        ) : null}

        {scoreLoading && !score ? (
          <LoadingState />
        ) : scoreError ? (
          <ErrorState
            message={scoreError}
            onRetry={() => {
              setScoreError(null);
              void fetchOpportunityScore();
            }}
          />
        ) : scoreMissing ? (
          <EmptyState
            title="尚未生成 Opportunity Score"
            description="请先在上方「PM Business Value」输入 0..10 之间的数值并保存——后端会在同一次 PATCH 中原子重算总分、AI priority 与权重，从而解锁后续 Accept / Override / Defer / Reject。"
            action={
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={async () => {
                  const mySignal = signal;
                  const res = await client.createOpportunityScore(insightId, mySignal ?? undefined);
                  if (mySignal?.aborted) return;
                  if (res.error) {
                    setScoreError(classifyError(res.error));
                    return;
                  }
                  setScore(res.data);
                  setScoreMissing(false);
                }}
              >
                生成预览评分（BV 留空，仅查看结构）
              </button>
            }
          />
        ) : score ? (
          <OpportunityScoreBody score={score} />
        ) : null}
      </section>

      {/* PM Decision */}
      <section className="section" data-od-id="decision">
        <header className="section-head">
          <h3>Product Decision · Human-in-the-loop</h3>
          <span className="badge badge-ai">AI 建议 + PM 终裁</span>
        </header>

        <div className="decision-panel">
          <div className="decision-box ai">
            <div className="decision-label">AI Recommendation</div>
            <div className="decision-value">
              <span className="badge badge-accent">
                {score?.ai_priority ?? decision?.ai_priority ?? <NotMeasured label="—" />}
              </span>
              <span style={{ marginLeft: 12, color: 'var(--ai-fg)' }}>
                {score?.ai_reasoning ?? 'AI 评分尚未生成，无独立建议；可直接由 PM 决策。'}
              </span>
            </div>
          </div>
          <div className="decision-box">
            <div className="decision-label">PM Decision</div>
            {decisionMissing ? (
              <div className="decision-value muted" style={{ fontSize: 'var(--t-small)' }}>
                尚未提交 PM 决策。下方按钮可立即生效。
              </div>
            ) : decision ? (
              <div className="decision-value">
                <div>
                  状态 <b className="mono">{decisionStatus}</b>
                  {decision.pm_priority ? (
                    <>
                      {' '}· 优先级 <span className="badge badge-accent">{decision.pm_priority}</span>
                    </>
                  ) : null}
                  {decision.override_reason ? (
                    <>
                      {' '}· 原因 <span className="mono">{decision.override_reason}</span>
                    </>
                  ) : null}
                </div>
                {decision.override_note ? (
                  <div className="muted" style={{ marginTop: 6, fontSize: 'var(--t-small)' }}>
                    {decision.override_note}
                  </div>
                ) : null}
                <div className="muted mono" style={{ marginTop: 6, fontSize: 'var(--t-caption)' }}>
                  version v{decision.version} · 更新 {formatRelative(decision.updated_at ?? null)}
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {decisionError ? (
          <ErrorState
            title="决策操作失败"
            message={decisionError}
            onRetry={() => {
              setDecisionError(null);
            }}
          />
        ) : null}

        <div className="decision-actions" data-testid="decision-actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => void acceptDecision()}
            disabled={decisionBusy || decisionStatus === 'accepted'}
            data-action="accept"
          >
            Accept
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => {
              setOverridePriority(
                (decision?.pm_priority as DecisionPriority | null) ??
                  (score?.ai_priority as DecisionPriority | null) ??
                  'P1',
              );
              setOverrideOpen(true);
            }}
            disabled={decisionBusy}
            data-action="override"
          >
            Override
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void deferDecision()}
            disabled={decisionBusy || decisionStatus === 'deferred'}
            data-action="defer"
          >
            Defer
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => void rejectDecision()}
            disabled={decisionBusy || decisionStatus === 'rejected'}
            data-action="reject"
          >
            Reject
          </button>
          <span style={{ flex: 1 }} />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void openHistory()}
            data-testid="open-history"
          >
            查看决策历史
          </button>
        </div>
      </section>

      {/* CTA — Create Requirement gate */}
      <section
        className={`cta-bar${ctaGate.enable ? '' : ' disabled'}`}
        data-od-id="cta"
        data-testid="cta-bar"
        data-disabled={ctaGate.enable ? 'false' : 'true'}
      >
        <div>
          <h4>
            {ctaGate.enable
              ? '下一步：创建 Requirement'
              : decisionStatus === 'pending_review'
                ? '需要先完成 PM Decision'
                : decisionStatus === 'deferred'
                  ? 'Insight 已 Deferred'
                  : decisionStatus === 'rejected'
                    ? 'Insight 已 Rejected'
                    : '尚不能创建 Requirement'}
          </h4>
          <p>
            {ctaGate.enable
              ? '将该 Insight 转化为可工程化的 Requirement，并自动生成 PRD 草案。'
              : !ctaGate.evidenceOk
                ? `需要至少 1 条证据（当前 evidence_count = ${
                    insight.evidence_count
                  }）。请扩充 cluster 或重试 Insight。`
                : !ctaGate.accepted
                  ? 'PM Decision 必须为 Accept / Override 才能创建 Requirement。'
                  : 'PM 优先级缺失，无法创建 Requirement。请用 Override 指定。'}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            type="button"
            className="btn btn-primary btn-lg"
            disabled={!ctaGate.enable || requirementBusy || liveInsightProjectMismatch}
            onClick={() => void createRequirementClick()}
            data-testid="create-requirement"
            aria-disabled={!ctaGate.enable || liveInsightProjectMismatch}
          >
            Create Requirement →
          </button>
          <Link className="btn btn-ghost btn-sm" href="/insights">
            返回列表
          </Link>
        </div>
      </section>

      {requirementError ? (
        <ErrorState
          title="创建 Requirement 失败"
          message={requirementError}
          onRetry={() => setRequirementError(null)}
        />
      ) : null}
      {createdRequirementId ? (
        <div className="alert alert-info" role="status" data-testid="requirement-created">
          Requirement 已创建（{createdRequirementId.slice(0, 8)}…），详情页路由由后续工作包实现。
        </div>
      ) : null}

      {/* Decision history drawer */}
      {historyOpen ? (
        <div
          className="drawer-backdrop open"
          role="presentation"
          onClick={() => setHistoryOpen(false)}
        >
          <aside
            className="drawer open"
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="drawer-head">
              <span className="drawer-title">决策历史 · {insight.id.slice(0, 8)}</span>
              <span style={{ flex: 1 }} />
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setHistoryOpen(false)}
              >
                关闭
              </button>
            </div>
            <div className="drawer-body">
              {historyLoading ? (
                <LoadingState />
              ) : historyError ? (
                <ErrorState
                  message={historyError}
                  onRetry={() => void fetchHistory()}
                />
              ) : history.length === 0 ? (
                <EmptyState title="尚无历史" description="本次 Insight 暂无 PM 操作事件。" />
              ) : (
                <ul className="history-list">
                  {history.map((h) => (
                    <li key={h.id} className="history-row">
                      <div className="history-row-head">
                        <span className="badge badge-accent mono">{h.event_type}</span>
                        <span className="muted mono">v{h.version_after}</span>
                        <span className="muted">{formatRelative(h.created_at ?? null)}</span>
                      </div>
                      {h.pm_priority || h.ai_priority ? (
                        <div className="muted" style={{ fontSize: 'var(--t-small)' }}>
                          {h.ai_priority ? `AI priority: ${h.ai_priority}` : null}
                          {h.pm_priority ? ` · PM priority: ${h.pm_priority}` : null}
                          {h.override_reason ? ` · 原因: ${h.override_reason}` : null}
                          {h.override_note ? ` · 备注: ${h.override_note}` : null}
                        </div>
                      ) : null}
                      <code className="muted mono" style={{ fontSize: 'var(--t-caption)' }}>
                        request_id {h.request_id.slice(0, 8)}…
                      </code>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </aside>
        </div>
      ) : null}

      {/* Override modal */}
      {overrideOpen ? (
        <div
          className="drawer-backdrop open"
          role="presentation"
          onClick={() => setOverrideOpen(false)}
        >
          <aside
            className="modal open"
            role="dialog"
            aria-modal="true"
            style={MODAL_STYLE}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-head">
              <h3>Override PM Priority</h3>
              <p>覆盖 AI 推荐的优先级，并记录 PM 决策原因。该操作会增加 Decision version。</p>
            </div>
            <div className="modal-body">
              <label className="form-field">
                <span className="form-label">目标优先级</span>
                <select
                  className="form-select"
                  value={overridePriority}
                  onChange={(e) =>
                    setOverridePriority(e.target.value as DecisionPriority)
                  }
                >
                  {PRIORITY_OPTIONS.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="form-field">
                <span className="form-label">Override 原因（必填）</span>
                <select
                  className="form-select"
                  value={overrideReason}
                  onChange={(e) =>
                    setOverrideReason(e.target.value as DecisionOverrideReason)
                  }
                >
                  {OVERRIDE_REASONS.map((r) => (
                    <option key={r.value} value={r.value}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="form-field">
                <span className="form-label">备注（可选）</span>
                <textarea
                  className="form-input"
                  rows={3}
                  value={overrideNote}
                  onChange={(e) => setOverrideNote(e.target.value)}
                  maxLength={2000}
                  placeholder="记录 PM 决策依据，便于下游 Requirement 引用。"
                />
              </label>
            </div>
            <div className="modal-foot">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setOverrideOpen(false)}
              >
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => void submitOverride()}
                disabled={decisionBusy}
                data-testid="submit-override"
              >
                提交 Override
              </button>
            </div>
          </aside>
        </div>
      ) : null}

      {/* Conflict 409 modal */}
      {conflictModal ? (
        <div className="drawer-backdrop open" role="presentation">
          <aside className="modal open" role="alertdialog" style={MODAL_STYLE}>
            <div className="modal-head">
              <h3>Decision 版本冲突</h3>
              <p>{conflictModal.message}</p>
            </div>
            <div className="modal-foot">
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => setConflictModal(null)}
              >
                已了解
              </button>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

const DECISION_LABEL: Record<DecisionStatusLabel, string> = {
  pending_review: '待复核',
  accepted: '已采纳',
  overridden: '已重排',
  deferred: '已延后',
  rejected: '已驳回',
};

const MODAL_STYLE: CSSProperties = {
  width: 480,
  maxWidth: '92vw',
};

interface ScoreBodyProps {
  score: OpportunityScore;
}

/**
 * Render the five-dimension breakdown exactly as the backend returns it.
 * No recalculation, no rounding on the frontend — the snapshot is the
 * source of truth so the same value can be matched against later audits.
 */
function OpportunityScoreBody({ score }: ScoreBodyProps) {
  const breakdown = score.breakdown;
  const totalScore = score.total_score;
  const bv = score.business_value;
  const reasoning = score.ai_reasoning;

  const totalAvailable = totalScore !== null && totalScore !== undefined;

  return (
    <>
      <div className="score-snap">
        <div className="score-headline">
          <div className="score-num">
            <span className={`score-value${totalAvailable ? '' : ' pending'}`}>
              {totalAvailable ? formatNumber(Number(totalScore)) : '—'}
            </span>
            <span className="out-of">/ 10</span>
          </div>
          <div className="muted" style={{ fontSize: 'var(--t-caption)' }}>
            权重配置 / 生效：见下方表格。AI priority：
            <span className="badge badge-accent mono">
              {score.ai_priority ?? <NotMeasured label="未生成" />}
            </span>
          </div>
        </div>
        <div className="score-meta">
          <div className="score-meta-item">
            <div className="meta-k">Configured Weight Sum</div>
            <div className="meta-v mono">
              {formatNumber(sumWeights(score.weights.configured))}
            </div>
          </div>
          <div className="score-meta-item">
            <div className="meta-k">Effective Weight Sum</div>
            <div className="meta-v mono">
              {formatNumber(sumWeights(score.weights.effective))}
            </div>
          </div>
          <div className="score-meta-item">
            <div className="meta-k">Business Value</div>
            <div className="meta-v mono">{bv !== null ? formatNumber(Number(bv)) : <NotMeasured label="Pending PM Input" />}</div>
          </div>
          <div className="score-meta-item">
            <div className="meta-k">Calculation Version</div>
            <div className="meta-v mono">{score.calculation_version}</div>
          </div>
        </div>
      </div>

      <table className="score-table" data-testid="score-table">
        <thead>
          <tr>
            <th>Dimension</th>
            <th className="num">Configured Wt</th>
            <th className="num">Effective Wt</th>
            <th className="num">Value</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {DIMENSION_KEYS.map((k) => {
            const dim = breakdown[k];
            const available = dim.available;
            const value = available && dim.score !== null ? formatNumber(Number(dim.score)) : null;
            return (
              <tr key={k}>
                <td className="label-cell">{DIMENSION_LABEL[k]}</td>
                <td className="weight-cell mono">
                  {score.weights.configured[k] ?? <NotMeasured />}
                </td>
                <td className="weight-cell mono">
                  {score.weights.effective[k] ?? <NotMeasured />}
                </td>
                <td className={`value-cell${available ? '' : ' unavailable'} mono`}>
                  {available && value !== null ? value : 'Not Available'}
                </td>
                <td className="reason-cell">
                  {available ? (
                    'OK'
                  ) : (
                    <span className="muted">{dim.note ?? 'Pending PM Input'}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {reasoning ? (
        <div className="ai-reasoning">
          <div className="ai-reasoning-label">AI Reasoning</div>
          <div className="ai-reasoning-body">{reasoning}</div>
        </div>
      ) : null}
    </>
  );
}

const DIMENSION_KEYS: ReadonlyArray<keyof OpportunityScore['breakdown']> = [
  'user_coverage',
  'severity',
  'growth',
  'user_value',
  'business_value',
];

const DIMENSION_LABEL: Record<keyof OpportunityScore['breakdown'], string> = {
  user_coverage: 'User Coverage',
  severity: 'Severity',
  growth: 'Growth',
  user_value: 'User Value',
  business_value: 'Business Value',
};

function sumWeights(weights: Record<string, string>): number {
  // The backend serialises Decimal as a string; do not coerce to number if
  // the row is empty. Negative or non-numeric values fall back to 0.
  let total = 0;
  for (const v of Object.values(weights)) {
    const n = Number(v);
    if (Number.isFinite(n)) total += n;
  }
  return Math.round(total * 100) / 100;
}

/* ---------- Idempotency ID helper ----------
 *
 * Phase 7 B1 fix: the backend PATCH handler validates ``request_id`` as
 * a real UUID (pydantic UUID4). The previous client emitted
 * ``req_<timestamp>_<rand>`` whenever ``crypto.randomUUID`` was missing,
 * which the server rejected with 422 — a silent dead-end in older Safari
 * builds. We now refuse to send rather than hand the server a value it
 * will reject.
 */
function mintRequestId(): string {
  if (typeof crypto === 'undefined') {
    throw new Error('当前环境未提供 crypto，无法生成请求 ID，请刷新或更换浏览器。');
  }
  if (typeof crypto.randomUUID !== 'function') {
    throw new Error('当前环境不支持 crypto.randomUUID，请使用现代浏览器。');
  }
  const id = crypto.randomUUID();
  // Defensive sanity check: the call returns a v4 UUID or throws, but if
  // a polyfill ever returns the wrong shape we still refuse to send it.
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
    throw new Error('生成的请求 ID 不是合法的 UUID v4。');
  }
  return id;
}
