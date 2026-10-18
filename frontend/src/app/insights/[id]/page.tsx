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
 *      render as "待填写业务价值" / "暂无数据")
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
import { TechnicalDetails } from '../../../components/TechnicalDetails';
import { displayLabel, sourceLabel, topicLabel, readableSystemText, scoreExplanation, userMessage } from '../../../lib/presentation';
import { useParams, useRouter } from 'next/navigation';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
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
import { ResearchAnswer } from '../../../components/ResearchAnswer';
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
  ResearchCoverageItem,
  ResearchCoverageReport,
  ResearchFindingRead,
  ResearchRunRead,
  ResearchSourceRead,
  ResearchTaskContract,
  ResearchTaskContractItem,
} from '../../../lib/api/types';
import {
  isResearchAnswerV7,
  isResearchSupportV7,
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
  { value: 'P0', label: 'P0 · 紧急' },
  { value: 'P1', label: 'P1 · 重要' },
  { value: 'P2', label: 'P2 · 一般' },
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
  const requestScopeRef = useRef({ projectId: activeProjectId, id: insightId });
  if (requestScopeRef.current.projectId !== activeProjectId || requestScopeRef.current.id !== insightId) {
    requestScopeRef.current = { projectId: activeProjectId, id: insightId };
  }

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

  /* ---------- External Research (Phase 8) ----------
   *
   * The runner synchronously POSTs a new ``research_run`` for the
   * current Insight; the page optimistically renders a "Running"
   * badge during the request window and refetches the history
   * once the response lands. Findings, sources and the run row
   * for the currently-selected run are loaded on demand so the
   * page does not surface a foreign project's research surface
   * (``activeProjectId`` is appended to every per-run GET as a
   * REQUIRED ``project_id`` query — the backend 404s on mismatch,
   * we surface a clear error and refuse to fall back).
   *
   * No sessionStorage, no local cache: a refresh always re-reads
   * from the backend via the history endpoint. */
  const [researchQuestion, setResearchQuestion] = useState('');
  const [researchScope, setResearchScope] = useState('');
  const [researchSubmitting, setResearchSubmitting] = useState(false);
  const [researchSubmitError, setResearchSubmitError] = useState<string | null>(null);
  const [researchHistory, setResearchHistory] = useState<ResearchRunRead[] | null>(null);
  const [researchHistoryError, setResearchHistoryError] = useState<string | null>(null);
  const [researchHistoryLoading, setResearchHistoryLoading] = useState(false);
  /* Synthetic "Running" status local to the page: the backend
   * synchronously finalises every run before returning 200, so a
   * real client never sees ``status === 'running'``. The page
   * surfaces the synthetic state exclusively during the active
   * POST window, then re-reads from the server so a true
   * terminal status (``completed`` / ``partial_failed`` /
   * ``failed``) replaces it immediately. */
  const [researchRunningRunId, setResearchRunningRunId] = useState<string | null>(null);
  const [selectedResearchRunId, setSelectedResearchRunId] = useState<string | null>(null);
  const [selectedResearchRun, setSelectedResearchRun] = useState<ResearchRunRead | null>(null);
  const [researchFindings, setResearchFindings] = useState<ResearchFindingRead[] | null>(null);
  const [researchSources, setResearchSources] = useState<ResearchSourceRead[] | null>(null);
  const [researchDetailLoading, setResearchDetailLoading] = useState(false);
  const [researchDetailError, setResearchDetailError] = useState<string | null>(null);
  /* expandedFindingIds tracks which competitor / opportunity /
   * risk rows the user has opened so the citations block is
   * collapsed by default — keeps the default view tight and
   * exposes ``source_refs`` only on demand to avoid implying
   * every finding carries a citation. */
  const [expandedFindingIds, setExpandedFindingIds] = useState<Record<string, boolean>>({});
  /* v6 — ``supportExpandedFindingIds`` tracks which
   * ``inference`` / ``recommendation`` rows the user has
   * opened so the supports block is collapsed by default.
   * Kept independent from ``expandedFindingIds`` because
   * the two toggles serve different affordances (citations
   * vs. supporting facts) and a user may want both open on
   * the same row. */
  const [supportExpandedFindingIds, setSupportExpandedFindingIds] = useState<Record<string, boolean>>({});
  const [researchUnrecoverable, setResearchUnrecoverable] = useState<string | null>(null);

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
      // errors so the UI can show "待填写业务价值" rather than an error.
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

  /* ---------- External Research fetchers (Phase 8) ---------- */

  const fetchResearchHistory = useCallback(async () => {
    if (!insightId || !signal || !activeProjectId) return;
    const mySignal = signal;
    setResearchHistoryLoading(true);
    setResearchHistoryError(null);
    // The backend's GET /insights/{id}/research takes a REQUIRED
    // ``project_id`` query parameter (a missing value 422s). The
    // page must always pass the active project id so a real call
    // succeeds AND so the backend's own cross-project guard can
    // 404 a request that targets a foreign insight.
    const res = await client.listInsightResearchRuns(
      insightId,
      { project_id: activeProjectId, limit: 50, offset: 0 },
      mySignal,
    );
    if (mySignal.aborted) return;
    if (res.error || !res.data) {
      setResearchHistoryError(
        res.error ? classifyError(res.error) : '研究历史加载失败。',
      );
      setResearchHistory([]);
      setResearchHistoryLoading(false);
      return;
    }
    setResearchHistory(res.data.items);
    setResearchHistoryLoading(false);
    // Auto-pick the latest run on every fresh load — the page
    // mirrors the backend's ``created_at DESC`` ordering, so
    // ``items[0]`` is the most recent. The detail fetcher below
    // reads the same id and triggers the full lookup.
    const latest = res.data.items[0];
    if (latest) {
      setSelectedResearchRunId((prev) => prev ?? latest.id);
    }
  }, [activeProjectId, client, insightId, signal]);

  /* Reload the detail (run row + findings + sources) for the
   * currently selected run. Always passes the active project id
   * as a REQUIRED ``project_id`` query — the backend 404s on
   * mismatch and the page surfaces that as ``External Research
   * Unavailable`` without leaking the foreign row. */
  const fetchResearchDetail = useCallback(async () => {
    if (!insightId || !signal || !activeProjectId) return;
    if (!selectedResearchRunId) {
      setSelectedResearchRun(null);
      setResearchFindings(null);
      setResearchSources(null);
      return;
    }
    const mySignal = signal;
    setResearchDetailLoading(true);
    setResearchDetailError(null);
    const projectId = activeProjectId;
    const runId = selectedResearchRunId;
    const [runRes, findingRes, sourceRes] = await Promise.all([
      client.getResearchRun(runId, { project_id: projectId }, mySignal),
      client.listResearchFindings(
        runId,
        { project_id: projectId, limit: 100, offset: 0 },
        mySignal,
      ),
      client.listResearchSources(
        runId,
        { project_id: projectId, limit: 100, offset: 0 },
        mySignal,
      ),
    ]);
    if (mySignal.aborted) return;
    // An isolation failure (404) collapses the entire surface to
    // a single error rather than mixing partial data; the page
    // also tags the run as ``Unrecoverable`` so a fresh selection
    // can recover.
    const firstErr = runRes.error || findingRes.error || sourceRes.error;
    if (firstErr) {
      setResearchDetailError(
        'External Research 加载失败，请稍后再试或换一个 Run。',
      );
      if (runRes.error) {
        setResearchUnrecoverable(
          `External Research Unavailable (${runRes.error.status})`,
        );
      }
      setResearchDetailLoading(false);
      return;
    }
    if (!runRes.data) {
      setResearchDetailError('External Research 数据为空。');
      setResearchDetailLoading(false);
      return;
    }
    // Drop the synthetic "Running" badge if the freshly-loaded
    // row says the run has already finalised (or the run_id is
    // the one we believed was still running). Once the server
    // confirms a terminal status, the page no longer pretends
    // the request is still in flight.
    if (runId === researchRunningRunId && runRes.data.status !== 'running') {
      setResearchRunningRunId(null);
    }
    setSelectedResearchRun(runRes.data);
    setResearchFindings(findingRes.data?.items ?? []);
    setResearchSources(sourceRes.data?.items ?? []);
    setResearchDetailLoading(false);
    setResearchUnrecoverable(null);
    // Reset the expanded-citations dictionary on every run
    // switch — the previous run's choice should not bleed into
    // the new run. The v6 support-toggle dictionary is reset
    // in lockstep so a click on a previous-run's
    // ``inference`` does not auto-expand a same-id row in
    // the new run.
    setExpandedFindingIds({});
    setSupportExpandedFindingIds({});
  }, [
    activeProjectId,
    client,
    insightId,
    researchRunningRunId,
    selectedResearchRunId,
    signal,
  ]);

  /* Submit a new research run. The runtime pipeline is
   * synchronous on the backend (200 lands with the finalised
   * row), so the optimistic Running badge is ONLY shown during
   * the network round-trip — once the row returns we refetch
   * the history so the new entry appears in the picker and the
   * detail pane reloads to reflect the server-side status. */
  const submitResearchRun = useCallback(async () => {
    if (!insightId || !signal || !activeProjectId) return;
    const question = researchQuestion.trim();
    if (question.length === 0) {
      setResearchSubmitError('请填写研究问题。');
      return;
    }
    if (question.length > 500) {
      setResearchSubmitError('研究问题不能超过 500 字。');
      return;
    }
    const scope = researchScope.trim();
    if (scope.length > 2000) {
      setResearchSubmitError('Scope 不能超过 2000 字。');
      return;
    }
    if (liveInsightProjectMismatch || !insight) {
      setResearchSubmitError('当前洞察不属于本项目，无法提交研究。');
      return;
    }
    const mySignal = signal;
    setResearchSubmitting(true);
    setResearchSubmitError(null);
    // Placeholder id for the synthetic Running badge — the
    // server-assigned UUID only exists after the POST returns,
    // so we label the badge by the user's intent ("提交中…")
    // and clear it the moment the response lands.
    setResearchRunningRunId('submitting');
    const res = await client.startResearchRun(
      insightId,
      {
        research_question: question,
        scope: scope.length > 0 ? scope : null,
        // Phase 8 v7 — pin the extractive answer contract
        // explicitly. The backend accepts an unset
        // ``prompt_version`` and defaults to v1; the page
        // always opts into v7 so the v5/v6 LLM-authored
        // audit shape never lands on a fresh run.
        prompt_version: 'research-agent-v7',
      },
      mySignal,
    );
    if (mySignal?.aborted) {
      setResearchSubmitting(false);
      setResearchRunningRunId(null);
      return;
    }
    setResearchSubmitting(false);
    if (res.error) {
      setResearchRunningRunId(null);
      setResearchSubmitError(
        res.error.status === 409
          ? 'External Research 配置缺失，请联系管理员配置 Tavily 与 LLM 凭证。'
          : classifyError(res.error),
      );
      return;
    }
    if (res.data) {
      // Backend returned a finalised run — push the user input
      // aside and route them to the new run's detail panel.
      setResearchQuestion('');
      setResearchScope('');
      setSelectedResearchRunId(res.data.id);
      setResearchRunningRunId(null);
      // Refetch the history so the new run tops the list. The
      // detail fetcher triggers automatically because we updated
      // selectedResearchRunId.
      await fetchResearchHistory();
    }
  }, [
    activeProjectId,
    client,
    fetchResearchHistory,
    insight,
    insightId,
    liveInsightProjectMismatch,
    researchQuestion,
    researchScope,
    signal,
  ]);

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
    /* Phase 8 — wipe every piece of External Research state on a
     * project switch so the new project never inherits a stale
     * running-runnner id, a stale selected run or a stale error.
     * The signal-based abort already cancels in-flight requests;
     * this guard catches data that landed BEFORE the abort fired. */
    setResearchQuestion('');
    setResearchScope('');
    setResearchSubmitting(false);
    setResearchSubmitError(null);
    setResearchHistory(null);
    setResearchHistoryError(null);
    setResearchHistoryLoading(false);
    setResearchRunningRunId(null);
    setSelectedResearchRunId(null);
    setSelectedResearchRun(null);
    setResearchFindings(null);
    setResearchSources(null);
    setResearchDetailLoading(false);
    setResearchDetailError(null);
    setExpandedFindingIds({});
    setSupportExpandedFindingIds({});
    setResearchUnrecoverable(null);
    // We DO list activeProjectId in deps. The hook fires on every project
    // change INCLUDING the initial null→first-project, which is exactly
    // what we want — a fresh project always means a fresh start.
  }, [activeProjectId]);

  useEffect(() => {
    void fetchInsight();
    void fetchEvidence();
    void fetchOpportunityScore();
    void refetchDecision();
    void fetchResearchHistory();
  }, [
    fetchInsight,
    fetchEvidence,
    fetchOpportunityScore,
    refetchDecision,
    fetchResearchHistory,
  ]);

  // Reload the External Research detail whenever the user picks
  // a different run. The fetcher is a no-op until the history
  // arrives (selectedResearchRunId stays null).
  useEffect(() => {
    void fetchResearchDetail();
  }, [fetchResearchDetail]);

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
    const requestScope = requestScopeRef.current;
    if (insight.project_id !== requestScope.projectId) return;
    const res = await client.createRequirement(
      insight.id,
      mySignal ?? undefined,
      // Phase 8 v7 — opt into the revised Requirement
      // generation prompt. The backend treats an empty
      // body as "use the active default", so passing
      // ``undefined`` would still be valid; the explicit
      // pin documents which prompt authored the row and
      // keeps PM edit/approval flow unchanged.
      'requirement-generation-v2', requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal?.aborted) {
      setRequirementBusy(false);
      return;
    }
    setRequirementBusy(false);
    if (res.error || !res.data) {
      setRequirementError(
        res.error ? classifyError(res.error) : '创建需求失败。',
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
          description="请切换到这条洞察所属的项目后查看。"
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
        description="请切换到这条洞察所属的项目后查看。"
      />
    );
  }

  return (
    <div className="page page-insight-detail">
      <header className="page-header">
        <span className="eyebrow">洞察详情</span>
        <h1 className="page-title">{insight.title ?? '尚无标题'}</h1>
        <p className="page-subtitle">更新于 {formatRelative(insight.created_at ?? null)}</p>
        <TechnicalDetails label="查看来源记录">
          <p>分析记录：<code>{insight.run_id}</code></p>
          <p>聚类记录：<code>{insight.source_cluster_external_id}</code></p>
        </TechnicalDetails>
      </header>

      <section className="section hero" data-od-id="hero">
        <div className="hero-tags">
          <span className="badge badge-accent">
            {displayLabel(insight.status)}
          </span>
          <span className="badge badge-ai">AI 生成</span>
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
        <p className="lead">{readableSystemText(insight.affected_segment, '受影响人群尚待确认')}</p>
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
          <h3>洞察内容</h3>
          <span className="badge badge-ai">AI 生成</span>
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
          {readableSystemText(insight.insight, '尚无洞察正文，请先核实相关反馈。')}
        </blockquote>
        <div className="meta-grid">
          <div className="meta-item">
            <div className="meta-k">用户痛点</div>
            <div className="meta-v">{readableSystemText(insight.pain_point, '尚待确认')}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">受影响用户</div>
            <div className="meta-v">{readableSystemText(insight.affected_segment, '尚待确认')}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">改进建议</div>
            <div className="meta-v">{readableSystemText(insight.recommendation, '尚待补充建议')}</div>
          </div>
        </div>
      </section>

      {/* Evidence */}
      <section className="section" data-od-id="evidence">
        <header className="section-head">
          <h3>反馈证据 · {evidence?.total ?? 0}</h3>
          <span className="muted" style={{ fontSize: 'var(--t-caption)' }}>
            用户反馈原文
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
            description="尚无代表反馈，可到分析页重新生成洞察。"
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
                    <span>来源 {ev.feedback_source ? sourceLabel(ev.feedback_source) : <NotMeasured label="—" />}</span>
                    <span>用户 <span className="mono">{ev.feedback_user_id ?? '—'}</span></span>
                    <span>证据类型 <span>{displayLabel(ev.evidence_type)}</span></span>
                    <span>相关度 <span className="mono">{formatNumber(ev.relevance_score)}</span></span>
                    <span>{formatRelative(ev.feedback_created_at ?? null)}</span>
                  </div>
                  {ev.feedback_analysis ? (
                    <div className="evidence-ai" data-testid="evidence-ai">
                      <span className="badge badge-ai">AI 分析</span>
                      <span>
                        类型 <b>{displayLabel(ev.feedback_analysis.feedback_type)}</b>
                      </span>
                      <span>
                        主题 <b>{topicLabel(ev.feedback_analysis.module)}</b>
                      </span>
                      <span>
                        意图 <b>{readableSystemText(ev.feedback_analysis.intent, '待核对')}</b>
                      </span>
                      <span>
                        情感 <b>{displayLabel(ev.feedback_analysis.sentiment)}</b>
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
                      <span className="badge badge-outline">AI 分析</span>
                      <span className="muted">尚无分类分析结果</span>
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
          <h3>机会评分</h3>
          <TechnicalDetails label="查看评分信息">
          {score ? (
            <span className="muted mono" style={{ fontSize: 'var(--t-caption)' }}>
              评分规则 {score.calculation_version} ·
              模型 <span className="mono">{score.model ?? '—'}</span>
            </span>
          ) : null}
          </TechnicalDetails>
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
              <span className="form-label">业务价值（0–10）</span>
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
              保存业务价值
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
            title="尚无机会评分"
            description="先填写业务价值并保存，即可查看评分并作出决策。"
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
                预览评分
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
          <h3>产品决策</h3>
          <span className="badge badge-ai">由你确认最终决策</span>
        </header>

        <div className="decision-panel">
          <div className="decision-box ai">
            <div className="decision-label">AI 建议</div>
            <div className="decision-value">
              <span className="badge badge-accent">
                {score?.ai_priority ?? decision?.ai_priority ?? <NotMeasured label="—" />}
              </span>
              <span style={{ marginLeft: 12, color: 'var(--ai-fg)' }}>
                {score ? scoreExplanation(score.ai_reasoning) : '尚无评分建议，请结合反馈作出判断。'}
              </span>
            </div>
          </div>
          <div className="decision-box">
            <div className="decision-label">产品决策</div>
            {decisionMissing ? (
              <div className="decision-value muted" style={{ fontSize: 'var(--t-small)' }}>
                尚未提交 PM 决策。下方按钮可立即生效。
              </div>
            ) : decision ? (
              <div className="decision-value">
                <div>
                  状态 <b>{displayLabel(decisionStatus)}</b>
                  {decision.pm_priority ? (
                    <>
                      {' '}· 优先级 <span className="badge badge-accent">{decision.pm_priority}</span>
                    </>
                  ) : null}
                  {decision.override_reason ? (
                    <>
                      {' '}· 原因 <span className="mono">{OVERRIDE_REASONS.find((reason) => reason.value === decision.override_reason)?.label ?? "其他原因"}</span>
                    </>
                  ) : null}
                </div>
                {decision.override_note ? (
                  <div className="muted" style={{ marginTop: 6, fontSize: 'var(--t-small)' }}>
                    {decision.override_note}
                  </div>
                ) : null}
                <div className="muted mono" style={{ marginTop: 6, fontSize: 'var(--t-caption)' }}>
                  版本 {decision.version} · 更新 {formatRelative(decision.updated_at ?? null)}
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
            采纳
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
            调整优先级
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void deferDecision()}
            disabled={decisionBusy || decisionStatus === 'deferred'}
            data-action="defer"
          >
            暂缓
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => void rejectDecision()}
            disabled={decisionBusy || decisionStatus === 'rejected'}
            data-action="reject"
          >
            驳回
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

      {/* External Research (Phase 8) — fully separated surface
          from Internal Evidence above. The launcher posts the
          user's research question + scope to the synchronous
          research runner; the history list refreshes on success
          and the detail panel reloads the run + findings +
          sources from the backend (always project-scoped — the
          page passes the active project id to every per-run
          GET so a foreign row 404s and the section clearly
          disables itself). */}
      <section
        className="research-section"
        data-od-id="external-research"
        data-testid="external-research"
      >
        <header className="research-section-header">
          <h3>外部研究</h3>
          <span className="badge badge-outline">公开资料</span>
          <span className="research-section-hint">
            检索公开资料，为产品判断补充依据。
          </span>
        </header>

        <div
          className="research-launcher"
          data-testid="research-launcher"
        >
          <label className="form-field" htmlFor="research-question">
            <span className="form-label">研究问题</span>
            <textarea
              id="research-question"
              className="form-textarea"
              rows={2}
              maxLength={500}
              value={researchQuestion}
              onChange={(e) => {
                setResearchQuestion(e.target.value);
                if (researchSubmitError) setResearchSubmitError(null);
              }}
              placeholder="例如：竞品 X 在 SMB 市场的定价策略是什么？"
              disabled={researchSubmitting}
              data-testid="research-question-input"
            />
          </label>
          <label className="form-field" htmlFor="research-scope">
            <span className="form-label">研究范围（可选）</span>
            <textarea
              id="research-scope"
              className="form-textarea"
              rows={2}
              maxLength={2000}
              value={researchScope}
              onChange={(e) => {
                setResearchScope(e.target.value);
                if (researchSubmitError) setResearchSubmitError(null);
              }}
              placeholder="市场 / 时间窗口 / 人群。留空则仅用研究问题检索。"
              disabled={researchSubmitting}
              data-testid="research-scope-input"
            />
          </label>
          <div className="research-launcher-cta" style={{ gridColumn: '1 / -1' }}>
            {researchSubmitting ? (
              <span
                className="research-launcher-running"
                data-testid="research-running"
              >
                <span
                  className="research-launcher-progress"
                  aria-hidden="true"
                />
                正在执行研究，请稍候…
              </span>
            ) : null}
            <span style={{ flex: 1 }} />
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={
                researchSubmitting ||
                liveInsightProjectMismatch ||
                researchQuestion.trim().length === 0
              }
              onClick={() => void submitResearchRun()}
              data-testid="research-run-button"
            >
              开始研究
            </button>
          </div>
          {researchSubmitError ? (
            <div
              className="research-launcher-error"
              role="alert"
              data-testid="research-submit-error"
              style={{ gridColumn: '1 / -1' }}
            >
              {researchSubmitError}
            </div>
          ) : null}
        </div>

        {researchUnrecoverable ? (
          <div
            className="research-unavailable"
            role="alert"
            data-testid="research-unavailable"
          >
            <span className="research-unavailable-title">
              研究暂不可用
            </span>
            {researchUnrecoverable}。当前项目无法查看这条研究记录。
          </div>
        ) : null}

        <div className="research-section-divider">研究历史</div>

        {researchHistoryLoading && !researchHistory ? (
          <LoadingState />
        ) : researchHistoryError && !researchHistory ? (
          <ErrorState
            message={researchHistoryError}
            onRetry={() => {
              setResearchHistoryError(null);
              void fetchResearchHistory();
            }}
          />
        ) : researchHistory && researchHistory.length === 0 ? (
          <div className="research-history-empty" data-testid="research-history-empty">
            尚无研究历史。输入研究问题，开始首次研究。
          </div>
        ) : researchHistory ? (
          <ul className="research-history" data-testid="research-history">
            {researchHistory.map((run) => {
              const isActive = run.id === selectedResearchRunId;
              const isRunning =
                researchRunningRunId === 'submitting' && isActive;
              return (
                <li
                  key={run.id}
                  className={`research-history-item${isActive ? ' is-active' : ''}`}
                  data-testid={`research-history-item-${run.id}`}
                >
                  <div className="research-history-row">
                    <span
                      className={`research-status-pill research-status-pill--${renderableResearchStatus(
                        isRunning ? 'running' : run.status,
                      )}`}
                    >
                      {renderResearchStatusLabel(
                        isRunning ? 'running' : run.status,
                      )}
                    </span>

                    <span className="research-history-row meta">
                      {formatRelative(run.created_at)}
                    </span>
                    <span className="research-history-spacer" />
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => {
                        setSelectedResearchRunId(run.id);
                        setResearchRunningRunId(null);
                      }}
                      disabled={
                        researchSubmitting && isActive
                      }
                      data-testid={`research-select-${run.id}`}
                    >
                      查看
                    </button>
                  </div>
                  <p className="research-history-question">
                    {run.research_question}
                  </p>
                </li>
              );
            })}
          </ul>
        ) : null}

        {selectedResearchRun ? (
          <ResearchDetailPanel
            run={selectedResearchRun}
            findings={researchFindings}
            sources={researchSources}
            loading={researchDetailLoading}
            error={researchDetailError}
            expanded={expandedFindingIds}
            onToggleFinding={(id) =>
              setExpandedFindingIds((prev) => ({ ...prev, [id]: !prev[id] }))
            }
            supportExpanded={supportExpandedFindingIds}
            onToggleSupport={(id) =>
              setSupportExpandedFindingIds((prev) => ({
                ...prev,
                [id]: !prev[id],
              }))
            }
          />
        ) : null}
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
              ? '下一步：创建需求'
              : decisionStatus === 'pending_review'
                ? '请先确认产品决策'
                : decisionStatus === 'deferred'
                  ? '这条洞察已暂缓'
                  : decisionStatus === 'rejected'
                    ? '这条洞察已驳回'
                    : '尚不能创建需求'}
          </h4>
          <p>
            {ctaGate.enable
              ? '将已确认的洞察整理为产品需求。'
              : !ctaGate.evidenceOk
                ? '请先补充反馈证据，再创建需求。'
                : !ctaGate.accepted
                  ? '采纳洞察或调整优先级后，即可创建需求。'
                  : '请先设置需求优先级。'}
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
            创建需求 →
          </button>
          <Link className="btn btn-ghost btn-sm" href="/insights">
            返回列表
          </Link>
        </div>
      </section>

      {requirementError ? (
        <ErrorState
          title="创建需求失败"
          message={requirementError}
          onRetry={() => setRequirementError(null)}
        />
      ) : null}
      {createdRequirementId ? (
        <div className="alert alert-info" role="status" data-testid="requirement-created">
          需求已创建（{createdRequirementId.slice(0, 8)}…），可在需求列表中查看。
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
                <EmptyState title="尚无历史" description="这条洞察还没有决策记录。" />
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
                          {h.ai_priority ? `AI 建议：${h.ai_priority}` : null}
                          {h.pm_priority ? ` · 最终优先级：${h.pm_priority}` : null}
                          {h.override_reason ? ` · 原因: ${OVERRIDE_REASONS.find((reason) => reason.value === h.override_reason)?.label ?? "其他原因"}` : null}
                          {h.override_note ? ` · 备注: ${h.override_note}` : null}
                        </div>
                      ) : null}
                      <code className="muted mono" style={{ fontSize: 'var(--t-caption)' }}>
                        操作编号 {h.request_id.slice(0, 8)}…
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
              <h3>调整需求优先级</h3>
              <p>调整优先级，并说明判断依据。</p>
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
                <span className="form-label">调整原因（必填）</span>
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
                  placeholder="说明调整原因。"
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
                确认调整
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
              <h3>决策已更新</h3>
              <p>{userMessage(conflictModal.message)}</p>
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
  const reasoning = scoreExplanation(score.ai_reasoning);

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
            建议优先级：
            <span className="badge badge-accent mono">
              {score.ai_priority ?? <NotMeasured label="未生成" />}
            </span>
          </div>
        </div>
        <TechnicalDetails label="查看评分明细">
        <div className="score-meta">
          <div className="score-meta-item">
            <div className="meta-k">配置权重合计</div>
            <div className="meta-v mono">
              {formatNumber(sumWeights(score.weights.configured))}
            </div>
          </div>
          <div className="score-meta-item">
            <div className="meta-k">实际权重合计</div>
            <div className="meta-v mono">
              {formatNumber(sumWeights(score.weights.effective))}
            </div>
          </div>
          <div className="score-meta-item">
            <div className="meta-k">业务价值</div>
            <div className="meta-v mono">{bv !== null ? formatNumber(Number(bv)) : <NotMeasured label="待填写业务价值" />}</div>
          </div>
          <div className="score-meta-item">
            <div className="meta-k">评分规则版本</div>
            <div className="meta-v mono">{score.calculation_version}</div>
          </div>
        </div>

      <table className="score-table" data-testid="score-table">
        <thead>
          <tr>
            <th>评分维度</th>
            <th className="num">配置权重</th>
            <th className="num">实际权重</th>
            <th className="num">得分</th>
            <th>状态</th>
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
                  {available && value !== null ? value : '暂无数据'}
                </td>
                <td className="reason-cell">
                  {available ? (
                    '已评分'
                  ) : (
                    <span className="muted">{displayLabel(dim.note, '暂无评分依据')}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
        </TechnicalDetails>
      </div>

      {reasoning ? (
        <div className="ai-reasoning">
          <div className="ai-reasoning-label">评分依据</div>
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
  user_coverage: '用户覆盖',
  severity: '问题严重度',
  growth: '增长潜力',
  user_value: '用户价值',
  business_value: '业务价值',
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

/* ---------- Phase 8 — External Research helpers ----------
 *
 * The Research Detail Panel is intentionally separated from
 * the Internal Evidence section above. Findings are grouped by
 * category so the PM can scan competitors / market patterns /
 * opportunities / risks independently. Every confirmed finding
 * shows an "cited" badge; every not_verified finding is
 * explicitly marked so the page can never silently pass off an
 * ungrounded claim as a fact.
 */

type ResearchFindingCategoryLabel =
  | 'competitor'
  | 'market_pattern'
  | 'opportunity'
  | 'risk'
  | 'summary';

const RESEARCH_CATEGORY_ORDER: ReadonlyArray<ResearchFindingCategoryLabel> = [
  'summary',
  'competitor',
  'market_pattern',
  'opportunity',
  'risk',
];

const RESEARCH_CATEGORY_LABEL: Record<
  ResearchFindingCategoryLabel,
  string
> = {
  summary: '摘要',
  competitor: '竞品发现',
  market_pattern: '市场模式',
  opportunity: '机会',
  risk: '风险',
};

function isKnownResearchCategory(
  raw: string,
): raw is ResearchFindingCategoryLabel {
  return (
    raw === 'summary' ||
    raw === 'competitor' ||
    raw === 'market_pattern' ||
    raw === 'opportunity' ||
    raw === 'risk'
  );
}

function renderableResearchStatus(status: string | null | undefined): string {
  // Backend returns one of: pending / running / completed /
  // partial_failed / failed. Anything else (very old rows,
  // unrecognised literals) collapses to ``pending`` so the CSS
  // class lookup never blows up — same convention used for the
  // ``Insight.status`` fallback further up the page.
  switch (status) {
    case 'pending':
    case 'running':
    case 'completed':
    case 'partial_failed':
    case 'failed':
      return status;
    default:
      return 'pending';
  }
}

function renderResearchStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'completed':
      return '已完成';
    case 'partial_failed':
      return '部分完成';
    case 'failed':
      return '失败';
    case 'running':
      return '研究中';
    case 'pending':
      return '等待中';
    default:
      return '状态待确认';
  }
}

function renderFindingCategoryLabel(
  raw: string | null | undefined,
): string {
  if (raw && isKnownResearchCategory(raw)) {
    return RESEARCH_CATEGORY_LABEL[raw];
  }
  return '未分类';
}

interface ResearchFindingGroupView {
  category: ResearchFindingCategoryLabel;
  label: string;
  findings: ResearchFindingRead[];
}

function analysisGapMessages(trace: unknown): string[] {
  if (!trace || typeof trace !== 'object' || !('analysis_gaps' in trace)) return [];
  const gaps = (trace as { analysis_gaps?: unknown }).analysis_gaps;
  if (!Array.isArray(gaps)) return [];
  const labels: Record<string, string> = { market_pattern: '市场模式', opportunity: '机会', risk: '风险' };
  return gaps.flatMap((gap: unknown) => {
    if (!gap || typeof gap !== 'object') return [];
    const entry = gap as { category?: unknown; reason?: unknown };
    if (typeof entry.category !== 'string' || !labels[entry.category]) return [];
    const reason = entry.reason === 'candidate_support_rejected'
      ? '分析候选未通过支持关系校验，仍需补充证据和人工核对。'
      : '本次未提出具有已确认事实支持的候选；这不代表没有相关机会或风险。';
    return [labels[entry.category] + '：' + reason];
  });
}

function groupResearchFindings(
  findings: ResearchFindingRead[] | null,
): ResearchFindingGroupView[] {
  if (!findings || findings.length === 0) return [];
  const buckets: Record<string, ResearchFindingRead[]> = {};
  for (const finding of findings) {
    const cat = isKnownResearchCategory(finding.category)
      ? finding.category
      : 'summary';
    if (!buckets[cat]) buckets[cat] = [];
    buckets[cat].push(finding);
  }
  return RESEARCH_CATEGORY_ORDER.filter(
    (c) => buckets[c] && buckets[c].length > 0,
  ).map((c) => ({
    category: c,
    label: RESEARCH_CATEGORY_LABEL[c],
    findings: buckets[c]!,
  }));
}

/* ---------- Research v5 — evidence coverage audit ----------
 *
 * The backend's v5 ``ResearchRunRead`` carries a
 * ``coverage_report`` (server-side per-requirement audit) and
 * a ``task_contract`` (parser's structured view of the user
 * question). The page reads them ONLY to display a
 * code-rule-level audit; the field's own
 * ``semantic_review`` is pinned to ``'NOT_MEASURED'`` on the
 * server, and the page must never claim the run is
 * "fully answered" based on any combination of these fields.
 *
 * Three orthogonal rules:
 *
 *   1. The summary label is derived from
 *      ``overall_completable`` (the server's boolean) AND
 *      the per-item ``status`` set. We never read
 *      ``confidence``, ``confirmed`` count, the LLM's
 *      ``model`` self-rating, or the ``semantic_review``
 *      field as truth — the audit is bounded code rules.
 *
 *   2. The per-item row joins ``coverage_item.requirement_id``
 *      back to ``task_contract.items[].id`` for the
 *      human-readable ``value``. The raw ``requirement_id``
 *      is treated as an internal handle and is never
 *      rendered. Items that don't resolve to a
 *      ``task_contract`` entry (parser didn't surface them)
 *      fall back to the literal "需人工核对的要求" label
 *      while keeping ``run.research_question`` queryable
 *      above the section.
 *
 *   3. We do NOT display any portion of
 *      ``execution_trace`` — it can carry internal step /
 *      request ids and the brief forbids exposing them.
 *
 * Non-technical boundary: the rendered UI never shows the
 * raw server field names (``coverage_report``,
 * ``overall_completable``, ``semantic_review``,
 * ``NOT_MEASURED``). PM-facing text is human-readable;
 * the audit is described as a procedural pre-check that
 * still requires manual semantic review.
 */

const COVERAGE_STATUS_LABEL: Readonly<Record<string, string>> = {
  covered: '已覆盖',
  partial: '部分覆盖',
  not_verified: '未验证',
  not_applicable: '不适用',
};

/* The v5 kind vocabulary the parser can surface. Any
 * unknown value falls back to "待核对要求" — the brief
 * is explicit that we must NEVER echo the raw English
 * enum to the user (it would leak the implementation
 * contract). */
/* The v5 kind vocabulary the parser can surface. Any
 * unknown value falls back to "待核对要求" — the brief
 * is explicit that we must NEVER echo the raw English
 * enum to the user (it would leak the implementation
 * contract). The labels are deliberately phrased in
 * non-technical, question-shape terms so a PM can read
 * them at a glance. */
const TASK_KIND_LABEL: Readonly<Record<string, string>> = {
  product: '产品',
  platform: '平台',
  entry: '入口',
  operation: '操作',
  multi_select: '多选项（须多选）',
  procedure: '完整操作步骤',
  limit: '限制',
  version: '版本',
  where: '位置/语境',
  choices: '选项',
  distinguish_auto: '手动与自动的区别',
  unresolved_clause: '未解析子句',
  /* Phase 8 Research v6 — the parser can surface new
   * ``coverage_report`` kind categories. Unknown v6
   * kinds fall through to ``UNKNOWN_KIND_LABEL``
   * ('待核对要求') so we never echo the raw English
   * enum to the user — a fabricated mapping for an
   * empty category would be worse than the conservative
   * fallback. */
  capability: '能力',
  entry_listing: '入口列表',
  destination: '目标',
  condition: '条件',
  restriction: '限制',
  comparison: '对比',
  analysis: '分析',
};

const UNKNOWN_KIND_LABEL = '待核对要求';

/* Phase 8 Research v6 — ``claim_kind`` is a three-state
 * taxonomy the LLM emits on top of a finding. ``fact`` is
 * the only claim that is grounded in a cited source;
 * ``inference`` and ``recommendation`` are the LLM's own
 * synthesis ON TOP of cited facts. The page surfaces this
 * distinction because the manual-semantic-review boundary
 * is sharply different for the two — an inference /
 * recommendation is ALWAYS treated as not-verified even
 * when every supporting fact is confirmed. Unknown
 * ``claim_kind`` values fall back to a conservative
 * "分析" hint so we never echo a raw English enum. */
const CLAIM_KIND_LABEL: Readonly<Record<string, string>> = {
  fact: '事实',
  inference: '推断',
  recommendation: '建议',
};

const UNKNOWN_CLAIM_KIND_LABEL = '分析';

function claimKindLabel(kind: string | null | undefined): string {
  if (!kind) return UNKNOWN_CLAIM_KIND_LABEL;
  return CLAIM_KIND_LABEL[kind] ?? UNKNOWN_CLAIM_KIND_LABEL;
}

function coverageStatusLabel(status: string): string {
  return COVERAGE_STATUS_LABEL[status] ?? status;
}

function taskKindLabel(
  kind: string | null | undefined,
  value?: string | null,
): string {
  if (!kind) return UNKNOWN_KIND_LABEL;
  /* The parser can surface a ``(kind, value)`` pair where
   * the ``value`` is itself a program-internal token
   * rather than a user-readable sub-question:
   * ``distinguish_auto`` + ``both``,
   * ``procedure`` + ``procedure``,
   * ``multi_select`` + ``multi_select``. Echoing the raw
   * ``value`` would leak the implementation contract to
   * the PM. Each pair is mapped to a fixed Chinese
   * phrase; any other pair falls through to the plain
   * ``TASK_KIND_LABEL[kind]`` entry so legitimate user
   * sub-questions (e.g. ``product`` + '企业段是否同价')
   * still render verbatim. */
  if (kind === 'distinguish_auto' && value === 'both') {
    return '区分手动与自动操作';
  }
  if (kind === 'procedure' && value === 'procedure') {
    return '完整操作步骤';
  }
  if (kind === 'multi_select' && value === 'multi_select') {
    return '明确多选';
  }
  return TASK_KIND_LABEL[kind] ?? UNKNOWN_KIND_LABEL;
}

/** True when the parser produced a ``(kind, value)`` pair
 *  where the ``value`` is itself a program-internal token
 *  rather than a user-readable sub-question. For these
 *  pairs the page uses the refined ``taskKindLabel``
 *  output as the main row label — echoing the raw value
 *  would leak the implementation contract. */
function isProgramInternalValue(
  kind: string | null | undefined,
  value: string | null | undefined,
): boolean {
  if (!kind || !value) return false;
  if (kind === 'distinguish_auto' && value === 'both') return true;
  if (kind === 'procedure' && value === 'procedure') return true;
  if (kind === 'multi_select' && value === 'multi_select') return true;
  return false;
}

type CoverageOverallLabel = '已覆盖' | '部分覆盖' | '未验证';

/** Overall summary derived ONLY from server
 *  ``overall_completable`` and the per-item ``status`` set —
 *  no LLM confidence / no confirmed-count. The rule table
 *  is the v5 spec verbatim:
 *
 *   1. items empty / all not_verified  → 未验证
 *   2. server bool false + any covered/partial item → 部分覆盖
 *   3. server bool true + every core item covered (or
 *      not_applicable)  → 已覆盖
 *   4. anything else (e.g. true with a partial core item,
 *      true with no core items) → 未验证
 *
 * Note: a real v5 "partial" sample is
 * ``overall_completable === false`` with at least one
 * covered/partial item; a fabricated ``true + partial``
 * sample is NOT a valid case and is intentionally
 * downgraded to "未验证" rather than mis-labelled. Also:
 * a server ``overall_completable === true`` payload that
 * contains NO ``role === 'core'`` items is structurally
 * insufficient — we MUST NOT promote such a run to
 * "已覆盖" just because the boolean is true. Without any
 * core item, the report cannot be auditing the questions
 * the PM actually cares about, so the safe label is
 * "未验证". */
function deriveCoverageOverall(
  report: ResearchCoverageReport | import('../../../lib/api/types').ResearchSupportV7,
): CoverageOverallLabel {
  const items = report.items ?? [];
  if (items.length === 0) return '未验证';
  if (report.overall_completable === false) {
    const hasCoveredOrPartial = items.some(
      (i) => i.status === 'covered' || i.status === 'partial',
    );
    return hasCoveredOrPartial ? '部分覆盖' : '未验证';
  }
  // overall_completable === true
  if (items.every((i) => i.status === 'not_verified')) return '未验证';
  const coreItems = items.filter((i) => i.role === 'core');
  if (coreItems.length === 0) return '未验证';
  const allCoreCovered = coreItems.every(
    (i) => i.status === 'covered' || i.status === 'not_applicable',
  );
  return allCoreCovered ? '已覆盖' : '未验证';
}

function findTaskItem(
  contract: ResearchTaskContract | import('../../../lib/api/types').ResearchSpecV7 | null | undefined,
  requirementId: string,
): ResearchTaskContractItem | null {
  if (!contract || !('items' in contract) || !contract.items) return null;
  return contract.items.find((i) => i.id === requirementId) ?? null;
}

/** True when the parser did not produce a structured,
 *  ``parsed``-status contract — i.e. the user question
 *  could not be decomposed into ``task_contract.items``.
 *  The page surfaces a "需人工核对的要求" row in that
 *  case so a PM can review the original question. */
function isUnparsedContract(
  contract: ResearchTaskContract | import('../../../lib/api/types').ResearchSpecV7 | null | undefined,
): boolean {
  if (!contract || !('items' in contract)) return true;
  if (contract.parse_status !== 'parsed') return true;
  if (!contract.items || contract.items.length === 0) return true;
  return false;
}

/* ---------- gap text + error_message sanitisation ----------
 *
 * The server's ``coverage_report.items[].gap`` field is
 * raw server text — a mix of natural-language hints
 * (e.g. "公开网页仅披露 SMB 段；企业段需登录后台") AND
 * program-internal diagnostics (e.g. "review required
 * from human reviewer", "evidence does not match the
 * requested flow (manual vs automatic)", "insufficient_
 * external_evidence: only 2 of 5 segments cited"). The
 * page MUST translate the known diagnostic phrases to
 * fixed Chinese, AND catch any other program-internal
 * tokens (req-ids, snake_case enums, source paths,
 * ``procedure=`` markers) so they never surface to the
 * PM. Plain natural-language gaps are passed through
 * unchanged — the brief explicitly says "普通自然语言
 * gap 如 fixture 企业段需登录等保留".
 *
 * The matching is intentionally conservative: a gap
 * must FAIL through every known-phrase check before
 * we look at the internal-diagnostic heuristics, so a
 * natural-language gap that happens to contain the word
 * "review" is still preserved (e.g. "review the v3
 * pricing page"). The known-phrase anchors are
 * multi-word so a single common word can't false-
 * trigger the mapping. */

const GAP_KNOWN_PHRASE_MAPPING: ReadonlyArray<{ pattern: RegExp; label: string }> = [
  { pattern: /manual vs automatic/i, label: '尚缺手动与自动操作区别的官方证据' },
  { pattern: /official snapshot/i, label: '尚缺可核对完整步骤的官方正文' },
  { pattern: /review required/i, label: '该要求仍需结合原问题人工核对' },
];

/* Heuristics for "the gap carries program-internal
 * tokens the user must not see": a ``req-…`` handle, a
 * ``procedure=``/``procedure.``/``procedure:`` marker,
 * the word ``invariant``, a source-file ``file.ext:N``
 * pattern, or any snake_case identifier of length ≥3
 * on each side (catches ``insufficient_external_
 * evidence`` / ``coverage_not_verified`` / ``evidence_
 * source_ids`` / ``manual_vs_auto``). */
const INTERNAL_DIAGNOSTIC_PATTERN =
  /(?:req-[a-z0-9_-]+|procedure[=.:]|invariant|\b[a-z_]+\.[a-z_]+\.\w+:\d+|\b[a-z]{3,}_[a-z_]{3,}\b)/i;

function translateGap(gap: string | null | undefined): string {
  if (!gap) return '';
  const trimmed = gap.trim();
  if (!trimmed) return '';
  for (const { pattern, label } of GAP_KNOWN_PHRASE_MAPPING) {
    if (pattern.test(trimmed)) return label;
  }
  if (INTERNAL_DIAGNOSTIC_PATTERN.test(trimmed)) {
    return '现有证据尚不能覆盖该要求，请核对官方原文';
  }
  return trimmed;
}

/* ---------- v6 analysis support filter ----------
 *
 * Resolves an analysis finding's
 * ``supporting_finding_ids`` to the set of ``fact``
 * rows the page can actually show. The filter is bounded
 * to the findings the page already has in memory (the
 * ``allFindings`` argument) so it never silently follows
 * a support id into a foreign run, a different run, or a
 * row the user has not yet loaded. The hard rules are:
 *
 *   1. The id must exist in ``allFindings`` — missing /
 *      not-yet-loaded ids are silently dropped, never
 *      rendered as broken links.
 *   2. The resolved row must share ``run_id`` with the
 *      analysis finding. Foreign-run facts are dropped
 *      because the analysis run's audit is bounded to its
 *      own Run.
 *   3. The resolved row must have ``claim_kind === 'fact'``.
 *      An analysis-on-analysis chain (a recommendation
 *      citing another inference) is rejected so the page
 *      never renders a recursive "查看支持事实" tree.
 *   4. The resolved row must be ``status === 'confirmed'``
 *      AND carry at least one ``source_refs`` entry —
 *      otherwise the click would surface a link to nothing.
 *
 * The function is a pure helper (no React state) so a
 * parent can memoize it on ``finding.id`` and
 * ``allFindings`` identity. */
function findSupportingFacts(
  finding: ResearchFindingRead,
  allFindings: ResearchFindingRead[],
): ResearchFindingRead[] {
  const ids = finding.supporting_finding_ids;
  if (!ids || ids.length === 0) return [];
  const byId = new Map<string, ResearchFindingRead>();
  for (const f of allFindings) byId.set(f.id, f);
  const supports: ResearchFindingRead[] = [];
  for (const sid of ids) {
    const fact = byId.get(sid);
    if (!fact) continue;
    if (fact.run_id !== finding.run_id) continue;
    if (fact.claim_kind !== 'fact') continue;
    if (fact.status !== 'confirmed') continue;
    if (!fact.source_refs || fact.source_refs.length === 0) continue;
    supports.push(fact);
  }
  return supports;
}

/** Fixed user-facing message for the Research detail
 *  panel's ``run.error_message`` slot. The server-side
 *  string can carry internal codes (procedure names,
 *  status enums, raw exception text) — none of which
 *  are user-readable. The brief: "中文一般提示'本次研
 *  究仍有证据或执行缺口，请结合来源与逐项覆盖核对'".
 *  The actual server string is intentionally NOT
 *  rendered; the original is preserved only on the
 *  per-item ``gap`` channel and the
 *  ``failure_reasons`` line where it is sanitised. */
const RESEARCH_ERROR_MESSAGE_PLACEHOLDER =
  '本次研究仍有证据或执行缺口，请结合来源与逐项覆盖核对。';

interface ResearchDetailPanelProps {
  run: ResearchRunRead;
  findings: ResearchFindingRead[] | null;
  sources: ResearchSourceRead[] | null;
  loading: boolean;
  error: string | null;
  expanded: Record<string, boolean>;
  onToggleFinding: (id: string) => void;
  /* v6 — analysis support trace. ``supportExpanded`` tracks
   * which ``inference`` / ``recommendation`` rows the user
   * has opened so the "查看支持事实" affordance is collapsed
   * by default. The toggle is independent from the citation
   * toggle (``expanded``) so a user can have one row's
   * citations open AND a different row's supports open at
   * the same time. */
  supportExpanded: Record<string, boolean>;
  onToggleSupport: (id: string) => void;
}

function ResearchDetailPanel({
  run,
  findings,
  sources,
  loading,
  error,
  expanded,
  onToggleFinding,
  supportExpanded,
  onToggleSupport,
}: ResearchDetailPanelProps) {
  const groups = groupResearchFindings(findings);
  // Surface ``not_verified`` once at the panel top so the PM
  // can read the run's honest grounding state without having to
  // scroll every group. A run composed entirely of confirmed
  // findings reports zero — no need to surface a "100% cited"
  // banner.
  const notVerifiedCount =
    findings?.filter((f) => f.status === 'not_verified').length ?? 0;

  return (
    <div
      className="research-detail"
      data-testid="research-detail-panel"
      data-run-id={run.id}
    >
      <div className="research-detail-head">
        <div className="research-detail-question">{run.research_question}</div>
        <span
          className={`research-status-pill research-status-pill--${renderableResearchStatus(
            run.status,
          )}`}
          data-testid="research-detail-status"
        >
          {renderResearchStatusLabel(run.status)}
        </span>
      </div>
      <TechnicalDetails label="查看研究详情">
      <div className="research-detail-meta">
        <span className="item">
          <span className="k">搜索服务</span>
          <span className="v">{run.provider}</span>
        </span>
        <span className="item">
          <span className="k">提示词版本</span>
          <span className="v">{run.prompt_version}</span>
        </span>
        {run.model ? (
          <span className="item">
            <span className="k">模型</span>
            <span className="v">{run.model}</span>
          </span>
        ) : null}
        <span className="item">
          <span className="k">研究结论</span>
          <span className="v" data-testid="research-finding-count">
            {run.finding_count}
          </span>
        </span>
        <span className="item">
          <span className="k">资料来源</span>
          <span className="v" data-testid="research-source-count">
            {run.source_count}
          </span>
        </span>
        <span className="item">
          <span className="k">耗时</span>
          <span className="v">{run.latency_ms} ms</span>
        </span>
      </div>
      </TechnicalDetails>
      {/* Completeness honesty notice — the brief is explicit that
       * a "completed" run is NOT a promise the original research
       * question was fully answered. We must surface this for
       * every terminal status, never derive it from LLM
       * ``confidence`` or a confirmed-count, and never mute it
       * for ``running`` / ``pending`` so a long in-flight run
       * can't accidentally read as "complete" mid-flight.
       *
       * v5 nuance: the backend's ``completed`` gate now also
       * requires ``coverage_report.overall_completable === true``,
       * so for v5 runs the "≥1 cited fact" framing is no longer
       * accurate. We split the wording by report-presence so a
       * PM can tell at a glance whether the run was actually
       * audited (and which gaps remain) vs. a pre-v5 historical
       * run that has no coverage report at all. */}
      {run.status === 'completed' ||
      run.status === 'partial_failed' ||
      run.status === 'failed' ? (
        run.coverage_report ? (
          <div
            className="alert alert-warn"
            role="status"
            data-testid="research-answer-completeness"
          >
            <b>回答完整性：未验证</b>
            <br />
            请对照引文核实研究结果。
          </div>
        ) : (
          <div
            className="alert alert-warn"
            role="status"
            data-testid="research-answer-completeness"
          >
            <b>回答完整性：未验证</b>
            <br />
            这条历史记录尚未核对完整性，请查看原始来源。
          </div>
        )
      ) : null}

      {/* Phase 8 v7 — extractive answer renderer. The
       * backend pins the v7 envelope at
       * ``coverage_report.version === 'research-support-v7'``;
       * a v7 row MAY carry an ``answer`` payload
       * (``coverage_report.answer.version ===
       * 'research-answer-v7'``) when planning succeeded,
       * or leave it ``null`` when planning failed.
       *
       * The page detects the v7 contract from the
       * envelope version alone — NEVER from the
       * presence of ``answer``. A planning-failed v7
       * run is honest about its lack of an answer and
       * renders the v7 "规划失败 / 未验证" row, which
       * never reads any v5 field. A non-v7
       * ``coverage_report`` (a v5 / v6 audit payload)
       * falls through to the historical evidence-
       * coverage section below. The two branches are
       * strictly mutually exclusive: a v7 row MUST
       * NEVER trigger the v5 ``failure_reasons`` /
       * ``items`` / ``task_contract.items`` access. */}
      {run.coverage_report && isResearchSupportV7(run.coverage_report) ? (
        isResearchAnswerV7(run.coverage_report.answer) ? (
          <ResearchAnswer
            runId={run.id}
            answer={run.coverage_report.answer}
            findings={findings ?? []}
          />
        ) : (
          <section
            className="research-answer-v7-planning-failed"
            data-testid="research-answer-v7-planning-failed"
          >
            <header className="research-answer-v7-head">
              <span className="research-answer-v7-title">
                研究回答
              </span>
              <span
                className="research-answer-v7-meta muted"
                data-testid="research-answer-v7-meta"
              >
                尚未形成回答 · 待人工复核
              </span>
            </header>
            <p
              className="research-answer-v7-planning-failed-body muted"
              data-testid="research-answer-v7-planning-failed-body"
            >
              本次研究未形成可核对的回答，原问题仍未验证。请查看失败原因后重试。
            </p>
            {run.coverage_report.overall_completable ? (
              <p
                className="research-answer-v7-planning-failed-overall muted"
                data-testid="research-answer-v7-planning-failed-overall"
              >
                程序结果仍需人工核对。
              </p>
            ) : null}
          </section>
        )
      ) : null}

      {/* Evidence coverage audit — only when the backend
       * shipped a v5 ``coverage_report``. The section is a
       * code-rule-level summary; we render the per-item
       * requirement text (joined back to ``task_contract``),
       * the kind label, the per-item status, and the
       * server-supplied ``gap`` reason. We never collapse
       * this into "fully answered" — the server itself pins
       * ``semantic_review = 'NOT_MEASURED'`` so any
       * "已覆盖" reading is a code-rule statement, not a
       * semantic audit.
       *
       * The v5 branch and the v7 branch above are STRICTLY
       * MUTUALLY EXCLUSIVE. A v7 ``coverage_report`` pins
       * ``version === 'research-support-v7'``; reading its
       * v5 fields (``items`` / ``failure_reasons`` /
       * ``overall_completable``) would either crash on
       * undefined or — worse — silently render stale v5
       * data on top of the v7 renderer. We gate on
       * ``!isResearchSupportV7`` so the v5 paths below
       * only fire for legacy / v6 / non-v7 reports, and
       * the typing narrows ``coverage_report`` to its v5
       * shape. */}
      {run.coverage_report && !isResearchSupportV7(run.coverage_report) ? (
        (() => {
          // The v5 branch only fires when
          // ``!isResearchSupportV7(run.coverage_report)``.
          // We still defensively default the v5-only array
          // fields (``items`` / ``failure_reasons``) to
          // empty arrays so the page can never crash on a
          // future wire-drift row that carries the v7
          // envelope shape but ships without a complete
          // v5 audit payload.
          const cr = run.coverage_report;
          const items = cr.items ?? [];
          const failureReasons = cr.failure_reasons ?? [];
          return (
        <section
          className="research-coverage"
          data-testid="research-evidence-coverage"
        >
          <header className="research-coverage-head">
            <span className="research-coverage-title">
              证据覆盖：{deriveCoverageOverall(cr)}
            </span>
            <span
              className="research-coverage-meta muted"
              data-testid="research-coverage-meta"
            >
              人工语义复核：未验证
              {' · '}
              服务端已逐项核对
              {' · '}
              {items.length} 项子问题
            </span>
          </header>
          {failureReasons.length > 0 ? (
            <p
              className="muted"
              data-testid="research-coverage-failure-reasons"
            >
              服务端程序检查未完全通过；请按下方逐项缺口人工核对。
            </p>
          ) : null}
          {isUnparsedContract(run.task_contract) ? (
            <div
              className="alert alert-warn"
              data-testid="research-coverage-unparsed"
            >
              需人工核对的要求：{run.research_question}
            </div>
          ) : items.length > 0 ? (
            <ul
              className="research-coverage-items"
              data-testid="research-coverage-items"
            >
              {items.map((item) => {
                const task = findTaskItem(run.task_contract, item.requirement_id);
                const effectiveKind = item.kind || task?.kind;
                // ``unresolved_clause`` items carry an
                // internal review string in ``value`` —
                // NOT a user-readable sub-question. The
                // page MUST surface "需人工核对的要求：
                // 原问题" instead, and never echo the
                // program's review note to the PM. The
                // program-taxonomy kind label is also
                // suppressed for this branch because the
                // whole row is "需人工核对".
                if (effectiveKind === 'unresolved_clause') {
                  return (
                    <li
                      key={item.requirement_id}
                      className="research-coverage-item"
                      data-testid={`research-coverage-item-${item.requirement_id}`}
                    >
                      <div
                        className="research-coverage-item-label"
                        data-testid="research-coverage-unresolved-clause"
                      >
                        需人工核对的要求：{run.research_question}
                      </div>
                      <div className="research-coverage-item-meta muted">
                        {coverageStatusLabel(item.status)}
                      </div>
                    </li>
                  );
                }
                const label = isProgramInternalValue(effectiveKind, task?.value)
                  ? taskKindLabel(effectiveKind, task?.value)
                  : task?.value || '需人工核对的要求';
                const translatedGap = translateGap(item.gap);
                return (
                  <li
                    key={item.requirement_id}
                    className="research-coverage-item"
                    data-testid={`research-coverage-item-${item.requirement_id}`}
                  >
                    <div className="research-coverage-item-label">{label}</div>
                    <div className="research-coverage-item-meta muted">
                      {taskKindLabel(effectiveKind, task?.value)} ·{' '}
                      {coverageStatusLabel(item.status)}
                    </div>
                    {translatedGap ? (
                      <p
                        className="research-coverage-item-gap muted"
                        data-testid={`research-coverage-gap-${item.requirement_id}`}
                      >
                        缺口：{translatedGap}
                      </p>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : (
            <div
              className="alert alert-warn"
              data-testid="research-coverage-unparsed"
            >
              需人工核对的要求：{run.research_question}
            </div>
          )}
        </section>
          );
        })()
      ) : null}

      {run.error_message ? (
        <div
          className="research-detail-error"
          data-testid="research-detail-error"
        >
          {RESEARCH_ERROR_MESSAGE_PLACEHOLDER}
        </div>
      ) : null}

      {loading ? <LoadingState /> : null}

      {error ? (
        <div
          className="research-unavailable"
          role="alert"
          data-testid="research-detail-error-banner"
        >
          <span className="research-unavailable-title">
            研究暂不可用
          </span>
          {error}
        </div>
      ) : null}

      {notVerifiedCount > 0 ? (
        <div
          className="alert alert-warn"
          data-testid="research-not-verified-banner"
        >
          本次研究含 {notVerifiedCount} 条 <b>未验证</b> 结论，请结合来源复核后使用。
        </div>
      ) : null}

      {analysisGapMessages(run.execution_trace).length > 0 ? (
        <div className="alert alert-warn" data-testid="research-analysis-gaps">
          {analysisGapMessages(run.execution_trace).map((message) => <p key={message}>{message}</p>)}
        </div>
      ) : null}

      {groups.length > 0 ? (
        <div className="research-findings" data-testid="research-findings">
          {groups.map((group) => (
            <div
              key={group.category}
              className="research-findings-group"
              data-testid={`research-group-${group.category}`}
            >
              <div className="research-findings-group-label">
                {group.label} · {group.findings.length}
              </div>
              {group.findings.map((finding) => (
                <ResearchFindingRow
                  key={finding.id}
                  finding={finding}
                  expanded={!!expanded[finding.id]}
                  onToggle={() => onToggleFinding(finding.id)}
                  allFindings={findings ?? []}
                  supportExpanded={!!supportExpanded[finding.id]}
                  onToggleSupport={() => onToggleSupport(finding.id)}
                />
              ))}
            </div>
          ))}
        </div>
      ) : null}

      {sources && sources.length > 0 ? (
        <div className="research-sources" data-testid="research-sources">
          <div className="research-section-divider">
            资料来源 · {sources.length}
          </div>
          <ul>
            {sources.map((source) => (
              <li
                key={source.id}
                className="research-source-card"
                data-testid={`research-source-${source.id}`}
              >
                <div className="research-source-title">
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer nofollow"
                    data-testid={`research-source-link-${source.id}`}
                  >
                    {source.title ?? source.url}
                  </a>
                </div>
                <div className="research-source-meta">
                  <span>{source.domain}</span>
                  <span>获取于 {formatRelative(source.retrieved_at)}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

interface ResearchFindingRowProps {
  finding: ResearchFindingRead;
  expanded: boolean;
  onToggle: () => void;
  /* v6 — analysis support trace. ``allFindings`` is the
   * currently-loaded finding list (the same slice the
   * panel rendered from). The row filters
   * ``finding.supporting_finding_ids`` against this list
   * so the page never follows a support id into a foreign
   * run or a row that has not been loaded. */
  allFindings: ResearchFindingRead[];
  supportExpanded: boolean;
  onToggleSupport: () => void;
}

function ResearchFindingRow({
  finding,
  expanded,
  onToggle,
  allFindings,
  supportExpanded,
  onToggleSupport,
}: ResearchFindingRowProps) {
  // ``status`` mirrors the backend enum (``confirmed`` /
  // ``not_verified``). The page surfaces both verbatim and
  // adds a third badge ``Insufficient Evidence`` for any
  // competitor / market_pattern row that lands without sources
  // — the backend actually returns ``not_verified`` for those,
  // but the UI label clarifies the WHY so the PM never mistakes
  // "the LLM refused to fake it" for "we'll come back to it".
  const statusClass =
    finding.status === 'confirmed'
      ? 'research-finding-tag--confirmed'
      : 'research-finding-tag--not_verified';
  const statusLabel =
    finding.status === 'confirmed' ? '有引用' : '未验证';
  const insufficient =
    finding.claim_kind !== 'inference' && finding.claim_kind !== 'recommendation' &&
    finding.category !== 'opportunity' &&
    finding.category !== 'risk' &&
    finding.status !== 'confirmed';
  const tagClass = insufficient
    ? 'research-finding-tag--insufficient_evidence'
    : statusClass;
  const tagLabel = insufficient ? '证据不足' : statusLabel;
  const citations = finding.source_refs ?? [];
  /* v6 — analysis support trace. The toggle is shown
   * whenever the row declares at least one support id;
   * legacy rows (no ``supporting_finding_ids``) keep the
   * pre-v6 behaviour. The supports list is filtered to
   * same-Run, ``claim_kind === 'fact'``,
   * ``status === 'confirmed'`` rows that are currently
   * loaded — foreign / unverified / unknown ids are
   * silently dropped, never rendered as broken links. */
  const supportingIds = finding.supporting_finding_ids ?? [];
  const hasSupportToggle = supportingIds.length > 0;
  const supports = hasSupportToggle
    ? findSupportingFacts(finding, allFindings)
    : [];
  const claimKindText = finding.claim_kind
    ? claimKindLabel(finding.claim_kind)
    : null;
  return (
    <div
      className="research-finding"
      data-testid={`research-finding-${finding.id}`}
    >
      <div className="research-finding-head">
        <span className="research-finding-title">{finding.title}</span>
        {claimKindText ? (
          <span
            className="research-finding-claim-kind"
            data-testid={`research-claim-kind-${finding.id}`}
          >
            {claimKindText}
          </span>
        ) : null}
        <span
          className={`research-finding-tag ${tagClass}`}
          data-testid={`research-finding-status-${finding.id}`}
        >
          {tagLabel}
        </span>
        {finding.confidence !== null && finding.confidence !== undefined ? (
          <span className="research-finding-confidence">
            置信度 {finding.confidence.toFixed(2)}
          </span>
        ) : null}
      </div>
      <p className="research-finding-description">{finding.description}</p>
      {citations.length > 0 ? (
        <button
          type="button"
          className="research-finding-toggle"
          onClick={onToggle}
          data-testid={`research-finding-toggle-${finding.id}`}
          aria-expanded={expanded}
        >
          {expanded ? '隐藏引用' : `查看来源 (${citations.length})`}
        </button>
      ) : null}
      {expanded && citations.length > 0 ? (
        <div
          className="research-finding-citations"
          data-testid={`research-finding-citations-${finding.id}`}
        >
          <ul>
            {citations.map((ref, idx) => (
              <li key={`${ref.provider_source_id}-${idx}`}>
                <a
                  href={ref.url}
                  target="_blank"
                  rel="noopener noreferrer nofollow"
                >
                  {ref.title}
                </a>
                <span className="ref-domain">
                  {(() => {
                    try {
                      return new URL(ref.url).hostname;
                    } catch {
                      return ref.url;
                    }
                  })()}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {hasSupportToggle ? (
        <button
          type="button"
          className="research-finding-support-toggle"
          onClick={onToggleSupport}
          data-testid={`research-support-toggle-${finding.id}`}
          aria-expanded={supportExpanded}
        >
          {supportExpanded ? '隐藏支持事实' : '查看支持事实'}
        </button>
      ) : null}
      {hasSupportToggle && supportExpanded ? (
        <div
          className="research-finding-supports"
          data-testid={`research-supports-${finding.id}`}
        >
          {supports.length === 0 ? (
            <p className="research-finding-supports-empty muted">
              本次研究中尚无可支持这项分析的已确认事实。
            </p>
          ) : (
            <ul>
              {supports.map((fact) => (
                <li
                  key={fact.id}
                  className="research-finding-support-item"
                  data-testid={`research-support-item-${finding.id}-${fact.id}`}
                >
                  <p className="research-finding-support-description">
                    {fact.description}
                  </p>
                  {fact.source_refs && fact.source_refs.length > 0 ? (
                    <ul className="research-finding-support-citations">
                      {fact.source_refs.map((ref, idx) => (
                        <li
                          key={`${fact.id}-${ref.provider_source_id}-${idx}`}
                        >
                          <a
                            href={ref.url}
                            target="_blank"
                            rel="noopener noreferrer nofollow"
                          >
                            {ref.title}
                          </a>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
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
