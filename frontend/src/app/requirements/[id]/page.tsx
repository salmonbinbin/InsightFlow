'use client';

/**
 * InsightFlow — Requirement detail (Phase 7 work package B2).
 *
 * Reads GET /api/requirements/{id} and lets a PM edit the AI-authored
 * content via PATCH /api/requirements/{id} with body
 *   { status?: RequirementStatus; content?: PMRequirementContent }
 *
 * Contract source of truth:
 *   - backend/app/schemas/requirement_prd.py
 *     • RequirementRead
 *     • PMRequirementContent
 *     • RequirementPatchRequest
 *     • RequirementStatusLiteral
 *
 * B2 round-trip rules:
 *   - buildContent preserves ALL items in success_metrics / risks /
 *     evidence_references (the previous B1 placeholder only kept index
 *     0, silently dropping every other entry on save).
 *   - The UI lets a PM add or remove one item at a time. Save sends the
 *     full current array, so unpicked items survive untouched.
 *   - Schema enforcement:
 *       • name / problem / target_user / user_story /
 *         solution_hypothesis     : min_length=1 each
 *       • success_metrics[].name   : min_length=1 each
 *       • success_metrics[].note   : optional (default ""), max_length=500
 *       • success_metrics          : MAY be empty
 *       • risks                    : MAY be empty
 *       • risks[].description      : min_length=1 each
 *       • evidence_references      : MUST contain ≥1 entry
 *
 *   - Unknown-enum gate (B2 fix):
 *       • If a read-side ``success_metrics[].type`` or
 *         ``risks[].category`` carries a value the frontend does not
 *         know (the backend may add new enum members at any time),
 *         the page renders a blocking banner + per-row warnings and
 *         disables Save. We never silently rewrite an unknown value
 *         to a frontend default and ship it in a PATCH.
 *
 * State lifecycle (B2 requirement):
 *   Loading → Empty (404) | Error → Retry
 *   Loaded → Idle → Dirty (form edits) → Saving → Saved | Failed (retry)
 * Project switch zeroes every piece of state; cross-project
 * project_id mismatch refuses to render.
 *
 * PM Priority is read-only on this page — priority is set during the
 * upstream PM Decision and never mutated here.
 */

import Link from 'next/link';
import { displayLabel, metricNote, documentText } from '../../../lib/presentation';
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
  MetricType,
  Requirement,
  RequirementContent,
  RequirementPatchRequest,
  RequirementStatus,
  RiskCategory,
  ToolRunPage,
  ToolRunRead,
  ToolRunSnapshot,
} from '../../../lib/api/types';

const STATUS_OPTIONS: ReadonlyArray<{ value: RequirementStatus; label: string }> = [
  { value: 'draft', label: '草稿' },
  { value: 'reviewing', label: '复核中' },
  { value: 'approved', label: '已批准' },
  { value: 'rejected', label: '已驳回' },
  { value: 'archived', label: '已归档' },
];

const RISK_CATEGORY_OPTIONS: ReadonlyArray<{ value: RiskCategory; label: string }> = [
  { value: 'known', label: '已知风险' },
  { value: 'speculative', label: '推测风险' },
];

const METRIC_TYPE_OPTIONS: ReadonlyArray<{ value: MetricType; label: string }> = [
  { value: 'metric_definition', label: '指标定义' },
  { value: 'baseline_only', label: '仅基线' },
];

/* ---------- enum runtime guards ----------
 *
 * The backend READ DTOs (SuccessMetricRead.type / RiskRead.category)
 * are typed ``str`` on the wire — they may carry any string, including
 * enum members added after the frontend's last build. The WRITE DTOs
 * (``MetricTypeLiteral`` / ``RiskCategoryLiteral``) are strict Pydantic
 * Literal unions; an illegal value would 422 the PATCH.
 *
 * Rule: NEVER silently rewrite an unknown read-side value to a default
 * literal and ship it in a PATCH. That would corrupt the AI-authored
 * snapshot (e.g. a future ``type: 'event_rate'`` would be flattened to
 * ``'metric_definition'`` on the first save). Instead, the helpers
 * below distinguish "narrow safely" from "reject loudly":
 *   - isMetricType / isRiskCategory: type predicates for SELECT
 *     onChange handlers, which can only ever receive option-list values.
 *   - detectUnknownEnums: scans a read DTO and reports the original
 *     raw value at each index. The page renders a blocking warning and
 *     refuses to save while any unknown value is present — the PM must
 *     contact an admin before the row can be edited through the wire.
 */
function isMetricType(v: string): v is MetricType {
  return v === 'metric_definition' || v === 'baseline_only';
}
function isRiskCategory(v: string): v is RiskCategory {
  return v === 'known' || v === 'speculative';
}
function normaliseMetricType(raw: string): MetricType {
  return isMetricType(raw) ? raw : 'metric_definition';
}
function normaliseRiskCategory(raw: string): RiskCategory {
  return isRiskCategory(raw) ? raw : 'speculative';
}

/** Per-index report of read-side enum values the frontend cannot
 *  round-trip. Stored as the original raw string so the UI can show
 *  the PM what the backend actually returned. */
export interface UnknownEnumReport {
  metrics: Array<{ index: number; raw: string }>;
  risks: Array<{ index: number; raw: string }>;
}
function emptyUnknownEnumReport(): UnknownEnumReport {
  return { metrics: [], risks: [] };
}
function detectUnknownEnums(req: Requirement): UnknownEnumReport {
  const metrics: Array<{ index: number; raw: string }> = [];
  req.success_metrics.forEach((m, index) => {
    if (!isMetricType(m.type)) metrics.push({ index, raw: m.type });
  });
  const risks: Array<{ index: number; raw: string }> = [];
  req.risks.forEach((r, index) => {
    if (!isRiskCategory(r.category)) risks.push({ index, raw: r.category });
  });
  return { metrics, risks };
}

/* ---------- buildContent ----------
 *
 * Round-trip-safe copy: every entry from each array travels into the
 * draft. Backend allows success_metrics and risks to be empty arrays;
 * evidence_references MUST contain at least one (we fall back to a
 * blank row when the backend ever hands us an empty list so the user
 * can fill it in without hitting the 422).
 *
 * The read→write enum normalisation for ``success_metrics[].type`` and
 * ``risks[].category`` runs through the guards above — we never copy
 * a raw ``string`` straight into a strict-union field.
 */
function buildContent(req: Requirement): RequirementContent {
  return {
    name: req.name,
    problem: req.problem,
    target_user: req.target_user,
    user_story: req.user_story,
    solution_hypothesis: req.solution_hypothesis,
    success_metrics: req.success_metrics.map((m) => ({
      name: m.name,
      type: normaliseMetricType(m.type),
      sources: m.sources ?? [],
      note: m.note ?? '',
    })),
    risks: req.risks.map((r) => ({
      category: normaliseRiskCategory(r.category),
      description: r.description,
      evidence_ids: r.evidence_ids ?? [],
      mitigation: r.mitigation ?? '',
    })),
    evidence_references:
      req.evidence_references.length > 0
        ? req.evidence_references.map((e) => ({
            external_id: e.external_id,
            usage: e.usage,
          }))
        : [{ external_id: '', usage: '' }],
  };
}

