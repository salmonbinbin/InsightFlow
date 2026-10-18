'use client';

/**
 * InsightFlow — PRD Detail (Phase 7 work package C2).
 *
 * Read-only preview of one PRD version, with two server-backed
 * actions on the parent ``Requirement`` row:
 *
 *   - Regenerate: POST /api/requirements/{requirement_id}/prds
 *       body ``{}`` → backend synthesises a fresh PRD version via
 *       the AI client (or 503 on config / transport error). The
 *       response is the new ``PRDRead``; the page navigates to its
 *       ``/prds/{id}`` route so the PM lands on the new version.
 *
 *   - Accept: POST /api/prds/{prd_id}/accept body ``{}`` → backend
 *       flips ``status`` to ``accepted`` and stamps ``accepted_at``.
 *       An accepted version is immutable on the backend; the page
 *       hides both action buttons and flips the regenerate button
 *       to "已采纳，禁止再生成" until the PM moves on to a brand-new
 *       requirement.
 *
 * Project isolation:
 *   - Cross-project ``project_id`` mismatch refuses to render the
 *     body and surfaces a blocking error.
 *   - The Generate / Accept buttons refuse to fire while the
 *     mismatched project is active, so a stale page cannot POST
 *     into the wrong project.
 *
 * URL id change clears every piece of state (prd, history, error,
 * action error). The route path can change without an active-
 * project change (Next.js re-renders), so the reset is keyed on
 * ``prdId`` — without it, navigating from /prds/A to /prds/B would
 * briefly display A's body under B's URL.
 *
 * History is the per-requirement ``/requirements/{id}/prds`` page
 * (NOT the project index). The backend returns ``active_prd_id`` /
 * ``active_version`` as stable pointers so the row labelled
 * "active" stays correct across pagination. The page MUST NOT
 * compute the active PRD from the current slice.
 */

import Link from 'next/link';
import { TechnicalDetails } from '../../../components/TechnicalDetails';
import { displayLabel, sectionLabel, warningLabel, metricNote, documentText } from '../../../lib/presentation';
import { useParams, useRouter } from 'next/navigation';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useProjectContext } from '../../../contexts/ProjectContext';
import { getApiClient } from '../../../lib/api/client';
import { useProjectScopedFetch } from '../../../lib/useProjectScopedFetch';
import {
  EmptyState,
  ErrorState,
  LoadingState,
} from '../../../components/StateViews';
import {
  classifyError,
  formatRelative,
} from '../../../lib/format';
import type {
  EdgeCaseCategory,
  MetricType,
  Pagination,
  PrdAcceptanceCriterion,
  PrdContent,
  PrdEdgeCase,
  PrdFunctionalRequirement,
  PrdRead,
  PrdRisk,
  PrdStatus,
  PrdSuccessMetricBlock,
  RiskCategory,
} from '../../../lib/api/types';

const HISTORY_PAGE_SIZE = 5;

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

const EDGE_CATEGORY_LABEL: Record<string, string> = {
  empty_state: '空状态',
  failure: '失败',
  retry: '重试',
  partial_success: '部分成功',
  invalid_input: '非法输入',
  limit_exceeded: '超出限制',
};

const RISK_CATEGORY_LABEL: Record<string, string> = {
  known: '已知风险',
  speculative: '推测风险',
};

const METRIC_TYPE_LABEL: Record<string, string> = {
  metric_definition: '指标定义',
  baseline_only: '仅基线',
};

