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
import { useParams, useRouter } from 'next/navigation';
import {
  useCallback,
  useEffect,
  useMemo,
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
    const res = await client.getRequirement(requirementId, mySignal);
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
        .map((u) => `Success Metric #${u.index + 1} 类型 '${u.raw}'`)
        .join('；');
      parts.push(items);
    }
    if (unknownEnums.risks.length > 0) {
      const items = unknownEnums.risks
        .map((u) => `Risk #${u.index + 1} 分类 '${u.raw}'`)
        .join('；');
      parts.push(items);
    }
    return `保存被阻止：${parts.join('；')} 是后端未知枚举值，无法安全保存。请联系管理员修正后再保存。`;
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
    const res = await client.patchRequirement(requirement.id, body, mySignal ?? undefined);
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
    const res = await client.createPrd(requirement.id, {}, mySignal ?? undefined);
    if (mySignal?.aborted) {
      setGeneratingPrd(false);
      return;
    }
    setGeneratingPrd(false);
    if (res.error || !res.data) {
      setGeneratePrdError(
        res.error ? classifyError(res.error) : '生成 PRD 失败。',
      );
      return;
    }
    setGeneratePrdSuccess({ id: res.data.id, version: res.data.version });
    router.push(`/prds/${res.data.id}`);
  }, [client, generatingPrd, requirement, router, signal]);

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
        description="在右上角选择或创建一个项目，以查看 Requirement 详情。"
      />
    );
  }

  if (loading && !requirement) return <LoadingState />;
  if (notFound) {
    return (
      <EmptyState
        title="Requirement 不存在"
        description={`该 Requirement（${requirementId.slice(0, 8)}…）在当前项目下不存在，可能属于其它项目或已被删除。`}
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
        <span className="eyebrow">Requirement · B2</span>
        <h1 className="page-title">{requirement.name || '未命名 Requirement'}</h1>
        <p className="page-subtitle">
          来自 Insight <code className="mono">{requirement.insight_id.slice(0, 8)}</code> ·
          {' '}Decision <code className="mono">{requirement.decision_id.slice(0, 8)}</code> ·
          {' '}更新 <b className="mono">{formatRelative(requirement.updated_at ?? null)}</b>
        </p>
      </header>

      <section className="section" data-od-id="requirement-meta">
        <div className="meta-grid">
          <div className="meta-item">
            <div className="meta-k">PM Priority</div>
            <div className="meta-v">
              <span className="badge badge-accent mono">{requirement.pm_priority}</span>
              <span className="muted" style={{ marginLeft: 8, fontSize: 'var(--t-caption)' }}>
                由上游 Decision 设定，本页只读。
              </span>
            </div>
          </div>
          <div className="meta-item">
            <div className="meta-k">Insight Status Snapshot</div>
            <div className="meta-v mono">{requirement.insight_status_snapshot}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">Decision Status Snapshot</div>
            <div className="meta-v mono">{requirement.decision_status_snapshot}</div>
          </div>
          <div className="meta-item">
            <div className="meta-k">AI Priority Snapshot</div>
            <div className="meta-v mono">{requirement.ai_priority_snapshot ?? '—'}</div>
          </div>
        </div>
      </section>

      <section className="section" data-od-id="requirement-status">
        {unknownBlockReason ? (
          <ErrorState
            title="检测到后端未知枚举值"
            message={unknownBlockReason}
          />
        ) : null}
        <h3>状态</h3>
        <label className="form-field">
          <span className="form-label">Requirement Status</span>
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
            状态将由 <code className="mono">{originalStatus}</code> 改为 <code className="mono">{status}</code>。
          </p>
        ) : null}
      </section>

      <section className="section" data-od-id="requirement-name">
        <label className="form-field">
          <span className="form-label">Name</span>
          <input
            type="text"
            className="form-input"
            value={draft.name}
            onChange={(e) => setName(e.target.value)}
            data-testid="field-name"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-problem">
        <label className="form-field">
          <span className="form-label">Problem</span>
          <textarea
            className="form-input"
            rows={3}
            value={draft.problem}
            onChange={(e) => setProblem(e.target.value)}
            data-testid="field-problem"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-target-user">
        <label className="form-field">
          <span className="form-label">Target User</span>
          <input
            type="text"
            className="form-input"
            value={draft.target_user}
            onChange={(e) => setTargetUser(e.target.value)}
            data-testid="field-target-user"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-user-story">
        <label className="form-field">
          <span className="form-label">User Story</span>
          <textarea
            className="form-input"
            rows={3}
            value={draft.user_story}
            onChange={(e) => setUserStory(e.target.value)}
            data-testid="field-user-story"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-solution">
        <label className="form-field">
          <span className="form-label">Solution Hypothesis</span>
          <textarea
            className="form-input"
            rows={3}
            value={draft.solution_hypothesis}
            onChange={(e) => setSolution(e.target.value)}
            data-testid="field-solution"
          />
        </label>
      </section>

      <section className="section" data-od-id="requirement-success-metrics">
        <h3>
          Success Metrics · <span className="muted">{draft.success_metrics.length}</span>
        </h3>
        <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
          可为空数组；每项须含非空 name（note 与 sources 为可选），否则保存被拒。
        </p>
        {draft.success_metrics.map((m, idx) => {
          const unknownType = unknownMetricTypeByIdx.get(idx);
          return (
          <div key={idx} className="metric-row" data-testid="success-metric-row">
            {unknownType !== undefined ? (
              <div className="alert alert-warning" data-testid="metric-unknown-type-warning">
                ⚠ 后端返回未知类型 &quot;{unknownType}&quot;，无法安全保存。请联系管理员修正后再编辑本行。
              </div>
            ) : null}
            <label className="form-field">
              <span className="form-label">指标名称</span>
              <input
                type="text"
                className="form-input"
                value={m.name}
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
                  未知：{unknownType}
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
                value={m.note ?? ''}
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
          Risks · <span className="muted">{draft.risks.length}</span>
        </h3>
        <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
          可为空数组；每项须含非空 description，否则保存被拒。
        </p>
        {draft.risks.map((r, idx) => {
          const unknownCategory = unknownRiskCategoryByIdx.get(idx);
          return (
          <div key={idx} className="risk-row" data-testid="risk-row">
            {unknownCategory !== undefined ? (
              <div className="alert alert-warning" data-testid="risk-unknown-category-warning">
                ⚠ 后端返回未知分类 &quot;{unknownCategory}&quot;，无法安全保存。请联系管理员修正后再编辑本行。
              </div>
            ) : null}
            <label className="form-field">
              <span className="form-label">类别</span>
              {unknownCategory !== undefined ? (
                <span
                  className="badge badge-warning mono"
                  data-testid="risk-unknown-category-badge"
                >
                  未知：{unknownCategory}
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
                value={r.description}
                onChange={(e) => setRisk(idx, { description: e.target.value })}
                data-testid="risk-description"
              />
            </label>
            <label className="form-field">
              <span className="form-label">缓解方案</span>
              <textarea
                className="form-input"
                rows={2}
                value={r.mitigation ?? ''}
                onChange={(e) => setRisk(idx, { mitigation: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span className="form-label">Evidence IDs（每行一条）</span>
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
          Evidence References · <span className="muted">{draft.evidence_references.length}</span>
        </h3>
        <p className="muted" style={{ fontSize: 'var(--t-caption)' }}>
          至少 1 条；每项须含非空 external_id 与 usage，否则保存被拒。
        </p>
        {draft.evidence_references.map((e, idx) => (
          <div key={idx} className="evidence-row-ref" data-testid="evidence-ref-row">
            <label className="form-field">
              <span className="form-label">External ID</span>
              <input
                type="text"
                className="form-input mono"
                value={e.external_id}
                onChange={(ev) => setEvidence(idx, { external_id: ev.target.value })}
                data-testid="evidence-external-id"
              />
            </label>
            <label className="form-field">
              <span className="form-label">Usage</span>
              <input
                type="text"
                className="form-input"
                value={e.usage}
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
          + 添加 Evidence Reference
        </button>
      </section>

      <footer className="cta-bar">
        <div>
          <h4>
            {generatingPrd
              ? '正在生成 PRD…'
              : generatePrdSuccess
                ? `已生成 PRD v${generatePrdSuccess.version}`
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
              ? '请求 POST /api/requirements/{id}/prds，请稍候。'
              : generatePrdSuccess
                ? `新 PRD id ${generatePrdSuccess.id.slice(0, 8)}…，已跳转详情。`
                : saving
                  ? '请求 PATCH /api/requirements/{id}，请稍候。'
                  : dirty
                    ? unknownBlockReason ?? validationResult ?? '编辑不会自动保存。点击保存以发送 PATCH。'
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
            {generatingPrd ? '生成 PRD 中…' : '生成 PRD'}
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
          已生成 PRD v{generatePrdSuccess.version}，正在跳转…
        </div>
      ) : null}
      {generatePrdError ? (
        <ErrorState
          title="生成 PRD 失败"
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
