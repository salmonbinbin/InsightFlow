'use client';

import { useEffect, useRef } from 'react';
import { NotMeasured, StatusPill } from './StateViews';
import {
  formatDateTime,
  formatPercent,
  sentimentLabel,
} from '../lib/format';
import type {
  Feedback,
  FeedbackAnalysisRead,
  FeedbackAnalysisSelection,
  FeedbackType,
  Sentiment,
} from '../lib/api/types';

export interface FeedbackDrawerProps {
  open: boolean;
  onClose: () => void;
  feedback: Feedback | null;
  loading?: boolean | null;
  /**
   * Full ``FeedbackAnalysisRead`` or ``null`` when the chosen run
   * has no analysis row for this feedback. The drawer never
   * falls back to a foreign run — the page already validated
   * ``analysis.feedback_id === drawer.feedback.id`` before
   * passing the value down.
   */
  analysis?: FeedbackAnalysisRead | null;
  /**
   * Selection descriptor that the page received from the
   * backend. ``null`` only when the fetch failed or has not
   * returned yet (the drawer renders the N/A branch in that
   * case). ``mode``/``selected_run_id``/``selection_rule`` are
   * always surfaced verbatim so the user can audit how the
   * row was picked.
   */
  selection?: FeedbackAnalysisSelection | null;
  error?: string | null;
  onRetry?: () => void;
}

const FEEDBACK_TYPE_LABELS: Record<FeedbackType, string> = {
  praise: '表扬',
  feature_request: '功能请求',
  bug_report: '缺陷报告',
  pain_point: '痛点',
  question: '提问',
  other: '其他',
};

const SELECTION_MODE_LABELS: Record<FeedbackAnalysisSelection['mode'], string> = {
  latest: '最新分类 Run',
  explicit: '指定 Run',
};

/* ``sentimentLabel`` accepts the strict ``Sentiment`` literal; the
 * runner currently emits the three known values, but the read DTO
 * is intentionally an open ``string`` for forward-compat. The
 * helper type-guards so we route known values through the existing
 * label table and surface the raw string otherwise. */
function renderSentiment(s: string): string {
  if (s === 'positive' || s === 'neutral' || s === 'negative') {
    return sentimentLabel(s as Sentiment);
  }
  return s;
}

/**
 * Side drawer rendering one feedback record.
 *
 * The drawer is split into two read-only sections:
 *   1. "原始反馈 (Raw)" — always shown, sourced verbatim from
 *      the ``Feedback`` record. The page guarantees this value
 *      is the latest loaded feedback row and never an in-flight
 *      stale response (project switch + myFeedbackId race
 *      guard).
 *   2. "AI 分析" — only the analysis row from the chosen run is
 *      shown. When ``analysis === null`` the drawer renders an
 *      explicit "AI 分析 N/A" badge so a missing snapshot is
 *      never confused with a slow fetch.
 *
 * Accessibility: ``Escape`` closes the drawer; on open the
 * close button is auto-focused so keyboard users can dismiss
 * without hunting for the trigger. The page is responsible for
 * restoring focus to the row that opened the drawer; the
 * drawer only handles the focus-in side of the contract.
 */