/* ---------- defaults ----------
 *
 * Initial values for add-button rows.
 *
 * Backend SuccessMetric / Risk / EvidenceReference all have at least
 * one ``min_length=1`` field, so ``blankRisk`` / ``blankEvidence``
 * pre-fill a non-empty placeholder. ``blankMetric`` does NOT pre-fill
 * ``note``: the backend treats ``note`` as optional (default ``""``,
 * max_length=500) and a fresh row's note is genuinely unknown until
 * the PM writes one. We refuse to default the field to ``"TBD"``
 * because that placeholder would silently land in a real PATCH and
 * pollute the AI-authored snapshot.
 */
function blankMetric(): RequirementContent['success_metrics'][number] {
  return { name: '', type: 'metric_definition', sources: [], note: '' };
}
function blankRisk(): RequirementContent['risks'][number] {
  return { category: 'speculative', description: 'TBD', evidence_ids: [], mitigation: '' };
}
function blankEvidence(): RequirementContent['evidence_references'][number] {
  return { external_id: '', usage: '' };
}

/* ---------- field-level validators ----------
 *
 * Each helper mirrors the backend PMRequirementContent schema. The
 * Save button stays disabled until EVERY required field passes — we
 * never upload a body that the backend would reject with 422.
 *
 * ``note`` is intentionally NOT in the required set: the backend's
 * SuccessMetric model declares ``note: str = Field(default="")``
 * (i.e. optional, default empty, max_length=500), so a PM may submit
 * an empty note. Same logic for ``risks[].mitigation`` and
 * ``success_metrics[].sources`` — both default to ``[]`` / ``""``.
 */
function validateContent(c: RequirementContent): string | null {
  if (!c.name.trim()) return 'Name 不能为空。';
  if (!c.problem.trim()) return 'Problem 不能为空。';
  if (!c.target_user.trim()) return 'Target User 不能为空。';
  if (!c.user_story.trim()) return 'User Story 不能为空。';
  if (!c.solution_hypothesis.trim()) return 'Solution Hypothesis 不能为空。';
  for (const [idx, m] of c.success_metrics.entries()) {
    if (!m.name.trim()) return `Success Metrics 第 ${idx + 1} 项缺少 name。`;
  }
  for (const [idx, r] of c.risks.entries()) {
    if (!r.description.trim()) return `Risks 第 ${idx + 1} 项缺少 description。`;
  }
  if (c.evidence_references.length < 1) {
    return '至少需要 1 条 Evidence Reference。';
  }
  for (const [idx, e] of c.evidence_references.entries()) {
    if (!e.external_id.trim()) return `Evidence References 第 ${idx + 1} 项缺少 external_id。`;
    if (!e.usage.trim()) return `Evidence References 第 ${idx + 1} 项缺少 usage。`;
  }
  return null;
}