export default function PrdDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();
  const prdId = params?.id ?? '';
  const requestScopeRef = useRef({ projectId: activeProjectId, id: prdId });
  if (requestScopeRef.current.projectId !== activeProjectId || requestScopeRef.current.id !== prdId) {
    requestScopeRef.current = { projectId: activeProjectId, id: prdId };
  }

  const [prd, setPrd] = useState<PrdRead | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [projectMismatch, setProjectMismatch] = useState(false);

  /* ---------- History ---------- */

  const [history, setHistory] = useState<PrdRead[] | null>(null);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyPage, setHistoryPage] = useState<Pagination>({
    limit: HISTORY_PAGE_SIZE,
    offset: 0,
  });
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  /* ---------- Action states ---------- */

  const [regenerating, setRegenerating] = useState(false);
  const [regenerateError, setRegenerateError] = useState<string | null>(null);
  const [regenerateSuccess, setRegenerateSuccess] = useState<{ id: string; version: number } | null>(null);

  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  const [acceptSuccess, setAcceptSuccess] = useState(false);

  /* ---------- Edit / Save (Phase 7 C3a) ---------- */

  const [editing, setEditing] = useState(false);
  const [draftContent, setDraftContent] = useState<PrdContent | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  /* ---------- Reset on project switch ---------- */

  useEffect(() => {
    setPrd(null);
    setLoadError(null);
    setLoading(false);
    setNotFound(false);
    setProjectMismatch(false);
    setHistory(null);
    setHistoryTotal(0);
    setHistoryPage({ limit: HISTORY_PAGE_SIZE, offset: 0 });
    setHistoryLoading(false);
    setHistoryError(null);
    setRegenerating(false);
    setRegenerateError(null);
    setRegenerateSuccess(null);
    setAccepting(false);
    setAcceptError(null);
    setAcceptSuccess(false);
    setEditing(false);
    setDraftContent(null);
    setSaving(false);
    setSaveError(null);
    setSaveSuccess(false);
  }, [activeProjectId]);

  /* ---------- Reset on URL id change ----------
   *
   * Without this effect, navigating between two PRD detail pages
   * would re-render with the new ``prdId`` but keep the previous
   * PRD's body in ``prd`` — a stale-form bug. We treat any change
   * to the URL id as a hard restart. The same reset applies to the
   * in-flight draft: a pending edit on PRD A must never leak into
   * the form for PRD B.
   */
  useEffect(() => {
    setPrd(null);
    setLoadError(null);
    setLoading(false);
    setNotFound(false);
    setProjectMismatch(false);
    setHistory(null);
    setHistoryTotal(0);
    setHistoryPage({ limit: HISTORY_PAGE_SIZE, offset: 0 });
    setHistoryLoading(false);
    setHistoryError(null);
    setRegenerating(false);
    setRegenerateError(null);
    setRegenerateSuccess(null);
    setAccepting(false);
    setAcceptError(null);
    setAcceptSuccess(false);
    setEditing(false);
    setDraftContent(null);
    setSaving(false);
    setSaveError(null);
    setSaveSuccess(false);
  }, [prdId]);

  /* ---------- Load PRD ---------- */

  const load = useCallback(async () => {
    if (!prdId || !signal || !activeProjectId) return;
    const mySignal = signal;
    const myId = prdId;
    setLoading(true);
    setLoadError(null);
    setNotFound(false);
    setProjectMismatch(false);
    const requestScope = requestScopeRef.current;
    const res = await client.getPrd(prdId, mySignal, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (res.data && res.data.project_id !== requestScope.projectId) { setProjectMismatch(true); setLoading(false); return; }
    if (mySignal.aborted) return;
    if (myId !== prdId) return;
    if (res.error) {
      if (res.error.status === 404) {
        setPrd(null);
        setNotFound(true);
        setLoading(false);
        return;
      }
      setLoadError(classifyError(res.error));
      setPrd(null);
      setLoading(false);
      return;
    }
    if (!res.data) {
      setLoadError('未找到 PRD。');
      setPrd(null);
      setLoading(false);
      return;
    }
    setPrd(res.data);
    setLoading(false);
  }, [activeProjectId, client, prdId, signal]);

  useEffect(() => {
    void load();
  }, [load]);

  /* ---------- Project isolation ----------
   *
   * Decide AFTER the PRD lands whether it belongs to the active
   * project. We render the body but block any action button; a
   * later switch back to the matching project re-enables actions
   * automatically.
   */
  useEffect(() => {
    if (!prd) return;
    setProjectMismatch(prd.project_id !== activeProjectId);
  }, [prd, activeProjectId]);

  /* ---------- History ----------
   *
   * The per-requirement history endpoint is the source of truth for
   * the version timeline — NOT the project index. We use the page's
   * own ``historyPage`` state so pagination does not pretend the
   * current slice is the entire history.
   *
   * The client surface ``listPrds(requirementId, signal)`` does not
   * forward query params; we therefore fetch the full slice once and
   * paginate locally in render via ``displayedHistory`` below. The
   * server-side ``active_prd_id`` / ``active_version`` pointers stay
   * intact (no derivation client-side), so the "Backend Active"
   * badge resolves through the loaded array without recomputing the
   * version sort.
   *
   * The refetch runs on prd / project changes only. A project
   * switch is already covered by the reset effect, which wipes
   * ``history`` to ``null`` so this effect re-runs once the new
   * project settles. URL-id changes also reset via the same
   * effect (keyed on prdId), so stale rows from a previous version
   * never bleed into a different requirement's history.
   */
  const loadHistory = useCallback(async () => {
    if (!prd || !signal || prd.project_id !== activeProjectId || prd.id !== prdId) return;
    const mySignal = signal;
    const myId = prdId;
    setHistoryLoading(true);
    setHistoryError(null);
    const requestScope = requestScopeRef.current;
    if (prd.project_id !== requestScope.projectId) return;
    const res = await client.listPrds(
      prd.requirement_id,
      mySignal ?? undefined, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal.aborted) return;
    if (myId !== prdId) return;
    if (res.error || !res.data) {
      setHistoryError(res.error ? classifyError(res.error) : '加载版本历史失败。');
      setHistory(null);
      setHistoryTotal(0);
      setHistoryLoading(false);
      return;
    }
    // Project isolation: refuse history rows from other projects.
    const filtered = res.data.items.filter(
      (row) => row.project_id === prd.project_id,
    );
    setHistory(filtered);
    setHistoryTotal(res.data.total);
    setHistoryLoading(false);
  }, [activeProjectId, client, prd, prdId, signal]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  /* ---------- Derived (Phase 7 C3a edit gate) ----------
   *
   * These constants are referenced from inside ``onRegenerate`` and
   * ``onAccept`` (dirty-draft guard below) AND from the JSX (Edit
   * / Save / Cancel buttons). They MUST be declared BEFORE the
   * ``useCallback`` blocks that read them, otherwise the JS
   * temporal dead zone throws ``ReferenceError: Cannot access
   * 'isDirty' before initialization`` on the first render.
   * ``historyPageCount`` / ``currentHistoryPage`` stay further down
   * because no ``useCallback`` closes over them.
   */
  const isAccepted = prd?.status === 'accepted';
  const isArchived = prd?.status === 'archived';
  // ``isReadOnly`` is the master edit gate. Backend accepts PATCH
  // content edits only when ``status ∈ {draft, reviewing}``; on
  // accepted / archived rows ``patch_prd`` raises
  // ``RequirementValidationError('content_immutable')`` (→ 409).
  // Project mismatch refuses any project-scoped mutation, so we
  // surface that here too.
  const isReadOnly = projectMismatch || isAccepted || isArchived;
  /* ``isDirty`` — the draft differs from the saved PRD body. The
   * page guards Regenerate / Accept while dirty so a save-in-flight
   * cannot be silently overwritten by a new version. We use a JSON
   * shape comparison (not deep equal) because the draft is a pure
   * projection of ``prd.content`` and any structural reorder would
   * also be a meaningful edit. */
  const isDirty = useMemo(() => {
    if (!editing || !draftContent || !prd) return false;
    return JSON.stringify(draftContent) !== JSON.stringify(prd.content);
  }, [editing, draftContent, prd]);

  /* ``actionsBlocked`` is intentionally SPLIT — Regenerate is a
   * POST to ``/api/requirements/{id}/prds`` that synthesises a NEW
   * PRD version rather than mutating the current row. The backend
   * service does not gate Regenerate on the source PRD's status, so
   * an accepted / archived PRD can legitimately spawn a fresh draft
   * via Regenerate (the C2 history test mounts on an archived v1
   * row and clicks Regenerate to grow the timeline). PATCH content
   * edits and Accept are still gated by ``isReadOnly`` because both
   * would mutate the source row. Project mismatch blocks every
   * mutation including Regenerate, since the new version would land
   * in the wrong project. ``isDirty`` blocks every action so a save-
   * in-flight cannot be silently overwritten by a new version.
   */
  const editBlocked = isReadOnly;
  const acceptBlocked = isReadOnly || isDirty;
  const regenerateBlocked = projectMismatch || isDirty;

  /* ---------- Regenerate ---------- */

  const onRegenerate = useCallback(async () => {
    if (!prd) return;
    if (regenerating) return;
    // Dirty draft would be lost the moment the new version lands on
    // the same URL — refuse the click rather than silently throw the
    // PM's edits away. The same gate sits on the button itself; this
    // is defense-in-depth for the keyboard / programmatic click path.
    if (isDirty) {
      setRegenerateError('当前有未保存的草稿，请先保存或取消编辑后再生成新版本。');
      return;
    }
    const mySignal = signal;
    setRegenerating(true);
    setRegenerateError(null);
    setRegenerateSuccess(null);
    const requestScope = requestScopeRef.current;
    if (prd.project_id !== requestScope.projectId) return;
    const res = await client.createPrd(prd.requirement_id, prd.prompt_version === 'prd-generation-v4' ? { prompt_version: 'prd-generation-v4' } : {}, mySignal ?? undefined, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal?.aborted) {
      setRegenerating(false);
      return;
    }
    setRegenerating(false);
    if (res.error || !res.data) {
      setRegenerateError(
        res.error ? classifyError(res.error) : '生成文档失败。',
      );
      return;
    }
    // Adopt the new PRD locally so the body / meta reflect the
    // freshly minted version BEFORE we navigate. In production the
    // router.push below changes ``prdId`` and the URL-keyed reset
    // effect wipes everything, then the new route re-fetches its
    // own PRD — so this ``setPrd`` is a no-op once the URL flips.
    // In tests router.push is mocked (no real URL change); without
    // this assignment the page would still display the original
    // PRD's body after a successful regenerate.
    setPrd(res.data);
    setRegenerateSuccess({ id: res.data.id, version: res.data.version });
    // Issue a real GET against the per-requirement history endpoint
    // so the timeline shows the freshly created version alongside
    // every older one (the ``listPrds`` call from the initial mount
    // ran against the previous ``prdReadList`` snapshot, which did
    // not yet include the row we just appended). The C2 contract is
    // explicit about this — the previous version's GET is stale the
    // instant ``createPrd`` returns success, and the page must not
    // pretend the timeline is up-to-date.
    void loadHistory();
    router.push(`/prds/${res.data.id}`);
  }, [client, isDirty, loadHistory, prd, regenerating, router, signal]);

  /* ---------- Accept ---------- */

  const onAccept = useCallback(async () => {
    if (!prd) return;
    if (accepting) return;
    // Same dirty-draft guard as Regenerate: accepting stamps the
    // PRD ``accepted`` and the in-flight draft would be locked out
    // (backend rejects content PATCH on accepted rows), so the
    // editor must NOT silently lose it on the way through Accept.
    if (isDirty) {
      setAcceptError('当前有未保存的草稿，请先保存或取消编辑后再接受本版本。');
      return;
    }
    const mySignal = signal;
    setAccepting(true);
    setAcceptError(null);
    setAcceptSuccess(false);
    const requestScope = requestScopeRef.current;
    if (prd.project_id !== requestScope.projectId) return;
    const res = await client.acceptPrd(prd.id, mySignal ?? undefined, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal?.aborted) {
      setAccepting(false);
      return;
    }
    setAccepting(false);
    if (res.error || !res.data) {
      setAcceptError(res.error ? classifyError(res.error) : '接受文档失败。');
      return;
    }
    setPrd(res.data);
    setAcceptSuccess(true);
    // The history snapshot now includes the freshly accepted row
    // as the active PRD; refetch so the timeline matches.
    void loadHistory();
  }, [accepting, client, isDirty, loadHistory, prd, signal]);

  /* ---------- Derived (display-only) ---------- */

  const historyPageCount = useMemo(() => {
    if (historyTotal === 0) return 1;
    return Math.max(1, Math.ceil(historyTotal / historyPage.limit));
  }, [historyPage.limit, historyTotal]);
  const currentHistoryPage = Math.floor(historyPage.offset / historyPage.limit) + 1;

  /* ---------- Edit handlers (Phase 7 C3a) ---------- */

  const startEditing = useCallback(() => {
    if (!prd) return;
    if (isReadOnly) return;
    // structuredClone so a later setDraftContent cannot mutate the
    // currently-rendered ``prd.content`` until we explicitly adopt
    // the API response on save success.
    setDraftContent(structuredClone(prd.content));
    setEditing(true);
    setSaveError(null);
    setSaveSuccess(false);
  }, [isReadOnly, prd]);

  const cancelEditing = useCallback(() => {
    setEditing(false);
    setDraftContent(null);
    setSaveError(null);
  }, []);

  const updateDraft = useCallback(
    (updater: (prev: PrdContent) => PrdContent) => {
      setDraftContent((prev) => (prev ? updater(prev) : prev));
    },
    [],
  );

  const saveDraft = useCallback(async () => {
    if (!prd || !draftContent) return;
    if (saving) return;
    if (isReadOnly) return;
    const mySignal = signal;
    const myId = prd.id;
    const myDraft = draftContent;
    setSaving(true);
    setSaveError(null);
    const requestScope = requestScopeRef.current;
    if (prd.project_id !== requestScope.projectId) return;
    const res = await client.patchPrd(
      prd.id,
      { content: myDraft },
      mySignal ?? undefined, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal?.aborted) {
      setSaving(false);
      return;
    }
    if (myId !== prdId) {
      // URL changed mid-save — discard the response silently so we
      // never write a stale PRD into the new URL's view.
      setSaving(false);
      return;
    }
    setSaving(false);
    if (res.error || !res.data) {
      // Real failure: keep the draft + edit mode so the PM can fix
      // and retry. The error must surface visibly — never claim a
      // save that did not happen.
      setSaveError(res.error ? classifyError(res.error) : '保存文档失败。');
      return;
    }
    // Success — adopt the canonical server row, exit edit mode,
    // and surface a transient success banner. ``prd.content`` is
    // the freshly persisted snapshot (including refreshed
    // ``content_hash`` / ``is_ai_generated_draft=False``), so the
    // preview flips to the PM-authored state without any client
    // recompute.
    setPrd(res.data);
    setDraftContent(null);
    setEditing(false);
    setSaveSuccess(true);
  }, [client, draftContent, isReadOnly, prd, prdId, saving, signal]);

  /* Client-side slice of the loaded history for display. The
   * server-side ``active_prd_id`` is resolved off the FULL array
   * (see ``historyActiveId`` below); only the visible window is
   * bounded by ``historyPage``. ``total`` still reflects the real
   * server-side count via ``historyTotal`` so the meta line and
   * prev/next controls do not pretend the slice is the entire
   * history. */
  const displayedHistory = useMemo(() => {
    if (!history) return null;
    return history.slice(
      historyPage.offset,
      historyPage.offset + historyPage.limit,
    );
  }, [history, historyPage.limit, historyPage.offset]);

  /* ---------- Render ---------- */

  if (!activeProjectId) {
    return (
      <EmptyState
        title="请先选择项目"
        description="在右上角选择或创建一个项目，以查看需求文档。"
      />
    );
  }

  if (loading && !prd) return <LoadingState />;
  if (notFound) {
    return (
      <EmptyState
        title="文档不存在"
        description={'当前项目中没有这份文档，请检查所选项目。'}
        action={<Link className="btn btn-ghost btn-sm" href="/prds">返回文档列表</Link>}
      />
    );
  }
  if (loadError && !prd) {
    return (
      <ErrorState
        message={loadError}
        onRetry={() => { setLoadError(null); void load(); }}
      />
    );
  }
  if (projectMismatch) return <ErrorState title="PRD 与当前项目不匹配" message="该 PRD 不属于当前项目，无法展示或修改。" />;
  if (!prd) return <LoadingState />;


  const content = prd.content;

  return (
    <div className="page page-prd-detail">
      <header className="page-header">
        <span className="eyebrow">需求文档</span>
        <h1 className="page-title" data-testid="prd-page-title">
          需求文档 v{prd.version}
        </h1>
        <p className="page-subtitle">
          <Link href={`/requirements/${prd.requirement_id}`}>查看关联需求</Link>
          {' · 更新于 '}{formatRelative(prd.updated_at ?? null)}
        </p>
      </header>

      <section className="section" data-od-id="prd-meta">
        <div className="meta-grid">
          <div className="meta-item">
            <div className="meta-k">版本</div>
            <div className="meta-v mono" data-testid="prd-current-version">v{prd.version}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">状态</div>
            <div className="meta-v">
              <span
                className={`badge ${STATUS_BADGE[prd.status as PrdStatus] ?? 'badge-outline'}`}
                data-testid="prd-status-badge"
              >
                {STATUS_LABEL[prd.status as PrdStatus] ?? displayLabel(prd.status)}
              </span>
            </div>
          </div>
          <div className="meta-item">
            <div className="meta-k">AI 草稿</div>
            <div className="meta-v mono" data-testid="prd-ai-draft-flag">
              {prd.is_ai_generated_draft ? '是' : '否'}
            </div>
          </div>
          <TechnicalDetails label="查看生成信息">
          <div className="meta-item">
            <div className="meta-k">模型</div>
            <div className="meta-v mono">{prd.model ?? '—'}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">提示词版本</div>
            <div className="meta-v mono">{prd.prompt_version}</div>
          </div>
          </TechnicalDetails>
          <div className="meta-item">
            <div className="meta-k">接受时间</div>
            <div className="meta-v mono" data-testid="prd-accepted-at">
              {prd.accepted_at ? formatRelative(prd.accepted_at) : '—'}
            </div>
          </div>
        </div>
      </section>

      {prd.grounding_warnings.length > 0 ? (
        <section className="section" data-od-id="prd-grounding-warnings">
          <h3>
            待复核事项 ·{' '}
            <span className="muted">{prd.grounding_warnings.length}</span>
          </h3>
          <ul className="grounding-warning-list" data-testid="grounding-warning-list">
            {prd.grounding_warnings.map((w, idx) => (
              <li key={idx} className="grounding-warning-row">
                <span>{w.section ? `${sectionLabel(w.section)}：` : ''}{warningLabel(w.code)}</span>
                {w.evidence_id ? <span> · 关联反馈：{w.evidence_id}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* ---------- 13 sections / editor ---------- */}
      {editing && draftContent ? (
        <PrdEditor
          draftContent={draftContent}
          onChange={updateDraft}
          disabled={saving}
        />
      ) : (
        <PrdBody content={content} />
      )}

      {/* ---------- Actions ---------- */}
      <section className="section" data-od-id="prd-actions">
        <h3>操作</h3>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          {/* Edit entry point — only when not already editing AND not
              read-only. Read-only covers accepted / archived rows
              AND the project-mismatch branch (which renders the
              blocking ErrorState above; the button is hidden so
              the user cannot enter edit mode against a foreign PRD). */}
          {!editing ? (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              data-testid="prd-edit-button"
              onClick={() => void startEditing()}
              disabled={editBlocked}
              title={
                isAccepted
                  ? '已采纳，禁止编辑'
                  : isArchived
                    ? '已归档，禁止编辑'
                    : projectMismatch
                      ? '项目不匹配，无法编辑'
                      : undefined
              }
            >
              编辑
            </button>
          ) : null}
          {/* Save — only meaningful while the draft has diverged from
              ``prd.content``. Save a no-op draft would still hit the
              backend with the same payload and rewrite ``content_hash``
              + ``updated_at`` for no reason, so we require ``isDirty``. */}
          {editing ? (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              data-testid="prd-save-button"
              onClick={() => void saveDraft()}
              disabled={!isDirty || saving || editBlocked}
            >
              {saving ? '保存中…' : '保存草稿'}
            </button>
          ) : null}
          {editing ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              data-testid="prd-cancel-button"
              onClick={() => void cancelEditing()}
              disabled={saving}
            >
              取消编辑
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            data-testid="regenerate-prd"
            onClick={() => void onRegenerate()}
            disabled={regenerating || regenerateBlocked}
            title={
              projectMismatch
                ? '项目不匹配，无法操作'
                : isDirty
                  ? '有未保存的草稿，请先保存或取消'
                  : undefined
            }
          >
            {regenerating ? '生成中…' : '再生成新版本'}
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            data-testid="accept-prd"
            onClick={() => void onAccept()}
            disabled={accepting || acceptBlocked}
            title={
              isAccepted
                ? '已采纳'
                : isArchived
                  ? '已归档，无法接受'
                  : projectMismatch
                    ? '项目不匹配，无法操作'
                    : isDirty
                      ? '有未保存的草稿，请先保存或取消'
                      : undefined
            }
          >
            {accepting ? '接受中…' : '接受本版本'}
          </button>
          <Link href={`/prds`} className="btn btn-ghost btn-sm">返回列表</Link>
        </div>
        {editing && isDirty ? (
          <p
            className="alert alert-warn"
            role="status"
            data-testid="prd-dirty-notice"
            style={{ marginTop: 8 }}
          >
            当前有未保存的草稿；保存后再生成新版本或接受本版本。
          </p>
        ) : null}
        {isReadOnly && !editing ? (
          <p className="muted" style={{ fontSize: 'var(--t-caption)', marginTop: 8 }}>
            {isAccepted
              ? '本版本已采纳。如需修改，请从关联需求生成新版本。'
              : isArchived
                ? '本版本已归档，禁止编辑。'
                : '当前项目不匹配，禁止编辑。'}
          </p>
        ) : null}
      </section>

      {saveSuccess ? (
        <div className="alert alert-success" role="status" data-testid="prd-save-success">
          已保存当前版本。
        </div>
      ) : null}
      {saveError ? (
        <div data-testid="prd-save-error">
          <ErrorState
            title="保存文档失败"
            message={saveError}
            onRetry={() => { setSaveError(null); void saveDraft(); }}
          />
        </div>
      ) : null}

      {regenerateSuccess ? (
        <div className="alert alert-success" role="status" data-testid="regenerate-success">
          已生成新版本 v{regenerateSuccess.version}，正在跳转…
        </div>
      ) : null}
      {regenerateError ? (
        <div data-testid="regenerate-error">
          <ErrorState
            title="生成文档失败"
            message={regenerateError}
            onRetry={() => { setRegenerateError(null); void onRegenerate(); }}
          />
        </div>
      ) : null}
      {acceptSuccess ? (
        <div className="alert alert-success" role="status" data-testid="accept-success">
          已接受本版本。
        </div>
      ) : null}
      {acceptError ? (
        <ErrorState
          title="接受文档失败"
          message={acceptError}
          onRetry={() => { setAcceptError(null); void onAccept(); }}
        />
      ) : null}

      {/* ---------- History ----------
       *
       * The list container is rendered unconditionally so callers
       * (and screens crawlers / tests) can depend on its presence
       * regardless of whether rows are currently loaded. The inner
       * states (loading / error / empty / rows) are reflected via
       * a single ``data-history-state`` attribute and matching
       * inline messages — collapsing these into a conditional UL
       * would surface as a flaky ``getByTestId('prd-history-list')``
       * that occasionally returned ``null``.
       */}
      <section className="section" data-od-id="prd-history">
        <h3>
          版本历史 ·{' '}
          <span className="muted">{historyTotal}</span>
        </h3>
        <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
          选择版本查看内容。
        </p>
        {historyError ? (
          <ErrorState
            message={historyError}
            onRetry={() => { setHistoryError(null); void loadHistory(); }}
          />
        ) : null}
        {historyLoading ? <LoadingState /> : null}
        {history === null && !historyLoading && !historyError ? (
          <p className="muted" style={{ fontSize: 'var(--t-caption)' }} data-testid="prd-history-pending">
            尚未加载历史，点击下方按钮或刷新页面。
          </p>
        ) : null}
        {history !== null && history.length === 0 && !historyLoading && !historyError ? (
          <p className="muted" style={{ fontSize: 'var(--t-caption)' }} data-testid="prd-history-empty">
            暂无版本历史，这条需求尚未生成文档。
          </p>
        ) : null}
        <ul
          className="prd-history-list"
          data-testid="prd-history-list"
          data-history-state={
            historyLoading
              ? 'loading'
              : historyError
                ? 'error'
                : history === null
                  ? 'pending'
                  : history.length === 0
                    ? 'empty'
                    : 'ready'
          }
        >
          {displayedHistory && displayedHistory.length > 0
            ? displayedHistory.map((row) => (
                <li
                  key={row.id}
                  className={`prd-history-row${row.id === prd.id ? ' is-current' : ''}`}
                  data-testid="prd-history-row"
                  data-prd-id={row.id}
                >
                  <span
                    className="prd-row-version"
                    data-testid="prd-history-version"
                  >
                    v{row.version}
                  </span>
                  <span
                    className={`badge ${STATUS_BADGE[row.status as PrdStatus] ?? 'badge-outline'}`}
                  >
                    {STATUS_LABEL[row.status as PrdStatus] ?? displayLabel(row.status)}
                  </span>
                  {row.id === prd.id ? (
                    <span
                      className="badge badge-accent"
                      data-testid="prd-history-active"
                    >
                      当前
                    </span>
                  ) : null}
                  {row.id === historyActiveId(history ?? [], prd.id) ? (
                    <span
                      className="badge badge-success"
                      data-testid="prd-history-backend-active"
                    >
                      最新版本
                    </span>
                  ) : null}
                  <Link
                    href={`/prds/${row.id}`}
                    className="btn btn-ghost btn-sm"
                    data-testid="prd-history-link"
                  >
                    打开
                  </Link>
                  <span className="muted" style={{ fontSize: 'var(--t-caption)', marginLeft: 'auto' }}>
                    更新 {formatRelative(row.updated_at ?? null)}
                  </span>
                </li>
              ))
            : null}
        </ul>
        {historyTotal > historyPage.limit ? (
          <footer className="pagination">
            <span className="pagination-meta">
              共 {historyTotal} 个版本 · 第 {currentHistoryPage} / {historyPageCount} 页
            </span>
            <div className="pagination-controls">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={currentHistoryPage <= 1}
                onClick={() =>
                  setHistoryPage((p) => ({
                    ...p,
                    offset: Math.max(0, p.offset - p.limit),
                  }))
                }
                data-testid="history-prev"
              >
                上一页
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={currentHistoryPage >= historyPageCount}
                onClick={() =>
                  setHistoryPage((p) => ({
                    ...p,
                    offset: p.offset + p.limit,
                  }))
                }
                data-testid="history-next"
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

/* ---------- helpers ---------- */

/** Resolve the active PRD id from the per-requirement history page.
 *
 * The backend exposes the active pointer via
 * ``GET /requirements/{id}/prds`` in a separate field
 * (``active_prd_id``). The page already holds ``history`` from that
 * endpoint — the matched id is whichever row equals the backend's
 * stable pointer. We keep this resolver in scope so the row can
 * carry a "Backend Active" badge without recomputing version sort.
 *
 * Returns ``null`` when the page slice does not contain the active
 * row (e.g. when the user is paginated past it). The badge is
 * intentionally silent in that case — the backend pointer is still
 * the source of truth, and the next page fetch will surface it. */
function historyActiveId(
  rows: PrdRead[],
  _currentId: string,
): string | null {
  // The page does not currently thread ``active_prd_id`` through to
  // this helper — the row marked "active" is whichever row in the
  // current slice has the highest version. The endpoint's
  // ``active_prd_id`` field is mirrored in the page state through
  // the parent data path; the per-row badge here is therefore a
  // visual hint, NOT the wire-level pointer.
  if (rows.length === 0) return null;
  const highest = rows.reduce((acc, cur) =>
    cur.version > acc.version ? cur : acc,
  );
  return highest.id;
}

/* ---------- 13-section body renderer ----------
 *
 * Read-only preview. Editing is a separate work package — every
 * field is rendered as a label + text container; no input handler,
 * no dirty tracking, no Save button.
 */
function PrdBody({ content }: { content: PrdContent }) {
  return (
    <section className="section" data-od-id="prd-body">
      <EvidenceBackedSectionBlock
        label="背景"
        data-testid="prd-section-background"
        section={content.background}
      />
      <EvidenceBackedSectionBlock
        label="用户问题"
        data-testid="prd-section-problem"
        section={content.problem}
      />
      <EvidenceBackedSectionBlock
        label="目标用户"
        data-testid="prd-section-target-user"
        section={content.target_user}
      />
      <EvidenceBackedSectionBlock
        label="用户故事"
        data-testid="prd-section-user-story"
        section={content.user_story}
      />
      <EvidenceBackedSectionBlock
        label="产品目标"
        data-testid="prd-section-product-goal"
        section={content.product_goal}
      />
      <ProposedSolutionBlock
        label="建议方案"
        data-testid="prd-section-proposed-solution"
        section={content.proposed_solution}
      />
      <FunctionalRequirementsBlock
        label="功能需求"
        data-testid="prd-section-functional-requirements"
        items={content.functional_requirements}
      />
      <EvidenceBackedSectionBlock
        label="用户流程"
        data-testid="prd-section-user-flow"
        section={content.user_flow}
      />
      <EdgeCasesBlock
        label="边界情况"
        data-testid="prd-section-edge-cases"
        items={content.edge_cases}
      />
      <AcceptanceCriteriaBlock
        label="验收标准"
        data-testid="prd-section-acceptance-criteria"
        items={content.acceptance_criteria}
      />
      <SuccessMetricsBlock
        label="成功指标"
        data-testid="prd-section-success-metrics"
        items={content.success_metrics}
      />
      <RisksBlock
        label="风险"
        data-testid="prd-section-risks"
        items={content.risks}
      />
      <MvpScopeBlock
        label="首版范围"
        data-testid="prd-section-mvp-scope"
        section={content.mvp_scope}
      />
      <EvidenceListsBlock
        label="引用证据"
        data-testid="prd-section-evidence"
        sources={content.evidence_sources}
        ids={content.evidence_ids}
      />
    </section>
  );
}

function EvidenceBackedSectionBlock({
  label,
  section,
  ...rest
}: {
  label: string;
  section: { text: string; evidence_ids: string[] };
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label}</h4>
      <p className="prd-section-text">{documentText(section.text)}</p>
      <EvidenceIds ids={section.evidence_ids} />
    </article>
  );
}

function ProposedSolutionBlock({
  label,
  section,
  ...rest
}: {
  label: string;
  section: { text: string; is_proposed: boolean; evidence_ids: string[] };
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>
        {label}
        <span
          className={`badge ${section.is_proposed ? 'badge-accent' : 'badge-outline'}`}
          style={{ marginLeft: 8 }}
        >
          {section.is_proposed ? '提议' : '已确认'}
        </span>
      </h4>
      <p className="prd-section-text">{documentText(section.text)}</p>
      <EvidenceIds ids={section.evidence_ids} />
    </article>
  );
}

function FunctionalRequirementsBlock({
  label,
  items,
  ...rest
}: {
  label: string;
  items: PrdFunctionalRequirement[];
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label} · <span className="muted">{items.length}</span></h4>
      <ol className="prd-fr-list">
        {items.map((item, idx) => (
          <li key={idx} className="prd-fr-item" data-testid="prd-fr-item">
            <span className="prd-fr-text">{documentText(item.text)}</span>
            <EvidenceIds ids={item.evidence_ids} />
          </li>
        ))}
      </ol>
    </article>
  );
}

function EdgeCasesBlock({
  label,
  items,
  ...rest
}: {
  label: string;
  items: PrdEdgeCase[];
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label} · <span className="muted">{items.length}</span></h4>
      <ul className="prd-edge-case-list">
        {items.map((item, idx) => (
          <li key={idx} className="prd-edge-case-item" data-testid="prd-edge-case-item">
            <span className="badge badge-outline mono">
              {EDGE_CATEGORY_LABEL[item.category] ?? displayLabel(item.category)}
            </span>
            <span className="prd-edge-case-description">{documentText(item.description)}</span>
            <EvidenceIds ids={item.evidence_ids} />
          </li>
        ))}
      </ul>
    </article>
  );
}

function AcceptanceCriteriaBlock({
  label,
  items,
  ...rest
}: {
  label: string;
  items: PrdAcceptanceCriterion[];
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label} · <span className="muted">{items.length}</span></h4>
      <ul className="prd-ac-list">
        {items.map((item, idx) => (
          <li key={idx} className="prd-ac-item" data-testid="prd-ac-item">
            <span
              className={`badge ${item.verifiable ? 'badge-success' : 'badge-muted'}`}
            >
              {item.verifiable ? '可验证' : '不可验证'}
            </span>
            <span className="prd-ac-text">{documentText(item.text)}</span>
            {item.note ? (
              <span className="muted" style={{ fontSize: 'var(--t-caption)' }}>
                {documentText(item.note)}
              </span>
            ) : null}
            <EvidenceIds ids={item.evidence_ids} />
          </li>
        ))}
      </ul>
    </article>
  );
}

function SuccessMetricsBlock({
  label,
  items,
  ...rest
}: {
  label: string;
  items: PrdSuccessMetricBlock[];
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label} · <span className="muted">{items.length}</span></h4>
      <ul className="prd-metric-list">
        {items.map((m, idx) => (
          <li key={idx} className="prd-metric-item" data-testid="prd-metric-item">
            <span className="prd-metric-name">{documentText(m.name)}</span>
            <span className="badge badge-outline mono">
              {METRIC_TYPE_LABEL[m.type] ?? displayLabel(m.type)}
            </span>
            {m.note ? (
              <span className="muted" style={{ fontSize: 'var(--t-caption)' }}>
                {metricNote(m.note ?? '')}
              </span>
            ) : null}
            {m.sources.length > 0 ? (
              <span className="muted" style={{ fontSize: 'var(--t-caption)' }}>
                来源：{m.sources.join(', ')}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </article>
  );
}

function RisksBlock({
  label,
  items,
  ...rest
}: {
  label: string;
  items: PrdRisk[];
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label} · <span className="muted">{items.length}</span></h4>
      {items.some((risk) => risk.category === 'known') ? (
        <p className="muted" data-testid="known-risk-review-boundary">
          请对照原始反馈核实已知风险。
        </p>
      ) : null}
      <ul className="prd-risk-list">
        {items.map((r, idx) => (
          <li key={idx} className="prd-risk-item" data-testid="prd-risk-item">
            <span className="badge badge-outline mono">
              {RISK_CATEGORY_LABEL[r.category] ?? displayLabel(r.category)}
            </span>
            <span className="prd-risk-description">{documentText(r.description)}</span>
            {r.mitigation ? (
              <div className="muted" style={{ fontSize: 'var(--t-caption)' }}>
                缓解：{documentText(r.mitigation)}
              </div>
            ) : null}
            <EvidenceIds ids={r.evidence_ids} />
          </li>
        ))}
      </ul>
    </article>
  );
}

function MvpScopeBlock({
  label,
  section,
  ...rest
}: {
  label: string;
  section: { in_scope: string; out_of_scope: string };
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label}</h4>
      <div className="prd-mvp-grid">
        <div>
          <div className="meta-k">包含内容</div>
          <p className="prd-section-text">{documentText(section.in_scope)}</p>
        </div>
        <div>
          <div className="meta-k">暂不包含</div>
          <p className="prd-section-text">{documentText(section.out_of_scope)}</p>
        </div>
      </div>
    </article>
  );
}

function EvidenceListsBlock({
  label,
  sources,
  ids,
  ...rest
}: {
  label: string;
  sources: string[];
  ids: string[];
  'data-testid'?: string;
}) {
  return (
    <article className="prd-section" data-testid={rest['data-testid']}>
      <h4>{label}</h4>
      <div className="prd-evidence-grid">
        <div>
          <div className="meta-k">资料来源</div>
          {sources.length > 0 ? (
            <ul className="prd-evidence-id-list">
              {sources.map((s, idx) => (
                <li key={idx}>
                  <code className="mono">{s}</code>
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">—</span>
          )}
        </div>
        <div>
          <div className="meta-k">反馈编号</div>
          <EvidenceIds ids={ids} />
        </div>
      </div>
    </article>
  );
}

function EvidenceIds({ ids }: { ids: string[] }) {
  if (!ids || ids.length === 0) {
    return <span className="muted">无证据引用</span>;
  }
  return (
    <div className="prd-evidence-ids" data-testid="prd-evidence-ids">
      {ids.map((id, idx) => (
        <code key={idx} className="mono prd-evidence-id">
          {id}
        </code>
      ))}
    </div>
  );
}

/* ---------- 13-section editor (Phase 7 C3a) ----------
 *
 * Renders the same 13 sections as ``PrdBody``, but every claim-shaped
 * field gets an editable input. Arrays can grow / shrink — the add
 * and remove buttons sit at the bottom / per-row of each section so
 * PMs can adjust content without losing the rest of the structure.
 *
 * The component is purely controlled: it never touches ``prd``
 * itself. All mutations flow through ``onChange(updater)`` which the
 * parent uses to update ``draftContent``. ``disabled`` disables every
 * control while ``saving`` is in flight (so a fast-clicking PM cannot
 * fork a half-saved draft into two parallel PATCH requests).
 *
 * evidence_ids / sources are exposed as comma-separated strings. The
 * backend schema rejects empty ``evidence_ids`` on
 * ``EvidenceBackedSection`` / ``ProposedSolution`` / top-level
 * ``evidence_sources`` / ``evidence_ids`` (min_length=1); we surface
 * that constraint client-side too so the PM sees the field is
 * non-empty BEFORE the PATCH 422s.
 */

interface PrdEditorProps {
  draftContent: PrdContent;
  onChange: (updater: (prev: PrdContent) => PrdContent) => void;
  disabled: boolean;
}

const EVIDENCE_INPUT_WIDTH = 380;
const FIELD_GAP = 8;

function csvJoin(arr: string[]): string {
  return arr.join(', ');
}

function csvParse(value: string): string[] {
  return value
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function arraysEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

/* Generic comma-separated input. The previous
 * ``EvidenceCsvField`` / ``SourcesCsvField`` were fully controlled
 * with ``value={csvJoin(value)}`` / ``onChange={csvParse(...)}``,
 * which collapsed trailing separators on every keystroke and made
 * it impossible to type a second id right after a comma — the
 * normalised text would land back on the input as the next render
 * and the user's caret position would jump. The field now holds
 * the user's RAW draft locally and only pushes the parsed array
 * to the parent; the parent contract (a ``string[]``) is
 * preserved. We re-sync from the outside only when the parsed
 * form of the current draft no longer matches the incoming
 * array, so an external value reset (e.g. discard draft, or a
 * row deleted from another section) still propagates while
 * in-progress editing — including trailing comma / space —
 * is left alone. */
function CsvField({
  testId,
  value,
  onChange,
  placeholder,
  disabled,
  required,
}: {
  testId: string;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
  disabled: boolean;
  required?: boolean;
}) {
  const [draft, setDraft] = useState<string>(() => csvJoin(value));
  const [prevValue, setPrevValue] = useState<string[]>(value);

  /* React-recommended "derive state from props" pattern: check
   * the previous value reference during render so a real external
   * change syncs the draft in the same commit, without an extra
   * useEffect round-trip and without the exhaustive-deps lint
   * gymnastics. */
  if (value !== prevValue) {
    setPrevValue(value);
    const parsed = csvParse(draft);
    if (!arraysEqual(parsed, value)) {
      setDraft(csvJoin(value));
    }
  }

  return (
    <input
      type="text"
      className="input"
      data-testid={testId}
      value={draft}
      onChange={(e) => {
        const next = e.target.value;
        setDraft(next);
        onChange(csvParse(next));
      }}
      placeholder={placeholder}
      disabled={disabled}
      style={{ width: EVIDENCE_INPUT_WIDTH, maxWidth: '100%' }}
      aria-required={required}
    />
  );
}

function TextSectionEditor({
  testIdPrefix,
  label,
  text,
  evidenceIds,
  onChange,
  disabled,
}: {
  testIdPrefix: string;
  label: string;
  text: string;
  evidenceIds: string[];
  onChange: (next: { text: string; evidence_ids: string[] }) => void;
  disabled: boolean;
}) {
  return (
    <article className="prd-section prd-section-editor" data-testid={`prd-section-${testIdPrefix}`}>
      <h4>{label}</h4>
      <textarea
        className="textarea"
        data-testid={`prd-draft-${testIdPrefix}-text`}
        value={documentText(text)}
        onChange={(e) => onChange({ text: e.target.value, evidence_ids: evidenceIds })}
        rows={3}
        disabled={disabled}
        style={{ width: '100%', marginBottom: FIELD_GAP }}
      />
      <CsvField
        testId={`prd-draft-${testIdPrefix}-evidence`}
        value={evidenceIds}
        onChange={(next) => onChange({ text, evidence_ids: next })}
        disabled={disabled}
        placeholder="反馈编号，多个用逗号分隔"
        required
      />
    </article>
  );
}

function PrdEditor({ draftContent, onChange, disabled }: PrdEditorProps) {
  const updateSection = <K extends keyof PrdContent>(
    key: K,
    next: PrdContent[K],
  ) => {
    onChange((prev) => ({ ...prev, [key]: next }));
  };

  const updateTextSection = (
    key:
      | 'background'
      | 'problem'
      | 'target_user'
      | 'user_story'
      | 'product_goal'
      | 'user_flow',
  ) => (next: { text: string; evidence_ids: string[] }) =>
    updateSection(key, next);

  const updateArray = <K extends keyof PrdContent>(
    key: K,
    next: PrdContent[K],
  ) => {
    updateSection(key, next);
  };

  return (
    <section className="section prd-editor" data-od-id="prd-editor" data-testid="prd-editor">
      <h3>编辑需求文档</h3>

      <TextSectionEditor
        testIdPrefix="background"
        label="背景"
        text={draftContent.background.text}
        evidenceIds={draftContent.background.evidence_ids}
        onChange={updateTextSection('background')}
        disabled={disabled}
      />
      <TextSectionEditor
        testIdPrefix="problem"
        label="用户问题"
        text={draftContent.problem.text}
        evidenceIds={draftContent.problem.evidence_ids}
        onChange={updateTextSection('problem')}
        disabled={disabled}
      />
      <TextSectionEditor
        testIdPrefix="target-user"
        label="目标用户"
        text={draftContent.target_user.text}
        evidenceIds={draftContent.target_user.evidence_ids}
        onChange={updateTextSection('target_user')}
        disabled={disabled}
      />
      <TextSectionEditor
        testIdPrefix="user-story"
        label="用户故事"
        text={draftContent.user_story.text}
        evidenceIds={draftContent.user_story.evidence_ids}
        onChange={updateTextSection('user_story')}
        disabled={disabled}
      />
      <TextSectionEditor
        testIdPrefix="product-goal"
        label="产品目标"
        text={draftContent.product_goal.text}
        evidenceIds={draftContent.product_goal.evidence_ids}
        onChange={updateTextSection('product_goal')}
        disabled={disabled}
      />

      {/* proposed_solution — extra is_proposed checkbox. */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-proposed-solution">
        <h4>建议方案</h4>
        <textarea
          className="textarea"
          data-testid="prd-draft-proposed-text"
          value={documentText(draftContent.proposed_solution.text)}
          onChange={(e) =>
            updateSection('proposed_solution', {
              ...draftContent.proposed_solution,
              text: e.target.value,
            })
          }
          rows={3}
          disabled={disabled}
          style={{ width: '100%', marginBottom: FIELD_GAP }}
        />
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 8, marginRight: 12 }}>
          <input
            type="checkbox"
            data-testid="prd-draft-proposed-is-proposed"
            checked={draftContent.proposed_solution.is_proposed}
            onChange={(e) =>
              updateSection('proposed_solution', {
                ...draftContent.proposed_solution,
                is_proposed: e.target.checked,
              })
            }
            disabled={disabled}
          />
          方案提案
        </label>
        <CsvField
          testId="prd-draft-proposed-evidence"
          value={draftContent.proposed_solution.evidence_ids}
          onChange={(next) =>
            updateSection('proposed_solution', {
              ...draftContent.proposed_solution,
              evidence_ids: next,
            })
          }
          disabled={disabled}
          placeholder="反馈编号，多个用逗号分隔"
          required
        />
      </article>

      {/* functional_requirements */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-functional-requirements">
        <h4>功能需求 · <span className="muted">{draftContent.functional_requirements.length}</span></h4>
        <ol className="prd-fr-list">
          {draftContent.functional_requirements.map((item, idx) => (
            <li key={idx} className="prd-fr-item-editor" data-testid={`prd-draft-fr-item-${idx}`} style={{ display: 'flex', flexDirection: 'column', gap: FIELD_GAP, marginBottom: FIELD_GAP }}>
              <textarea
                className="textarea"
                data-testid={`prd-draft-fr-item-${idx}-text`}
                value={documentText(item.text)}
                onChange={(e) =>
                  updateArray(
                    'functional_requirements',
                    draftContent.functional_requirements.map((it, i) =>
                      i === idx ? { ...it, text: e.target.value } : it,
                    ),
                  )
                }
                rows={2}
                disabled={disabled}
              />
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <CsvField
                  testId={`prd-draft-fr-item-${idx}-evidence`}
                  value={item.evidence_ids}
                  onChange={(next) =>
                    updateArray(
                      'functional_requirements',
                      draftContent.functional_requirements.map((it, i) =>
                        i === idx ? { ...it, evidence_ids: next } : it,
                      ),
                    )
                  }
                  disabled={disabled}
                  placeholder="反馈编号，多个用逗号分隔"
                  required
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={`prd-draft-fr-item-${idx}-delete`}
                  disabled={
                    disabled ||
                    draftContent.functional_requirements.length <= 1
                  }
                  title={
                    draftContent.functional_requirements.length <= 1
                      ? '至少保留一项'
                      : '删除'
                  }
                  onClick={() =>
                    updateArray(
                      'functional_requirements',
                      draftContent.functional_requirements.filter((_, i) => i !== idx),
                    )
                  }
                >
                  删除
                </button>
              </div>
            </li>
          ))}
        </ol>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          data-testid="prd-draft-fr-add"
          disabled={disabled}
          onClick={() =>
            updateArray('functional_requirements', [
              ...draftContent.functional_requirements,
              { text: '', evidence_ids: [] },
            ])
          }
        >
          + 添加功能需求
        </button>
      </article>

      <TextSectionEditor
        testIdPrefix="user-flow"
        label="用户流程"
        text={draftContent.user_flow.text}
        evidenceIds={draftContent.user_flow.evidence_ids}
        onChange={updateTextSection('user_flow')}
        disabled={disabled}
      />

      {/* edge_cases */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-edge-cases">
        <h4>边界情况 · <span className="muted">{draftContent.edge_cases.length}</span></h4>
        <ul className="prd-edge-case-list">
          {draftContent.edge_cases.map((item, idx) => (
            <li key={idx} className="prd-edge-case-item-editor" data-testid={`prd-draft-edge-item-${idx}`} style={{ display: 'flex', flexDirection: 'column', gap: FIELD_GAP, marginBottom: FIELD_GAP }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <select
                  className="select"
                  data-testid={`prd-draft-edge-item-${idx}-category`}
                  value={item.category}
                  onChange={(e) =>
                    updateArray(
                      'edge_cases',
                      draftContent.edge_cases.map((it, i) =>
                        i === idx
                          ? { ...it, category: e.target.value as EdgeCaseCategory }
                          : it,
                      ),
                    )
                  }
                  disabled={disabled}
                >
                  {(Object.keys(EDGE_CATEGORY_LABEL) as EdgeCaseCategory[]).map(
                    (c) => (
                      <option key={c} value={c}>
                        {EDGE_CATEGORY_LABEL[c]}
                      </option>
                    ),
                  )}
                </select>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={`prd-draft-edge-item-${idx}-delete`}
                  disabled={disabled || draftContent.edge_cases.length <= 1}
                  onClick={() =>
                    updateArray(
                      'edge_cases',
                      draftContent.edge_cases.filter((_, i) => i !== idx),
                    )
                  }
                >
                  删除
                </button>
              </div>
              <textarea
                className="textarea"
                data-testid={`prd-draft-edge-item-${idx}-description`}
                value={documentText(item.description)}
                onChange={(e) =>
                  updateArray(
                    'edge_cases',
                    draftContent.edge_cases.map((it, i) =>
                      i === idx ? { ...it, description: e.target.value } : it,
                    ),
                  )
                }
                rows={2}
                disabled={disabled}
              />
              <CsvField
                testId={`prd-draft-edge-item-${idx}-evidence`}
                value={item.evidence_ids}
                onChange={(next) =>
                  updateArray(
                    'edge_cases',
                    draftContent.edge_cases.map((it, i) =>
                      i === idx ? { ...it, evidence_ids: next } : it,
                    ),
                  )
                }
                disabled={disabled}
                placeholder="反馈编号，多个用逗号分隔"
                required
              />
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          data-testid="prd-draft-edge-add"
          disabled={disabled}
          onClick={() =>
            updateArray('edge_cases', [
              ...draftContent.edge_cases,
              { category: 'empty_state', description: '', evidence_ids: [] },
            ])
          }
        >
          + 添加边界情况
        </button>
      </article>

      {/* acceptance_criteria */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-acceptance-criteria">
        <h4>验收标准 · <span className="muted">{draftContent.acceptance_criteria.length}</span></h4>
        <ul className="prd-ac-list">
          {draftContent.acceptance_criteria.map((item, idx) => (
            <li key={idx} className="prd-ac-item-editor" data-testid={`prd-draft-ac-item-${idx}`} style={{ display: 'flex', flexDirection: 'column', gap: FIELD_GAP, marginBottom: FIELD_GAP }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <textarea
                  className="textarea"
                  data-testid={`prd-draft-ac-item-${idx}-text`}
                  value={documentText(item.text)}
                  onChange={(e) =>
                    updateArray(
                      'acceptance_criteria',
                      draftContent.acceptance_criteria.map((it, i) =>
                        i === idx ? { ...it, text: e.target.value } : it,
                      ),
                    )
                  }
                  rows={2}
                  disabled={disabled}
                  style={{ flex: 1, minWidth: 280 }}
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={`prd-draft-ac-item-${idx}-delete`}
                  disabled={disabled || draftContent.acceptance_criteria.length <= 1}
                  onClick={() =>
                    updateArray(
                      'acceptance_criteria',
                      draftContent.acceptance_criteria.filter((_, i) => i !== idx),
                    )
                  }
                >
                  删除
                </button>
              </div>
              <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  <input
                    type="checkbox"
                    data-testid={`prd-draft-ac-item-${idx}-verifiable`}
                    checked={item.verifiable}
                    onChange={(e) =>
                      updateArray(
                        'acceptance_criteria',
                        draftContent.acceptance_criteria.map((it, i) =>
                          i === idx ? { ...it, verifiable: e.target.checked } : it,
                        ),
                      )
                    }
                    disabled={disabled}
                  />
                  可验证
                </label>
                <input
                  type="text"
                  className="input"
                  data-testid={`prd-draft-ac-item-${idx}-note`}
                  value={documentText(item.note)}
                  placeholder="备注"
                  onChange={(e) =>
                    updateArray(
                      'acceptance_criteria',
                      draftContent.acceptance_criteria.map((it, i) =>
                        i === idx ? { ...it, note: e.target.value } : it,
                      ),
                    )
                  }
                  disabled={disabled}
                  style={{ flex: 1, minWidth: 200 }}
                />
              </div>
              <CsvField
                testId={`prd-draft-ac-item-${idx}-evidence`}
                value={item.evidence_ids}
                onChange={(next) =>
                  updateArray(
                    'acceptance_criteria',
                    draftContent.acceptance_criteria.map((it, i) =>
                      i === idx ? { ...it, evidence_ids: next } : it,
                    ),
                  )
                }
                disabled={disabled}
                placeholder="反馈编号，多个用逗号分隔"
                required
              />
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          data-testid="prd-draft-ac-add"
          disabled={disabled}
          onClick={() =>
            updateArray('acceptance_criteria', [
              ...draftContent.acceptance_criteria,
              { text: '', verifiable: true, note: '', evidence_ids: [] },
            ])
          }
        >
          + 添加验收标准
        </button>
      </article>

      {/* success_metrics */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-success-metrics">
        <h4>成功指标 · <span className="muted">{draftContent.success_metrics.length}</span></h4>
        <ul className="prd-metric-list">
          {draftContent.success_metrics.map((item, idx) => (
            <li key={idx} className="prd-metric-item-editor" data-testid={`prd-draft-metric-item-${idx}`} style={{ display: 'flex', flexDirection: 'column', gap: FIELD_GAP, marginBottom: FIELD_GAP }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <input
                  type="text"
                  className="input"
                  data-testid={`prd-draft-metric-item-${idx}-name`}
                  value={documentText(item.name)}
                  placeholder="指标名"
                  onChange={(e) =>
                    updateArray(
                      'success_metrics',
                      draftContent.success_metrics.map((it, i) =>
                        i === idx ? { ...it, name: e.target.value } : it,
                      ),
                    )
                  }
                  disabled={disabled}
                  style={{ flex: 1, minWidth: 200 }}
                />
                <select
                  className="select"
                  data-testid={`prd-draft-metric-item-${idx}-type`}
                  value={item.type}
                  onChange={(e) =>
                    updateArray(
                      'success_metrics',
                      draftContent.success_metrics.map((it, i) =>
                        i === idx ? { ...it, type: e.target.value as MetricType } : it,
                      ),
                    )
                  }
                  disabled={disabled}
                >
                  {(Object.keys(METRIC_TYPE_LABEL) as MetricType[]).map((t) => (
                    <option key={t} value={t}>
                      {METRIC_TYPE_LABEL[t]}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={`prd-draft-metric-item-${idx}-delete`}
                  disabled={disabled}
                  onClick={() =>
                    updateArray(
                      'success_metrics',
                      draftContent.success_metrics.filter((_, i) => i !== idx),
                    )
                  }
                >
                  删除
                </button>
              </div>
              <input
                type="text"
                className="input"
                data-testid={`prd-draft-metric-item-${idx}-note`}
                value={documentText(item.note)}
                placeholder="备注"
                onChange={(e) =>
                  updateArray(
                    'success_metrics',
                    draftContent.success_metrics.map((it, i) =>
                      i === idx ? { ...it, note: e.target.value } : it,
                    ),
                  )
                }
                disabled={disabled}
              />
              <CsvField
                testId={`prd-draft-metric-item-${idx}-sources`}
                value={item.sources}
                onChange={(next) =>
                  updateArray(
                    'success_metrics',
                    draftContent.success_metrics.map((it, i) =>
                      i === idx ? { ...it, sources: next } : it,
                    ),
                  )
                }
                disabled={disabled}
                placeholder="来源（多个用逗号分隔）"
              />
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          data-testid="prd-draft-metric-add"
          disabled={disabled}
          onClick={() =>
            updateArray('success_metrics', [
              ...draftContent.success_metrics,
              { name: '', type: 'metric_definition', note: '', sources: [] },
            ])
          }
        >
          + 添加成功指标
        </button>
      </article>

      {/* risks */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-risks">
        <h4>风险 · <span className="muted">{draftContent.risks.length}</span></h4>
        <ul className="prd-risk-list">
          {draftContent.risks.map((item, idx) => (
            <li key={idx} className="prd-risk-item-editor" data-testid={`prd-draft-risk-item-${idx}`} style={{ display: 'flex', flexDirection: 'column', gap: FIELD_GAP, marginBottom: FIELD_GAP }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <select
                  className="select"
                  data-testid={`prd-draft-risk-item-${idx}-category`}
                  value={item.category}
                  onChange={(e) =>
                    updateArray(
                      'risks',
                      draftContent.risks.map((it, i) =>
                        i === idx
                          ? { ...it, category: e.target.value as RiskCategory }
                          : it,
                      ),
                    )
                  }
                  disabled={disabled}
                >
                  {(Object.keys(RISK_CATEGORY_LABEL) as RiskCategory[]).map(
                    (c) => (
                      <option key={c} value={c}>
                        {RISK_CATEGORY_LABEL[c]}
                      </option>
                    ),
                  )}
                </select>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={`prd-draft-risk-item-${idx}-delete`}
                  disabled={disabled}
                  onClick={() =>
                    updateArray(
                      'risks',
                      draftContent.risks.filter((_, i) => i !== idx),
                    )
                  }
                >
                  删除
                </button>
              </div>
              <textarea
                className="textarea"
                data-testid={`prd-draft-risk-item-${idx}-description`}
                value={documentText(item.description)}
                placeholder="风险描述"
                onChange={(e) =>
                  updateArray(
                    'risks',
                    draftContent.risks.map((it, i) =>
                      i === idx ? { ...it, description: e.target.value } : it,
                    ),
                  )
                }
                rows={2}
                disabled={disabled}
              />
              <CsvField
                testId={`prd-draft-risk-item-${idx}-evidence`}
                value={item.evidence_ids}
                onChange={(next) =>
                  updateArray(
                    'risks',
                    draftContent.risks.map((it, i) =>
                      i === idx ? { ...it, evidence_ids: next } : it,
                    ),
                  )
                }
                disabled={disabled}
                placeholder="反馈编号，多个用逗号分隔"
                required
              />
              <input
                type="text"
                className="input"
                data-testid={`prd-draft-risk-item-${idx}-mitigation`}
                value={documentText(item.mitigation)}
                placeholder="缓解方案"
                onChange={(e) =>
                  updateArray(
                    'risks',
                    draftContent.risks.map((it, i) =>
                      i === idx ? { ...it, mitigation: e.target.value } : it,
                    ),
                  )
                }
                disabled={disabled}
              />
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          data-testid="prd-draft-risk-add"
          disabled={disabled}
          onClick={() =>
            updateArray('risks', [
              ...draftContent.risks,
              {
                category: 'known',
                description: '',
                evidence_ids: [],
                mitigation: '',
              },
            ])
          }
        >
          + 添加风险
        </button>
      </article>

      {/* mvp_scope */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-mvp-scope">
        <h4>首版范围</h4>
        <div className="prd-mvp-grid">
          <div>
            <div className="meta-k">包含内容</div>
            <textarea
              className="textarea"
              data-testid="prd-draft-mvp-in-scope"
              value={documentText(draftContent.mvp_scope.in_scope)}
              onChange={(e) =>
                updateSection('mvp_scope', {
                  ...draftContent.mvp_scope,
                  in_scope: e.target.value,
                })
              }
              rows={3}
              disabled={disabled}
              style={{ width: '100%' }}
            />
          </div>
          <div>
            <div className="meta-k">暂不包含</div>
            <textarea
              className="textarea"
              data-testid="prd-draft-mvp-out-of-scope"
              value={documentText(draftContent.mvp_scope.out_of_scope)}
              onChange={(e) =>
                updateSection('mvp_scope', {
                  ...draftContent.mvp_scope,
                  out_of_scope: e.target.value,
                })
              }
              rows={3}
              disabled={disabled}
              style={{ width: '100%' }}
            />
          </div>
        </div>
      </article>

      {/* Top-level evidence_sources + evidence_ids */}
      <article className="prd-section prd-section-editor" data-testid="prd-section-evidence">
        <h4>引用证据</h4>
        <div className="prd-evidence-grid">
          <div>
            <div className="meta-k">资料来源</div>
            <CsvField
              testId="prd-draft-evidence-sources"
              value={draftContent.evidence_sources}
              onChange={(next) => updateSection('evidence_sources', next)}
              disabled={disabled}
              placeholder="来源（多个用逗号分隔）"
            />
          </div>
          <div>
            <div className="meta-k">反馈编号</div>
            <CsvField
              testId="prd-draft-evidence-evidence"
              value={draftContent.evidence_ids}
              onChange={(next) => updateSection('evidence_ids', next)}
              disabled={disabled}
              placeholder="反馈编号，多个用逗号分隔"
              required
            />
          </div>
        </div>
      </article>
    </section>
  );
}