export function FeedbackDrawer({
  open,
  onClose,
  feedback,
  loading,
  analysis,
  selection,
  error,
  onRetry,
}: FeedbackDrawerProps) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    }
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  // Focus the close button when the drawer opens so keyboard
  // users land somewhere usable. The page restores focus to
  // the row that triggered the open on close.
  useEffect(() => {
    if (!open) return;
    const btn = closeButtonRef.current;
    if (btn) btn.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div className="drawer-overlay" role="presentation" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label="反馈详情"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="drawer-header">
          <div>
            <div className="eyebrow">反馈详情</div>
            <h2 className="drawer-title">{feedback?.external_id ?? '—'}</h2>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            aria-label="关闭反馈详情"
          >
            关闭
          </button>
        </header>
        <div className="drawer-body">
          {loading ? (
            <div className="state-loading">正在加载反馈详情…</div>
          ) : error ? (
            <div className="alert alert-danger" role="alert">
              <div>{error}</div>
              {onRetry ? (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={onRetry}
                >
                  重试
                </button>
              ) : null}
            </div>
          ) : !feedback ? (
            <div className="state-empty">未选择反馈。</div>
          ) : (
            <>
              <section className="drawer-section">
                <h3 className="drawer-section-title">原始反馈</h3>
                <dl className="drawer-meta">
                  <dt>来源</dt>
                  <dd>{feedback.source || <NotMeasured />}</dd>
                  <dt>用户 ID</dt>
                  <dd>{feedback.user_id || <NotMeasured />}</dd>
                  <dt>用户类型</dt>
                  <dd>{feedback.user_type || <NotMeasured />}</dd>
                  <dt>原始时间</dt>
                  <dd>{formatDateTime(feedback.feedback_created_at ?? null)}</dd>
                  <dt>入库时间</dt>
                  <dd>{formatDateTime(feedback.created_at ?? null)}</dd>
                </dl>
                <div className="drawer-content">{feedback.content}</div>
              </section>

              <section className="drawer-section">
                <h3 className="drawer-section-title">AI 分析</h3>
                {analysis ? (
                  <>
                    <dl className="drawer-meta">
                      <dt>反馈类型</dt>
                      <dd>
                        {FEEDBACK_TYPE_LABELS[
                          analysis.feedback_type as FeedbackType
                        ] ?? analysis.feedback_type}
                      </dd>
                      <dt>主题 / 模块</dt>
                      <dd>{analysis.module || <NotMeasured />}</dd>
                      <dt>意图 (intent)</dt>
                      <dd>{analysis.intent || <NotMeasured />}</dd>
                      <dt>痛点 (pain_point)</dt>
                      <dd>
                        {analysis.pain_point ? (
                          analysis.pain_point
                        ) : (
                          <NotMeasured />
                        )}
                      </dd>
                      <dt>情感</dt>
                      <dd>{renderSentiment(analysis.sentiment)}</dd>
                      <dt>严重度</dt>
                      <dd>{analysis.severity} / 5</dd>
                      <dt>置信度</dt>
                      <dd>{formatPercent(analysis.confidence)}</dd>
                      <dt>语义文本</dt>
                      <dd>
                        {analysis.semantic_text ? (
                          analysis.semantic_text
                        ) : (
                          <NotMeasured />
                        )}
                      </dd>
                      <dt>来源 Run ID</dt>
                      <dd>
                        <code className="mono">{analysis.run_id}</code>
                      </dd>
                      <dt>分析时间</dt>
                      <dd>{formatDateTime(analysis.created_at)}</dd>
                    </dl>
                  </>
                ) : (
                  <div className="drawer-empty">
                    <p>
                      AI 分析 N/A — 该反馈在所选 Run 下没有分类结果。
                    </p>
                    <NotMeasured label="尚无 AI 分析" />
                  </div>
                )}
                {selection ? (
                  <div className="drawer-meta-inline drawer-meta-inline--block">
                    <span>选择模式</span>
                    <span>{SELECTION_MODE_LABELS[selection.mode]}</span>
                  </div>
                ) : null}
                {selection ? (
                  <div className="drawer-meta-inline drawer-meta-inline--block">
                    <span>选定 Run ID</span>
                    <span>
                      {selection.selected_run_id ? (
                        <code className="mono">{selection.selected_run_id}</code>
                      ) : (
                        <NotMeasured />
                      )}
                    </span>
                  </div>
                ) : null}
                {selection ? (
                  <p className="drawer-note drawer-note--mono">
                    {selection.selection_rule}
                  </p>
                ) : null}
              </section>

              <section className="drawer-section">
                <h3 className="drawer-section-title">流转</h3>
                <div className="drawer-meta-inline">
                  <span>当前状态</span>
                  <StatusPill status="pending_review">尚未流转</StatusPill>
                </div>
                <p className="drawer-note">
                  后续工作包（B/C）将接入机会评分、决策、需求与 PRD。当前阶段仅呈现最基础流转位。
                </p>
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