export default function RequirementDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();
  const requirementId = params?.id ?? '';
  const requestScopeRef = useRef({ projectId: activeProjectId, id: requirementId });
  if (requestScopeRef.current.projectId !== activeProjectId || requestScopeRef.current.id !== requirementId) {
    requestScopeRef.current = { projectId: activeProjectId, id: requirementId };
  }

  const [requirement, setRequirement] = useState<Requirement | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);

  const [draft, setDraft] = useState<RequirementContent | null>(null);
  const [status, setStatus] = useState<RequirementStatus>('draft');
  const [originalStatus, setOriginalStatus] = useState<RequirementStatus>('draft');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedTick, setSavedTick] = useState<number | null>(null);
  const [validationMessage, setValidationMessage] = useState<string | null>(null);

  /* Phase 7 C2: Generate PRD action. Calls
   * POST /api/requirements/{id}/prds with body ``{}`` (AI path);
   * on success the page navigates to the new PRD's detail route.
   * The action is project-scoped — the requirement already passed
   * the ``project_id === activeProjectId`` gate during load, so
   * ``generatingPrd`` can only run while the requirement is
   * actively rendered (i.e. is non-null AND matches the active
   * project). */
  const [generatingPrd, setGeneratingPrd] = useState(false);
  const [generatePrdError, setGeneratePrdError] = useState<string | null>(null);
  const [generatePrdSuccess, setGeneratePrdSuccess] = useState<{ id: string; version: number } | null>(null);

  /* Phase 8 C: Tool Action Preview (Simulator).
   *
   * The section is rendered ONLY when the current Requirement is
   * in ``status='approved'`` AND belongs to the active project
   * (the load gate above already enforces the project check).
   *
   * The three in-flight flags are mutually exclusive — a single
   * tool action runs at a time, and the buttons stay disabled
   * while the corresponding flag is set so a rapid second click
   * cannot double-POST (the backend would dedupe on
   * ``request_id``, but the UI must not depend on that). */
  const [toolHistory, setToolHistory] = useState<ToolRunPage | null>(null);
  const [toolHistoryError, setToolHistoryError] = useState<string | null>(null);
  const [toolHistoryLoading, setToolHistoryLoading] = useState(false);
  const [preparingToolRun, setPreparingToolRun] = useState(false);
  const [cancellingToolRun, setCancellingToolRun] = useState(false);
  const [approvingToolRun, setApprovingToolRun] = useState(false);
  const [toolActionError, setToolActionError] = useState<string | null>(null);
  /* ``failedToolAction`` remembers WHICH action the current
   * ``toolActionError`` came from so the ErrorState's Retry
   * button can re-invoke the SAME handler (Cancel error →
   * retry Cancel, not Prepare). Cleared together with the
   * error so a fresh error path starts from an empty slate. */
  const [failedToolAction, setFailedToolAction] = useState<'prepare' | 'cancel' | 'approve' | null>(null);
  /* The last successfully prepared ``request_id`` is kept around
   * so the Cancel / Approve buttons can target it without
   * re-deriving it from the history. The list endpoint's
   * ``active_run_id`` pointer is the source of truth though —
   * the page falls back to it whenever the locally-issued UUID
   * does not match a returned row. */
  const [lastPreparedRequestId, setLastPreparedRequestId] = useState<string | null>(null);
  /* ``inFlightRef`` is a synchronous guard against the
   * double-POST that would otherwise be possible when two
   * clicks fire inside the same React batch — the React-state
   * ``preparingToolRun`` flag is not yet ``true`` when the
   * second click's closure runs. A ref updates synchronously
   * and so is the single source of truth for "another action
   * is already in flight". The state flag is still used to
   * drive the disabled UI. */
  const inFlightRef = useRef(false);
  /* Indices + raw strings of read-side enum values that the frontend
   * does not know how to round-trip. While this is non-empty the page
   * blocks the Save button — see ``unknownBlockReason`` below. The
   * raw strings are kept so the UI can show "the backend sent this,
   * please contact an admin" rather than a flattened default. */
  const [unknownEnums, setUnknownEnums] = useState<UnknownEnumReport>(
    emptyUnknownEnumReport(),
  );

  /* ---------- Reset on project switch ---------- */

  useEffect(() => {
    setRequirement(null);
    setLoadError(null);
    setLoading(false);
    setNotFound(false);
    setDraft(null);
    setStatus('draft');
    setOriginalStatus('draft');
    setSaving(false);
    setSaveError(null);
    setSavedTick(null);
    setValidationMessage(null);
    setUnknownEnums(emptyUnknownEnumReport());
    setGeneratingPrd(false);
    setGeneratePrdError(null);
    setGeneratePrdSuccess(null);
    setToolHistory(null);
    setToolHistoryError(null);
    setToolHistoryLoading(false);
    setPreparingToolRun(false);
    setCancellingToolRun(false);
    setApprovingToolRun(false);
    setToolActionError(null);
    setFailedToolAction(null);
    setLastPreparedRequestId(null);
    inFlightRef.current = false;
  }, [activeProjectId]);

  /* ---------- Reset on URL id change ----------
   *
   * useParams is route-driven: navigating between two Requirement
   * detail pages re-renders with a new ``requirementId`` but does NOT
   * change ``activeProjectId``. Without this effect, switching from
   * ``/requirements/{A}`` to ``/requirements/{B}`` would keep the old
   * requirement's body in the draft — a stale-form bug. We treat any
   * change to the URL id as a hard restart.
   */
  useEffect(() => {
    setRequirement(null);
    setLoadError(null);
    setLoading(false);
    setNotFound(false);
    setDraft(null);
    setStatus('draft');
    setOriginalStatus('draft');
    setSaving(false);
    setSaveError(null);
    setSavedTick(null);
    setValidationMessage(null);
    setUnknownEnums(emptyUnknownEnumReport());
    setGeneratingPrd(false);
    setGeneratePrdError(null);
    setGeneratePrdSuccess(null);
    setToolHistory(null);
    setToolHistoryError(null);
    setToolHistoryLoading(false);
    setPreparingToolRun(false);
    setCancellingToolRun(false);
    setApprovingToolRun(false);
    setToolActionError(null);
    setFailedToolAction(null);
    setLastPreparedRequestId(null);
    inFlightRef.current = false;
  }, [requirementId]);

  /* ---------- Load ---------- */

  const load = useCallback(async () => {
    if (!requirementId || !signal) return;
    // Capture both signal AND requirementId at call time so a later
    // navigation to /requirements/{other} can be distinguished from a
    // still-good response. ``signal`` alone doesn't change on URL-id
    // changes — useProjectScopedFetch keys off activeProjectId, not the
    // route — so without ``myId`` we would risk an out-of-date response
    // overwriting freshly-loaded state for the new id.
    const mySignal = signal;
    const myId = requirementId;
    setLoading(true);
    setLoadError(null);
    setNotFound(false);
    const requestScope = requestScopeRef.current;
    const res = await client.getRequirement(requirementId, mySignal, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal.aborted) return;
    if (myId !== requirementId) return; // route moved on while we awaited
    if (res.error) {
      if (res.error.status === 404) {
        setRequirement(null);
        setNotFound(true);
        setLoading(false);
        return;
      }
      setLoadError(classifyError(res.error));
      setRequirement(null);
      setLoading(false);
      return;
    }
    if (!res.data) {
      setLoadError('未找到 Requirement。');
      setRequirement(null);
      setLoading(false);
      return;
    }
    // Project isolation: refuse to surface requirements from other projects.
    if (res.data.project_id !== activeProjectId) {
      setRequirement(null);
      setLoadError(
        `Requirement 属于其它项目（${res.data.project_id ?? '未知'}），无法展示。`,
      );
      setLoading(false);
      return;
    }
    setRequirement(res.data);
    setDraft(buildContent(res.data));
    setStatus(res.data.status as RequirementStatus);
    setOriginalStatus(res.data.status as RequirementStatus);
    // Run the unknown-enum scan AFTER the requirement lands, so the
    // banner + per-row warnings render with the freshest data. This is
    // also what powers ``canSave``'s hard block — we never ship a
    // PATCH that would silently flatten a backend-introduced enum
    // value down to a frontend default.
    setUnknownEnums(detectUnknownEnums(res.data));
    setLoading(false);
  }, [activeProjectId, client, requirementId, signal]);

  useEffect(() => {
    void load();
  }, [load]);

  /* ---------- Dirty / Saved ---------- */

  const dirty = useMemo(() => {
    if (!draft || !requirement) return false;
    if (status !== originalStatus) return true;
    const a = buildContent(requirement);
    return !shallowEqualContent(a, draft);
  }, [draft, requirement, status, originalStatus]);

  const validationResult = useMemo(
    () => (draft ? validateContent(draft) : '尚未加载表单。'),
    [draft],
  );

  /* Unknown-enum gate: blocks Save entirely while any read-side
   * ``success_metrics[].type`` or ``risks[].category`` carried a value
   * the frontend cannot round-trip. The message is shown both inline
   * (in the Save button's tooltip and as a top-of-form banner) and is
   * the single source of truth for the save-gate — we never let the
   * PM flatten an unknown value to a frontend default by accident. */
  const unknownBlockReason = useMemo<string | null>(() => {
    if (unknownEnums.metrics.length === 0 && unknownEnums.risks.length === 0) {
      return null;
    }
    const parts: string[] = [];
    if (unknownEnums.metrics.length > 0) {
      const items = unknownEnums.metrics
        .map((u) => `第${u.index + 1}项指标`)
        .join('；');
      parts.push(items);
    }
    if (unknownEnums.risks.length > 0) {
      const items = unknownEnums.risks
        .map((u) => `第${u.index + 1}项风险`)
        .join('；');
      parts.push(items);
    }
    return `${parts.join('；')}的分类暂不支持编辑，请联系维护人员修正后保存。`;
  }, [unknownEnums]);

  /* Per-index lookups for inline rendering. The type select is
   * replaced with a read-only badge when the row's enum value is
   * unknown — the dropdown options only carry known values, so a
   * row whose raw value isn't in the legal set would otherwise have
   * a phantom selected state. */
  const unknownMetricTypeByIdx = useMemo(() => {
    const m = new Map<number, string>();
    for (const u of unknownEnums.metrics) m.set(u.index, u.raw);
    return m;
  }, [unknownEnums]);
  const unknownRiskCategoryByIdx = useMemo(() => {
    const m = new Map<number, string>();
    for (const u of unknownEnums.risks) m.set(u.index, u.raw);
    return m;
  }, [unknownEnums]);

  const canSave = useMemo(() => {
    if (!draft || saving) return false;
    if (!dirty) return false;
    if (unknownBlockReason !== null) return false;
    return validationResult === null;
  }, [dirty, draft, saving, validationResult, unknownBlockReason]);

  /* ---------- Save ---------- */

  const onSave = useCallback(async () => {
    if (!requirement || !draft) return;
    if (!canSave) return;
    const mySignal = signal;
    setSaving(true);
    setSaveError(null);
    setValidationMessage(null);
    const body: RequirementPatchRequest = { content: draft };
    if (status !== originalStatus) body.status = status;
    const requestScope = requestScopeRef.current;
    if (requirement.project_id !== requestScope.projectId) return;
    const res = await client.patchRequirement(requirement.id, body, mySignal ?? undefined, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal?.aborted) {
      setSaving(false);
      return;
    }
    setSaving(false);
    if (res.error || !res.data) {
      setSaveError(res.error ? classifyError(res.error) : '保存失败。');
      return;
    }
    setRequirement(res.data);
    setDraft(buildContent(res.data));
    setStatus(res.data.status as RequirementStatus);
    setOriginalStatus(res.data.status as RequirementStatus);
    // The save succeeded — re-scan for unknowns against the freshly
    // persisted response so the gate stays in sync if the backend
    // re-introduced (or fixed) an unknown enum value during this PATCH.
    setUnknownEnums(detectUnknownEnums(res.data));
    setSavedTick(Date.now());
  }, [canSave, client, draft, requirement, signal, status, originalStatus]);

  const onRetry = useCallback(() => {
    setSaveError(null);
    void onSave();
  }, [onSave]);

  /* ---------- Generate PRD (Phase 7 C2) ----------
   *
   * Sends ``POST /api/requirements/{id}/prds`` with body ``{}`` so the
   * AI client fills in the structured PRD. On success the page
   * navigates to the new PRD's detail route so the PM lands directly
   * on the freshly-minted version.
   *
   * Project isolation is implicit: the page refuses to render the
   * requirement row at all if ``requirement.project_id !==
   * activeProjectId`` (see the load gate above). By the time this
   * handler runs, ``requirement`` is non-null AND belongs to the
   * active project — no cross-project POST can land.
   *
   * The signal captured at handler entry follows the same race-
   * protection pattern as the upstream load: if a project switch
   * aborts the in-flight create we simply drop the response.
   */
  const onGeneratePrd = useCallback(async () => {
    if (!requirement) return;
    if (generatingPrd) return;
    const mySignal = signal;
    setGeneratingPrd(true);
    setGeneratePrdError(null);
    setGeneratePrdSuccess(null);
    const requestScope = requestScopeRef.current;
    if (requirement.project_id !== requestScope.projectId) return;
    const res = await client.createPrd(
      requirement.id,
      // Phase 8 v7 — opt into the revised PRD prompt.
      // The backend accepts an empty body (AI client
      // generates the PRD) and an unset ``prompt_version``;
      // the explicit pin documents which prompt authored
      // the row and keeps PM edit / approval flow
      // unchanged.
      { prompt_version: 'prd-generation-v4' },
      mySignal ?? undefined, requestScope.projectId ?? undefined);
    if (requestScopeRef.current !== requestScope) return;
    if (mySignal?.aborted) {
      setGeneratingPrd(false);
      return;
    }
    setGeneratingPrd(false);
    if (res.error || !res.data) {
      setGeneratePrdError(
        res.error ? classifyError(res.error) : '生成需求文档失败。',
      );
      return;
    }
    setGeneratePrdSuccess({ id: res.data.id, version: res.data.version });
    router.push(`/prds/${res.data.id}`);
  }, [client, generatingPrd, requirement, router, signal]);

  /* ---------- Tool Action Preview (Phase 8 C) ----------
   *
   * Three handlers (Prepare / Cancel / Approve) plus a refetch
   * helper. Every handler captures ``mySignal = signal`` at entry
   * and bails out when the project-scope AbortController fires
   * (i.e. a project switch aborted the in-flight POST); this is
   * the same race-protection pattern as ``load`` /
   * ``onGeneratePrd`` above.
   *
   * Idempotency: each Prepare click mints a fresh
   * ``crypto.randomUUID()`` so a real backend would create a
   * fresh awaiting_approval row. The page never re-uses a
   * request_id across clicks; the brief says "重复点击防重" —
   * we rely on the in-flight flag (a second click is a no-op
   * because the button is disabled) AND on the unique
   * request_id so a double-click that races past the disabled
   * gate still cannot create two equivalent rows.
   *
   * Cross-project safety: ``requirement.project_id ===
   * activeProjectId`` is already enforced by the load gate
   * above, so the ``project_id`` query we send to every tool
   * endpoint is always the active project's id.
   */

  const refetchToolHistory = useCallback(async () => {
    if (!requirement || !activeProjectId) return;
    const mySignal = signal;
    const myId = requirement.id;
    setToolHistoryLoading(true);
    setToolHistoryError(null);
    const res = await client.listToolRuns(
      requirement.id,
      { project_id: activeProjectId, limit: 50, offset: 0 },
      mySignal ?? undefined,
    );
    if (mySignal?.aborted) {
      setToolHistoryLoading(false);
      return;
    }
    // URL-id race: navigating between two Requirements while a
    // listToolRuns is in flight could otherwise overwrite the
    // new requirement's history with the old requirement's rows.
    if (myId !== requirementId) {
      setToolHistoryLoading(false);
      return;
    }
    setToolHistoryLoading(false);
    if (res.error || !res.data) {
      setToolHistoryError(
        res.error ? classifyError(res.error) : '加载工具运行历史失败。',
      );
      return;
    }
    setToolHistory(res.data);
  }, [activeProjectId, client, requirement, requirementId, signal]);

  // Refetch whenever a fresh Requirement lands. The dependency on
  // ``requirement?.id`` + ``activeProjectId`` keeps the network
  // round-trips bounded to the project / id transitions the user
  // actually makes (i.e. a project switch + the matching load
  // clears history BEFORE this effect runs because the
  // activeProjectId effect above zeros the state first).
  useEffect(() => {
    if (!requirement || !activeProjectId) return;
    if (requirement.status !== 'approved') return;
    void refetchToolHistory();
  }, [activeProjectId, refetchToolHistory, requirement]);

  const onPrepareToolRun = useCallback(async () => {
    if (!requirement || !activeProjectId) return;
    if (preparingToolRun) return;
    if (inFlightRef.current) return;
    if (requirement.status !== 'approved') {
      setToolActionError('请先批准需求，再预览模拟任务。');
      return;
    }
    inFlightRef.current = true;
    const mySignal = signal;
    const requestId = crypto.randomUUID();
    setPreparingToolRun(true);
    setToolActionError(null);
    setFailedToolAction(null);
    const res = await client.prepareToolRun(
      requirement.id,
      { project_id: activeProjectId },
      {
        tool_name: 'simulated_issue_tracker',
        tool_action: 'create_issue',
        request_id: requestId,
        payload: {},
      },
      mySignal ?? undefined,
    );
    if (mySignal?.aborted) {
      setPreparingToolRun(false);
      inFlightRef.current = false;
      return;
    }
    setPreparingToolRun(false);
    inFlightRef.current = false;
    if (res.error || !res.data) {
      setToolActionError(
        res.error ? classifyError(res.error) : '准备工具运行失败。',
      );
      setFailedToolAction('prepare');
      return;
    }
    setLastPreparedRequestId(requestId);
    await refetchToolHistory();
  }, [activeProjectId, client, preparingToolRun, refetchToolHistory, requirement, signal]);

  const onCancelToolRun = useCallback(async () => {
    if (!requirement || !activeProjectId) return;
    if (cancellingToolRun || approvingToolRun) return;
    if (inFlightRef.current) return;
    const targetRunId = toolHistory?.active_run_id;
    if (!targetRunId) {
      setToolActionError('当前没有可取消的待批准工具运行。');
      return;
    }
    inFlightRef.current = true;
    const mySignal = signal;
    setCancellingToolRun(true);
    setToolActionError(null);
    setFailedToolAction(null);
    const res = await client.cancelToolRun(
      targetRunId,
      { project_id: activeProjectId },
      mySignal ?? undefined,
    );
    if (mySignal?.aborted) {
      setCancellingToolRun(false);
      inFlightRef.current = false;
      return;
    }
    setCancellingToolRun(false);
    inFlightRef.current = false;
    if (res.error || !res.data) {
      setToolActionError(
        res.error ? classifyError(res.error) : '取消失败。',
      );
      setFailedToolAction('cancel');
      return;
    }
    await refetchToolHistory();
  }, [activeProjectId, approvingToolRun, cancellingToolRun, client, refetchToolHistory, requirement, signal, toolHistory]);

  const onApproveToolRun = useCallback(async () => {
    if (!requirement || !activeProjectId) return;
    if (cancellingToolRun || approvingToolRun) return;
    if (inFlightRef.current) return;
    const targetRunId = toolHistory?.active_run_id;
    if (!targetRunId) {
      setToolActionError('当前没有可批准的待批准工具运行。');
      return;
    }
    inFlightRef.current = true;
    const mySignal = signal;
    setApprovingToolRun(true);
    setToolActionError(null);
    setFailedToolAction(null);
    const res = await client.approveToolRun(
      targetRunId,
      { project_id: activeProjectId },
      mySignal ?? undefined,
    );
    if (mySignal?.aborted) {
      setApprovingToolRun(false);
      inFlightRef.current = false;
      return;
    }
    setApprovingToolRun(false);
    inFlightRef.current = false;
    if (res.error || !res.data) {
      setToolActionError(
        res.error ? classifyError(res.error) : '批准失败。',
      );
      setFailedToolAction('approve');
      return;
    }
    await refetchToolHistory();
  }, [activeProjectId, approvingToolRun, cancellingToolRun, client, refetchToolHistory, requirement, signal, toolHistory]);

  /* ---------- Mutators ---------- */

  const setName = (v: string) => setDraft((d) => (d ? { ...d, name: v } : d));
  const setProblem = (v: string) => setDraft((d) => (d ? { ...d, problem: v } : d));
  const setTargetUser = (v: string) => setDraft((d) => (d ? { ...d, target_user: v } : d));
  const setUserStory = (v: string) => setDraft((d) => (d ? { ...d, user_story: v } : d));
  const setSolution = (v: string) =>
    setDraft((d) => (d ? { ...d, solution_hypothesis: v } : d));

  const setMetric = (idx: number, patch: Partial<RequirementContent['success_metrics'][number]>) =>
    setDraft((d) => {
      if (!d) return d;
      const next = [...d.success_metrics];
      next[idx] = { ...next[idx]!, ...patch };
      return { ...d, success_metrics: next };
    });

  const addMetric = () =>
    setDraft((d) => (d ? { ...d, success_metrics: [...d.success_metrics, blankMetric()] } : d));

  const removeMetric = (idx: number) =>
    setDraft((d) => {
      if (!d) return d;
      if (d.success_metrics.length <= 1) return d;
      const next = [...d.success_metrics];
      next.splice(idx, 1);
      return { ...d, success_metrics: next };
    });

  const setRisk = (idx: number, patch: Partial<RequirementContent['risks'][number]>) =>
    setDraft((d) => {
      if (!d) return d;
      const next = [...d.risks];
      next[idx] = { ...next[idx]!, ...patch };
      return { ...d, risks: next };
    });

  const addRisk = () =>
    setDraft((d) => (d ? { ...d, risks: [...d.risks, blankRisk()] } : d));

  const removeRisk = (idx: number) =>
    setDraft((d) => {
      if (!d) return d;
      if (d.risks.length <= 1) return d;
      const next = [...d.risks];
      next.splice(idx, 1);
      return { ...d, risks: next };
    });

  const setEvidence = (
    idx: number,
    patch: Partial<RequirementContent['evidence_references'][number]>,
  ) =>
    setDraft((d) => {
      if (!d) return d;
      const next = [...d.evidence_references];
      next[idx] = { ...next[idx]!, ...patch };
      return { ...d, evidence_references: next };
    });

  const addEvidence = () =>
    setDraft((d) => (d ? { ...d, evidence_references: [...d.evidence_references, blankEvidence()] } : d));

  // evidence_references MUST keep at least one row per the backend's
  // schema (``min_length=1``). The remove button is therefore hidden on
  // the last remaining entry.
  const removeEvidence = (idx: number) =>
    setDraft((d) => {
      if (!d) return d;
      if (d.evidence_references.length <= 1) return d;
      const next = [...d.evidence_references];
      next.splice(idx, 1);
      return { ...d, evidence_references: next };
    });

  /* ---------- Render ---------- */

  if (!activeProjectId) {
    return (
      <EmptyState
        title="请先选择项目"
        description="在右上角选择或创建一个项目，以查看需求详情。"
      />
    );
  }

  if (loading && !requirement) return <LoadingState />;
  if (notFound) {
    return (
      <EmptyState
        title="需求不存在"
        description={'当前项目中没有这条需求，请检查所选项目。'}
        action={<Link className="btn btn-ghost btn-sm" href="/requirements">返回列表</Link>}
      />
    );
  }
  if (loadError && !requirement) {
    return (
      <ErrorState
        message={loadError}
        onRetry={() => { setLoadError(null); void load(); }}
      />
    );
  }
  if (!requirement || !draft) return <LoadingState />;

  return (
    <div className="page page-requirement-detail">
      <header className="page-header">
        <span className="eyebrow">需求详情</span>
        <h1 className="page-title">{requirement.name || '未命名需求'}</h1>
        <p className="page-subtitle">
          <Link href={`/insights/${requirement.insight_id}`}>查看来源洞察</Link>
          {' · 更新于 '}{formatRelative(requirement.updated_at ?? null)}
        </p>
      </header>

      <section className="section" data-od-id="requirement-meta">
        <div className="meta-grid">
          <div className="meta-item">
            <div className="meta-k">需求优先级</div>
            <div className="meta-v">
              <span className="badge badge-accent mono">{requirement.pm_priority}</span>
              <span className="muted" style={{ marginLeft: 8, fontSize: 'var(--t-caption)' }}>
                来自产品决策
              </span>
            </div>
          </div>
          <div className="meta-item">
            <div className="meta-k">来源洞察状态</div>
            <div className="meta-v mono">{displayLabel(requirement.insight_status_snapshot)}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">来源决策状态</div>
            <div className="meta-v mono">{displayLabel(requirement.decision_status_snapshot)}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">AI 建议优先级</div>
            <div className="meta-v mono">{requirement.ai_priority_snapshot ?? '—'}</div>
          </div>
        </div>
      </section>

      <section className="section" data-od-id="requirement-status">
        {unknownBlockReason ? (
          <ErrorState
            title="部分内容需要修正"
            message="部分指标或风险的分类暂不支持编辑，请联系维护人员处理。"
            details={unknownBlockReason}
          />
        ) : null}
        <h3>状态</h3>
        <label className="form-field">
          <span className="form-label">需求状态</span>
          <select
            className="form-select"
            value={status}
            onChange={(e) => setStatus(e.target.value as RequirementStatus)}
            data-testid="status-select"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        {status !== originalStatus ? (
          <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
            状态将由 <code className="mono">{displayLabel(originalStatus)}</code> 改为 <code className="mono">{displayLabel(status)}</code>。
          </p>
        ) : null}
      </section>

      <section className="section" data-od-id="requirement-name">
        <label className="form-field">
          <span className="form-label">需求名称</span>
          <input
            type="text"
            className="form-input"
            value={documentText(draft.name)}
            onChange={(e) => setName(e.target.value)}
            data-testid="field-name"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-problem">
        <label className="form-field">
          <span className="form-label">用户问题</span>
          <textarea
            className="form-input"
            rows={3}
            value={documentText(draft.problem)}
            onChange={(e) => setProblem(e.target.value)}
            data-testid="field-problem"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-target-user">
        <label className="form-field">
          <span className="form-label">目标用户</span>
          <input
            type="text"
            className="form-input"
            value={documentText(draft.target_user)}
            onChange={(e) => setTargetUser(e.target.value)}
            data-testid="field-target-user"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-user-story">
        <label className="form-field">
          <span className="form-label">用户故事</span>
          <textarea
            className="form-input"
            rows={3}
            value={documentText(draft.user_story)}
            onChange={(e) => setUserStory(e.target.value)}
            data-testid="field-user-story"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-solution">
        <label className="form-field">
          <span className="form-label">方案假设</span>
          <textarea
            className="form-input"
            rows={3}
            value={documentText(draft.solution_hypothesis)}
            onChange={(e) => setSolution(e.target.value)}
            data-testid="field-solution"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-success-metrics">
        <h3>
          成功指标 · <span className="muted">{draft.success_metrics.length}</span>
        </h3>
        <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
          填写希望观察的指标，暂时没有可留空。
        </p>
        {draft.success_metrics.map((m, idx) => {
          const unknownType = unknownMetricTypeByIdx.get(idx);
          return (
          <div key={idx} className="metric-row" data-testid="success-metric-row">
            {unknownType !== undefined ? (
              <div className="alert alert-warning" data-testid="metric-unknown-type-warning">
                此指标类型暂不支持编辑，请联系维护人员处理。
              </div>
            ) : null}
            <label className="form-field">
              <span className="form-label">指标名称</span>
              <input
                type="text"
                className="form-input"
                value={documentText(m.name)}
                onChange={(e) => setMetric(idx, { name: e.target.value })}
                data-testid="metric-name"
              />
            </label>
            <label className="form-field">
              <span className="form-label">类型</span>
              {unknownType !== undefined ? (
                <span
                  className="badge badge-warning mono"
                  data-testid="metric-unknown-type-badge"
                >
                  暂不支持的指标类型
                </span>
              ) : (
                <select
                  className="form-select"
                  value={m.type}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (isMetricType(v)) setMetric(idx, { type: v });
                  }}
                >
                  {METRIC_TYPE_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              )}
            </label>
            <label className="form-field">
              <span className="form-label">来源（每行一条）</span>
              <textarea
                className="form-input"
                rows={2}
                value={(m.sources ?? []).join('\n')}
                onChange={(e) =>
                  setMetric(idx, {
                    sources: e.target.value
                      .split('\n')
                      .map((s) => s.trim())
                      .filter(Boolean),
                  })
                }
              />
            </label>
            <label className="form-field">
              <span className="form-label">说明</span>
              <textarea
                className="form-input"
                rows={2}
                value={metricNote(m.note ?? '')}
                onChange={(e) => setMetric(idx, { note: e.target.value })}
                data-testid="metric-note"
              />
            </label>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => removeMetric(idx)}
                disabled={draft.success_metrics.length <= 1}
                data-testid="metric-remove"
              >
                删除
              </button>
            </div>
          </div>
          );
        })}
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={addMetric}
          data-testid="metric-add"
        >
          + 添加指标
        </button>
      </section>

      <section className="section" data-od-id="requirement-risks">
        <h3>
          风险 · <span className="muted">{draft.risks.length}</span>
        </h3>
        <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
          记录可能影响需求落地的风险。
        </p>
        {draft.risks.some((risk) => risk.category === 'known') ? (
          <p className="muted" data-testid="known-risk-review-boundary">
            请对照原始反馈核实已知风险。
          </p>
        ) : null}
        {draft.risks.map((r, idx) => {
          const unknownCategory = unknownRiskCategoryByIdx.get(idx);
          return (
          <div key={idx} className="risk-row" data-testid="risk-row">
            {unknownCategory !== undefined ? (
              <div className="alert alert-warning" data-testid="risk-unknown-category-warning">
                此风险分类暂不支持编辑，请联系维护人员处理。
              </div>
            ) : null}
            <label className="form-field">
              <span className="form-label">类别</span>
              {unknownCategory !== undefined ? (
                <span
                  className="badge badge-warning mono"
                  data-testid="risk-unknown-category-badge"
                >
                  暂不支持的风险分类
                </span>
              ) : (
                <select
                  className="form-select"
                  value={r.category}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (isRiskCategory(v)) setRisk(idx, { category: v });
                  }}
                >
                  {RISK_CATEGORY_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              )}
            </label>
            <label className="form-field">
              <span className="form-label">描述</span>
              <textarea
                className="form-input"
                rows={3}
                value={documentText(r.description)}
                onChange={(e) => setRisk(idx, { description: e.target.value })}
                data-testid="risk-description"
              />
            </label>
            <label className="form-field">
              <span className="form-label">缓解方案</span>
              <textarea
                className="form-input"
                rows={2}
                value={documentText(r.mitigation ?? '')}
                onChange={(e) => setRisk(idx, { mitigation: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span className="form-label">反馈编号（每行一条）</span>
              <textarea
                className="form-input"
                rows={2}
                value={(r.evidence_ids ?? []).join('\n')}
                onChange={(e) =>
                  setRisk(idx, {
                    evidence_ids: e.target.value
                      .split('\n')
                      .map((s) => s.trim())
                      .filter(Boolean),
                  })
                }
              />
            </label>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => removeRisk(idx)}
                disabled={draft.risks.length <= 1}
                data-testid="risk-remove"
              >
                删除
              </button>
            </div>
          </div>
          );
        })}
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={addRisk}
          data-testid="risk-add"
        >
          + 添加风险
        </button>
      </section>

      <section className="section" data-od-id="requirement-evidence">
        <h3>
          引用反馈 · <span className="muted">{draft.evidence_references.length}</span>
        </h3>
        <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
          至少引用一条反馈，并说明它支持什么。
        </p>
        {draft.evidence_references.map((e, idx) => (
          <div key={idx} className="evidence-row-ref" data-testid="evidence-ref-row">
            <label className="form-field">
              <span className="form-label">反馈编号</span>
              <input
                type="text"
                className="form-input mono"
                value={e.external_id}
                onChange={(ev) => setEvidence(idx, { external_id: ev.target.value })}
                data-testid="evidence-external-id"
              />
            </label>
            <label className="form-field">
              <span className="form-label">引用用途</span>
              <input
                type="text"
                className="form-input"
                value={documentText(e.usage)}
                onChange={(ev) => setEvidence(idx, { usage: ev.target.value })}
                data-testid="evidence-usage"
              />
            </label>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => removeEvidence(idx)}
                disabled={draft.evidence_references.length <= 1}
                data-testid="evidence-remove"
              >
                删除
              </button>
            </div>
          </div>
        ))}
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={addEvidence}
          data-testid="evidence-add"
        >
          + 添加引用反馈
        </button>
      </section>

      {/* ---------- Phase 8 C: Tool Action Preview (Simulator) ----------
       *
       * Renders ONLY when the current Requirement is in
       * ``status='approved'`` AND belongs to the active project
       * (the load gate above enforces the project check; the
       * status check is here). The section is deliberately
       * placed BELOW the editable form so a PM can still edit
       * the Requirement content / status / Save / Generate PRD
       * without interference from the simulator controls.
       *
       * The brief mandates this is a SIMULATOR — the user must
       * never believe a real Jira / external ticket was created.
       * The "仅模拟" notice is the first thing rendered, and
       * the "simulated" badge next to the SIM-… id is the
       * audit-visible marker. No real external link is ever
       * constructed; the id is shown as plain monospace text. */}
      {requirement.status === 'approved' ? (
        <section className="section tool-action-preview" data-od-id="tool-action-preview" data-testid="tool-action-preview">
          <h3>
            外部任务预览（模拟）
            <span className="muted" style={{ marginLeft: 8, fontSize: 'var(--t-caption)' }}>
              仅供预览
            </span>
          </h3>
          <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
            此操作为模拟，不会创建真实外部任务。
          </p>
          {renderToolPreview(
            toolHistory,
            toolHistoryLoading,
            toolHistoryError,
            preparingToolRun,
            cancellingToolRun,
            approvingToolRun,
            toolActionError,
            failedToolAction,
            onPrepareToolRun,
            onCancelToolRun,
            onApproveToolRun,
          )}
        </section>
      ) : null}

      <footer className="cta-bar">
        <div>
          <h4>
            {generatingPrd
              ? '正在生成需求文档…'
              : generatePrdSuccess
                ? `已生成需求文档 v${generatePrdSuccess.version}`
                : saving
                  ? '正在保存…'
                  : dirty
                    ? '有未保存的修改'
                    : savedTick
                      ? '已保存'
                      : '同步状态'}
          </h4>
          <p>
            {generatingPrd
              ? '正在整理需求并生成文档，请稍候。'
              : generatePrdSuccess
                ? '文档已生成，正在打开详情。'
                : saving
                  ? '正在保存修改，请稍候。'
                  : dirty
                    ? (unknownBlockReason ? '请先处理需要修正的内容。' : validationResult ?? '修改尚未保存。')
                    : savedTick
                      ? `最近一次保存于 ${formatRelative(new Date(savedTick).toISOString())}。`
                      : '当前未对内容做修改。'}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Link className="btn btn-ghost btn-sm" href="/requirements">返回列表</Link>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={generatingPrd}
            onClick={() => { void onGeneratePrd(); }}
            data-testid="generate-prd"
          >
            {generatingPrd ? '正在生成文档…' : '生成需求文档'}
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!canSave}
            onClick={() => {
              setValidationMessage(null);
              if (validationResult) {
                setValidationMessage(validationResult);
                return;
              }
              void onSave();
            }}
            data-testid="save-requirement"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </footer>

      {generatePrdSuccess ? (
        <div className="alert alert-success" role="status" data-testid="generate-prd-success">
          已生成需求文档 v{generatePrdSuccess.version}，正在跳转…
        </div>
      ) : null}
      {generatePrdError ? (
        <ErrorState
          title="生成需求文档失败"
          message={generatePrdError}
          onRetry={() => { setGeneratePrdError(null); void onGeneratePrd(); }}
        />
      ) : null}

      {validationMessage ? (
        <ErrorState
          title="表单尚未通过校验"
          message={validationMessage}
          onRetry={() => setValidationMessage(null)}
        />
      ) : null}
      {saveError ? (
        <ErrorState
          title="保存失败"
          message={saveError}
          onRetry={() => { setSaveError(null); void onRetry(); }}
        />
      ) : null}
    </div>
  );
}

