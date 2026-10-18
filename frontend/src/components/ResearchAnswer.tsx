'use client';

/**
 * ResearchAnswer — Phase 8 v7 extractive answer renderer.
 *
 * The v7 contract pins ``coverage_report.answer`` to a
 * strict shape:
 *
 *   {
 *     version: 'research-answer-v7',
 *     items: [
 *       { requirement_id, question, kind, status,
 *         answer_polarity, quotes[], gap, adopted_attempt }
 *     ],
 *     unresolved: [...],
 *     analyses: [
 *       { finding_id, title, description, claim_kind,
 *         supporting_finding_ids, ... }
 *     ],
 *     semantic_review: 'NOT_MEASURED',
 *   }
 *
 * The renderer never:
 *   - reads the v5 ``failure_reasons`` / ``overall_completable``
 *     keys for a v7 row (the v7 panel is bound to ``answer`` only);
 *   - claims a row is "fully answered" or "manually audited" —
 *     the page surfaces the manual-semantic-review boundary
 *     (人工语义复核：未验证) verbatim;
 *   - uses the model-supplied title or the internal
 *     ``evidence_id`` as the visible title of a quote — the
 *     user-visible title for every cited fact is the saved
 *     source ``text`` (the extractive span itself);
 *   - auto-writes a positive or negative verdict on a
 *     conflicting row. The renderer strictly surfaces the
 *     server-side ``answer_polarity`` and labels
 *     ``positive`` / ``negative`` / ``conflicting`` /
 *     ``unknown`` so the user can audit the program
 *     decision; a ``conflicting`` row is rendered as
 *     "来源结论冲突" — NEVER as a positive
 *     resolution.
 *
 * The component is a pure React function so it can be
 * unit-tested in isolation (see
 * ``components/ResearchAnswer.test.tsx``). The page
 * passes the ``answer`` payload verbatim — no shaping
 * upstream — so a malformed row would surface as a
 * missing / unknown label rather than a fabricated
 * domain answer.
 */

import { useState, type ReactNode } from 'react';
import { TechnicalDetails } from './TechnicalDetails';
import type {
  ResearchAnswerItemStatusV7,
  ResearchAnswerPolarityV7,
  ResearchAnswerV7,
  ResearchFindingRead,
} from '../lib/api/types';

/* ---------- gap + polarity + status Chinese labels ----------
 *
 * The page maps the server-side enum to a fixed Chinese
 * phrase so a PM never has to read the raw enum. Unknown
 * values fall through to a conservative "请人工核对"
 * hint rather than the raw English token (a fabricated
 * mapping for an empty bucket would be worse than the
 * fallback). */

const GAP_LABEL: Readonly<Record<string, string>> = {
  evidence_missing: '尚无原文支持',
  obligation_unfulfilled: '原问题的操作要求尚未覆盖',
  product_omitted: '仍有请求的产品未覆盖',
  comparison_side_not_covered: '比较前提已失效',
  ledger_selection_budget_exhausted: '完整支持超出本次证据预算',
  version_conflict: '版本条件存在冲突，请分别核对来源',
  device_conflict: '资料限定的设备与问题不一致',
  object_branch: '所问对象分支缺少完整原文',
  action: '原文未命中操作',
  object: '原文未命中对象',
  mode: '原文未命中执行方式',
  quantity: '原文未命中数量',
  condition: '原文未命中条件',
  procedure: '原文未按编号步骤覆盖',
  version: '原文未命中版本号',
  limit: '原文未命中限制/上限',
  entry_listing: '原文未命中入口选项',
  destination: '原文未命中目标位置',
  comparison_sides: '对照两侧不足',
  platform_conflict: '平台范围冲突',
  platform_unproven: '平台范围未在原文出现',
  operation_local_conflict: '本地操作不匹配',
  operation_branch_conflict: '分支操作不匹配',
  mode_conflict: '执行方式冲突',
  product_conflict: '产品身份冲突',
  fact_not_verified: '事实未通过官方源核验',
  conflicting_capability: '同时存在支持与否定',
  analysis_facets_missing: '仍有分析维度未覆盖',
  analysis_pending: '分析待验证',
};