function shallowEqualContent(a: RequirementContent, b: RequirementContent): boolean {
  if (
    a.name !== b.name ||
    a.problem !== b.problem ||
    a.target_user !== b.target_user ||
    a.user_story !== b.user_story ||
    a.solution_hypothesis !== b.solution_hypothesis
  ) {
    return false;
  }
  const aMetrics = JSON.stringify(a.success_metrics);
  const bMetrics = JSON.stringify(b.success_metrics);
  if (aMetrics !== bMetrics) return false;
  const aRisks = JSON.stringify(a.risks);
  const bRisks = JSON.stringify(b.risks);
  if (aRisks !== bRisks) return false;
  const aRefs = JSON.stringify(a.evidence_references);
  const bRefs = JSON.stringify(b.evidence_references);
  return aRefs === bRefs;
}

/* ---------- Tool Action Preview renderer ----------
 *
 * Pure render helper that takes a snapshot of the page's tool
 * state plus the three handler references and returns the
 * appropriate section body. Split out so the page's JSX stays
 * readable and so the interaction tests can assert on a stable
 * ``data-testid`` set without spelunking through conditional
 * ternaries.
 *
 * State precedence:
 *   1. ``toolActionError`` — surfaces 404 / 409 / 503 / network
 *      errors with a Retry that re-runs the same action by
 *      re-invoking the supplied handler.
 *   2. ``toolHistoryError`` — surfaces history-list failures
 *      (does not block a fresh Prepare).
 *   3. Active awaiting_approval run → Cancel + Approve + preview.
 *   4. Active completed run → SIM-… + simulated badge (no real
 *      link fabricated).
 *   5. Active cancelled run → "已取消，无模拟结果" + Prepare
 *      re-try. We deliberately do NOT auto-Prepare here — the
 *      brief says "可重试" and a fresh Prepare must be a
 *      deliberate click.
 *   6. No history yet → Prepare button only.
 */
function renderToolPreview(
  history: ToolRunPage | null,
  loading: boolean,
  historyError: string | null,
  preparing: boolean,
  cancelling: boolean,
  approving: boolean,
  actionError: string | null,
  failedAction: 'prepare' | 'cancel' | 'approve' | null,
  onPrepare: () => void,
  onCancel: () => void,
  onApprove: () => void,
) {
  // ``active_run_id`` is the awaiting_approval run when one
  // exists (the row Cancel / Approve target). After Cancel /
  // Approve the backend nulls the pointer; in that case we
  // must still render the most recent row so the user can see
  // the resulting state (``cancelled`` / ``completed``) and the
  // re-Prepare path. ``items`` is pre-sorted ``created_at DESC,
  // id DESC`` by both the backend and the mock, so
  // ``items[0]`` is the most recent run.
  const activeRun =
    history?.active_run_id !== null && history?.active_run_id !== undefined
      ? (history?.items.find((r) => r.id === history?.active_run_id) ?? null)
      : (history?.items[0] ?? null);
  // ``actionError`` is the per-action failure (Cancel / Approve
  // / Prepare). It MUST take priority over the "active run"
  // view so a 409 "state machine violation" surfaces even when
  // a fresh refetch already moved the row to cancelled.
  if (actionError) {
    // Re-issue the EXACT action that failed. The brief says
    // "失败可重试" — a Cancel error retrying as Prepare would
    // be a contract violation (a fresh audit row would land
    // while the user expected a state-machine re-attempt).
    const retry = () => {
      if (failedAction === 'cancel') onCancel();
      else if (failedAction === 'approve') onApprove();
      else onPrepare();
    };
    return (
      <ErrorState
        title="工具操作失败"
        message={actionError}
        onRetry={retry}
      />
    );
  }
  if (loading && !history) {
    return <LoadingState />;
  }
  if (historyError && !history) {
    return (
      <ErrorState
        title="加载工具运行历史失败"
        message={historyError}
        onRetry={onPrepare}
      />
    );
  }
  if (activeRun && activeRun.status === 'awaiting_approval') {
    return (
      <ToolAwaitingApproval
        snapshot={activeRun.request_payload}
        preparing={preparing}
        cancelling={cancelling}
        approving={approving}
        onCancel={onCancel}
        onApprove={onApprove}
      />
    );
  }
  if (activeRun && activeRun.status === 'completed') {
    return (
      <ToolCompleted
        externalId={activeRun.external_id}
        responseSummary={activeRun.response_summary}
        onPrepare={onPrepare}
        preparing={preparing}
      />
    );
  }
  if (activeRun && activeRun.status === 'cancelled') {
    return (
      <ToolCancelled
        onPrepare={onPrepare}
        preparing={preparing}
      />
    );
  }
  return (
    <ToolIdle
      preparing={preparing}
      onPrepare={onPrepare}
    />
  );
}