function gapLabel(gap: string): string {
  if (!gap) return '尚无明确缺口';
  // The backend packs codes with ``;`` separators; we
  // map each code in order and preserve the boundary so
  // the user can audit the predicate chain.
  const codes = gap
    .split(';')
    .map((c) => c.trim())
    .filter((c) => c.length > 0);
  if (codes.length === 0) return '尚无明确缺口';
  return codes.map((c) => GAP_LABEL[c] ?? '支持尚未核实，请人工核对').join('；');
}

const POLARITY_LABEL: Readonly<Record<ResearchAnswerPolarityV7, string>> = {
  positive: '有支持原文',
  negative: '已引用否定',
  conflicting: '来源结论冲突',
  unknown: '未在原文出现',
};

function polarityLabel(p: ResearchAnswerPolarityV7): string {
  return POLARITY_LABEL[p] ?? POLARITY_LABEL.unknown;
}

const STATUS_LABEL: Readonly<Record<ResearchAnswerItemStatusV7, string>> = {
  covered: '已覆盖',
  partial: '部分覆盖',
  not_verified: '未验证',
};

function statusLabel(s: ResearchAnswerItemStatusV7): string {
  return STATUS_LABEL[s] ?? '请人工核对';
}

const ANALYSIS_CLAIM_KIND_LABEL: Readonly<Record<string, string>> = {
  inference: '推断（基于事实前提）',
  recommendation: '建议（待验证）',
};

const UNRESOLVED_REASON_LABEL: Readonly<Record<string, string>> = {
  anchor_omitted: '原问题片段未在分析中复述',
  condition_omitted: '原问题条件未在分析中复述',
};

/* ---------- Component ---------- */

export interface ResearchAnswerProps {
  answer: ResearchAnswerV7;
  findings: ResearchFindingRead[];
  /* The id of the run whose findings we are rendering.
   * The component filters supporting facts to
   * ``f.run_id === runId`` so a foreign-run fact
   * (cross-pollinated by a stale page state, a project
   * switch race, or a misrouted listing endpoint) cannot
   * leak into the v7 audit trace. The page MUST pass
   * ``run.id`` so the audit chain stays inside the
   * current Run. */
  runId: string;
}