function ToolSnapshotFields({ snapshot }: { snapshot: ToolRunSnapshot }) {
  return (
    <div className="tool-preview-grid" data-testid="tool-preview-fields">
      <div className="tool-preview-field">
        <div className="tool-preview-label">任务标题</div>
        <div className="tool-preview-value mono" data-testid="tool-preview-title">
          {snapshot.title || '—'}
        </div>
      </div>
      <div className="tool-preview-field">
        <div className="tool-preview-label">任务描述</div>
        <div className="tool-preview-value tool-preview-description" data-testid="tool-preview-description">
          {snapshot.description || '—'}
        </div>
      </div>
      <div className="tool-preview-field">
        <div className="tool-preview-label">优先级</div>
        <div className="tool-preview-value mono" data-testid="tool-preview-priority">
          {snapshot.priority ?? '—'}
        </div>
      </div>
      <div className="tool-preview-field">
        <div className="tool-preview-label">来源洞察</div>
        <div className="tool-preview-value tool-preview-source" data-testid="tool-preview-source">
          {snapshot.source_insight_preview || '—'}
        </div>
      </div>
    </div>
  );
}

function ToolIdle({ preparing, onPrepare }: { preparing: boolean; onPrepare: () => void }) {
  return (
    <div className="tool-action-controls" data-testid="tool-action-idle">
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={onPrepare}
        disabled={preparing}
        data-testid="tool-prepare"
      >
        {preparing ? '准备中…' : '预览模拟任务'}
      </button>
    </div>
  );
}

function ToolAwaitingApproval({
  snapshot,
  preparing,
  cancelling,
  approving,
  onCancel,
  onApprove,
}: {
  snapshot: ToolRunSnapshot;
  preparing: boolean;
  cancelling: boolean;
  approving: boolean;
  onCancel: () => void;
  onApprove: () => void;
}) {
  return (
    <div data-testid="tool-action-awaiting-approval">
      <ToolSnapshotFields snapshot={snapshot} />
      <div className="alert alert-info" role="status" data-testid="tool-awaiting-notice">
        请确认预览内容后批准模拟运行。
      </div>
      <div className="tool-action-controls" data-testid="tool-action-controls">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onCancel}
          disabled={cancelling || approving || preparing}
          data-testid="tool-cancel"
        >
          {cancelling ? '取消中…' : '取消运行'}
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={onApprove}
          disabled={cancelling || approving || preparing}
          data-testid="tool-approve"
        >
          {approving ? '批准中…' : '批准模拟运行'}
        </button>
      </div>
    </div>
  );
}

function ToolCompleted({
  externalId,
  responseSummary,
  onPrepare,
  preparing,
}: {
  externalId: string | null;
  responseSummary: string | null;
  onPrepare: () => void;
  preparing: boolean;
}) {
  return (
    <div data-testid="tool-action-completed">
      <div className="tool-completed-row" data-testid="tool-completed-row">
        <span className="tool-preview-label">模拟任务编号</span>
        <span className="badge badge-accent mono" data-testid="tool-external-id">
          {externalId ?? '—'}
        </span>
        <span
          className="badge badge-warning mono"
          data-testid="tool-simulated-tag"
          title="此编号对应模拟任务"
        >
          模拟
        </span>
      </div>
      {responseSummary ? (
        <details className="technical-details"><summary>查看运行详情</summary><p className="muted" data-testid="tool-response-summary">
          模拟运行已完成，可核对任务内容。
        </p></details>
      ) : null}
      <div className="alert alert-success" role="status" data-testid="tool-completed-notice">
        模拟任务已生成，未写入外部系统。
      </div>
      <div className="tool-action-controls">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onPrepare}
          disabled={preparing}
          data-testid="tool-reprepare"
        >
          {preparing ? '准备中…' : '重新预览'}
        </button>
      </div>
    </div>
  );
}

function ToolCancelled({ onPrepare, preparing }: { onPrepare: () => void; preparing: boolean }) {
  return (
    <div data-testid="tool-action-cancelled">
      <div className="alert alert-warn" role="status" data-testid="tool-cancelled-notice">
        已取消；无模拟结果。
      </div>
      <div className="tool-action-controls">
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={onPrepare}
          disabled={preparing}
          data-testid="tool-reprepare"
        >
          {preparing ? '准备中…' : '重新预览'}
        </button>
      </div>
    </div>
  );
}