export function ResearchAnswer({
  answer,
  findings,
  runId,
}: ResearchAnswerProps): ReactNode {
  // The user can open at most one analysis row at a time
  // to keep the support-trace tree audit-friendly; the
  // toggle is per ``finding_id`` so two side-by-side
  // inferences can be opened independently if the user
  // re-opens the second.
  const [expandedAnalysisId, setExpandedAnalysisId] = useState<string | null>(
    null,
  );

  return (
    <section
      className="research-answer-v7"
      data-testid="research-answer-v7"
      data-version={answer.version}
    >
      <header className="research-answer-v7-head">
        <span className="research-answer-v7-title">研究回答</span>
        <span
          className="research-answer-v7-meta muted"
          data-testid="research-answer-v7-meta"
        >
          待人工复核
        </span>
      </header>

      <p className="research-answer-v7-original-question muted">
        <span className="research-answer-v7-original-label">原问题：</span>
        <span data-testid="research-answer-v7-original-question">
          {answer.items[0]?.question ?? '（无原问题回显）'}
        </span>
      </p>

      {answer.unresolved.length > 0 ? (
        <ul
          className="research-answer-v7-unresolved"
          data-testid="research-answer-v7-unresolved"
        >
          {answer.unresolved.map((u) => (
            <li key={u.id} className="research-answer-v7-unresolved-item">
              <span className="research-answer-v7-unresolved-reason">
                {UNRESOLVED_REASON_LABEL[u.reason] ?? '原问题仍有未核实要求'}：
              </span>
              <span className="research-answer-v7-unresolved-text">{u.text}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <ol
        className="research-answer-v7-items"
        data-testid="research-answer-v7-items"
      >
        {answer.items.map((item) => (
          <li
            key={item.requirement_id}
            className="research-answer-v7-item"
            data-testid={`research-answer-v7-item-${item.requirement_id}`}
            data-status={item.status}
            data-polarity={item.answer_polarity}
          >
            <div className="research-answer-v7-item-head">
              <span
                className="research-answer-v7-item-question"
                data-testid={`research-answer-v7-item-question-${item.requirement_id}`}
              >
                {item.question || item.requirement_id}
              </span>
              <span
                className={`research-answer-v7-item-polarity research-answer-v7-item-polarity--${item.answer_polarity}`}
                data-testid={`research-answer-v7-item-polarity-${item.requirement_id}`}
                data-polarity={item.answer_polarity}
              >
                {polarityLabel(item.answer_polarity)}
              </span>
              <span
                className="research-answer-v7-item-status"
                data-testid={`research-answer-v7-item-status-${item.requirement_id}`}
              >
                {statusLabel(item.status)}
              </span>
              {item.adopted_attempt !== null ? (
                <span
                  className="research-answer-v7-item-attempt muted"
                  data-testid={`research-answer-v7-item-attempt-${item.requirement_id}`}
                >
                  采用第 {item.adopted_attempt} 轮
                </span>
              ) : null}
            </div>
            {item.version_constraints && item.version_constraints.length > 0 ? (
              <p className="muted" data-testid={`research-answer-v7-versions-${item.requirement_id}`}>
                原文版本条件：{item.version_constraints.map((value) => {
                  const names: Record<string, string> = { ios: 'iOS', ipados: 'iPadOS', macos: 'macOS', android: 'Android', windows: 'Windows', linux: 'Linux', version: '版本' };
                  const relations = { at_least: '及以上', at_most: '及以下', required: '（要求版本）', earlier_than: '之前', later_than: '之后' };
                  return `${names[value.platform] ?? value.platform} ${value.version}${relations[value.relation]}`;
                }).join('；')}
              </p>
            ) : null}
            {item.quotes.length > 0 ? (
              <ul
                className="research-answer-v7-quotes"
                data-testid={`research-answer-v7-quotes-${item.requirement_id}`}
              >
                {item.quotes.map((q, idx) => (
                  <li
                    key={`${q.finding_id}-${idx}`}
                    className="research-answer-v7-quote"
                    data-testid={`research-answer-v7-quote-${item.requirement_id}-${idx}`}
                  >
                    <p
                      className="research-answer-v7-quote-text"
                      data-testid={`research-answer-v7-quote-text-${item.requirement_id}-${idx}`}
                    >
                      原文：{q.text}
                    </p>
                    <div className="research-answer-v7-quote-meta muted">
                      <TechnicalDetails label="来源编号"><span
                        className="research-answer-v7-quote-source-id"
                        data-testid={`research-answer-v7-quote-source-id-${item.requirement_id}-${idx}`}
                      >
                        来源编号：{q.provider_source_id}
                      </span></TechnicalDetails>
                      <a
                        className="research-answer-v7-quote-link"
                        href={q.url}
                        target="_blank"
                        rel="noopener noreferrer nofollow"
                        data-testid={`research-answer-v7-quote-link-${item.requirement_id}-${idx}`}
                      >
                        打开来源
                      </a>
                    </div>
                  </li>
                ))}
              </ul>
            ) : null}
            <p
              className="research-answer-v7-gap muted"
              data-testid={`research-answer-v7-gap-${item.requirement_id}`}
            >
              缺口：{gapLabel(item.gap)}
            </p>
          </li>
        ))}
      </ol>

      {answer.analyses.length > 0 ? (
        <section
          className="research-answer-v7-analyses"
          data-testid="research-answer-v7-analyses"
        >
          <header className="research-answer-v7-analyses-head">
            分析（事实前提 / 推断 / 建议）
          </header>
          <ul className="research-answer-v7-analysis-list">
            {answer.analyses.map((row) => {
              const isOpen = expandedAnalysisId === row.finding_id;
              const claimKindLabel =
                ANALYSIS_CLAIM_KIND_LABEL[row.claim_kind] ??
                `分析（${row.claim_kind}）`;
              // The supporting-fact panel ONLY shows the
              // fact rows currently loaded for this Run,
              // filtered by ``supporting_finding_ids`` and
              // gated to same-Run + fact + confirmed. A
              // missing / foreign / unverified fact is
              // silently dropped from the rendered list
              // (the v7 envelope's audit chain surfaces
              // them via the per-requirement ``reason_codes``,
              // NOT via this UI). The toggle label and the
              // empty-list fallback below both count the
              // filtered set so a PM can tell at a glance
              // whether the cited premises are available
              // (and where they go missing).
              const supportingFacts = row.supporting_finding_ids
                .map((id) =>
                  findings.find(
                    (f) =>
                      f.id === id &&
                      f.run_id === runId &&
                      f.claim_kind === 'fact' &&
                      f.status === 'confirmed',
                  ),
                )
                .filter(
                  (f): f is ResearchFindingRead => f !== undefined,
                );
              const supportingDeclaredCount =
                row.supporting_finding_ids.length;
              return (
                <li
                  key={row.finding_id}
                  className="research-answer-v7-analysis"
                  data-testid={`research-answer-v7-analysis-${row.finding_id}`}
                  data-claim-kind={row.claim_kind}
                >
                  <div className="research-answer-v7-analysis-head">
                    <span
                      className="research-answer-v7-analysis-claim-kind"
                      data-testid={`research-answer-v7-claim-kind-${row.finding_id}`}
                    >
                      {claimKindLabel}
                    </span>
                    <span className="research-answer-v7-analysis-title">
                      {row.title}
                    </span>
                  </div>
                  <pre
                    className="research-answer-v7-analysis-description"
                    data-testid={`research-answer-v7-analysis-description-${row.finding_id}`}
                  >
                    {row.description}
                  </pre>
                  {supportingDeclaredCount > 0 ? (
                    <button
                      type="button"
                      className="research-answer-v7-analysis-toggle"
                      onClick={() =>
                        setExpandedAnalysisId(
                          isOpen ? null : row.finding_id,
                        )
                      }
                      aria-expanded={isOpen}
                      data-testid={`research-answer-v7-analysis-toggle-${row.finding_id}`}
                    >
                      {isOpen
                        ? '隐藏支持事实'
                        : `查看支持事实（${supportingFacts.length} / ${supportingDeclaredCount}）`}
                    </button>
                  ) : null}
                  {isOpen &&
                  supportingDeclaredCount > 0 &&
                  supportingFacts.length === 0 ? (
                    <p
                      className="research-answer-v7-analysis-supports-empty muted"
                      data-testid={`research-answer-v7-analysis-supports-empty-${row.finding_id}`}
                    >
                      缺少本次研究的支持事实（{supportingDeclaredCount}），请对照官方原文核实。
                    </p>
                  ) : null}
                  {isOpen && supportingFacts.length > 0 ? (
                    <ul
                      className="research-answer-v7-analysis-supports"
                      data-testid={`research-answer-v7-analysis-supports-${row.finding_id}`}
                    >
                      {supportingFacts.map((fact) => (
                        <li
                          key={fact.id}
                          className="research-answer-v7-analysis-support"
                          data-testid={`research-answer-v7-analysis-support-${row.finding_id}-${fact.id}`}
                        >
                          <p
                            className="research-answer-v7-analysis-support-description"
                            data-testid={`research-answer-v7-analysis-support-description-${row.finding_id}-${fact.id}`}
                          >
                            事实前提：{fact.description}
                          </p>
                          {fact.source_refs && fact.source_refs.length > 0 ? (
                            <ul className="research-answer-v7-analysis-support-sources">
                              {fact.source_refs.map((ref, idx) => (
                                <li
                                  key={`${fact.id}-${ref.provider_source_id}-${idx}`}
                                >
                                  <a
                                    href={ref.url}
                                    target="_blank"
                                    rel="noopener noreferrer nofollow"
                                    data-testid={`research-answer-v7-analysis-support-link-${row.finding_id}-${fact.id}-${idx}`}
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
                  ) : null}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      <p
        className="research-answer-v7-semantic muted"
        data-testid="research-answer-v7-semantic"
      >
        请对照原文核实，未验证内容不作为结论。
      </p>
    </section>
  );
}

export default ResearchAnswer;
