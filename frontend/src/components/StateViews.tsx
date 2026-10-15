'use client';

import type { CSSProperties, ReactNode } from 'react';

export interface StateViewsProps {
  className?: string;
  children?: ReactNode;
  style?: CSSProperties;
}

/**
 * Loading skeleton block — keeps the layout stable while data is fetched.
 * No spinners, no bouncing, no fake progress: a real placeholder.
 */
export function LoadingState({ className, children, style }: StateViewsProps) {
  return (
    <div className={`state-loading${className ? ` ${className}` : ''}`} style={style} aria-busy="true" aria-live="polite">
      {children ?? (
        <div className="state-loading-stack">
          <div className="state-loading-bar state-loading-bar--lg" />
          <div className="state-loading-bar" />
          <div className="state-loading-bar state-loading-bar--sm" />
        </div>
      )}
    </div>
  );
}

export interface EmptyStateProps extends StateViewsProps {
  title?: string;
  description?: string;
  action?: ReactNode;
}

export function EmptyState({
  title = '暂无可显示的内容',
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div className={`state-empty empty${className ? ` ${className}` : ''}`}>
      <div className="empty-icon" aria-hidden>∅</div>
      <div className="empty-title">{title}</div>
      {description ? <div className="empty-description">{description}</div> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export interface ErrorStateProps extends StateViewsProps {
  title?: string;
  message?: string;
  onRetry?: () => void;
  details?: string;
}

export function ErrorState({
  title = '加载失败',
  message = '请检查网络连接或稍后重试。',
  onRetry,
  details,
  className,
}: ErrorStateProps) {
  return (
    <div className={`state-error alert alert-danger${className ? ` ${className}` : ''}`} role="alert">
      <div className="state-error-title">{title}</div>
      <div className="state-error-message">{message}</div>
      {details ? <div className="state-error-details">{details}</div> : null}
      {onRetry ? (
        <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

export interface NotMeasuredProps {
  label?: string;
  className?: string;
}

/**
 * Shown when a metric exists in the layout but the underlying measurement has
 * never been collected (no analysis run, no cluster, etc). Distinct from
 * "loading" and "zero" — see Phase 7 v2 prototype review notes on
 * not-measured semantics.
 */
export function NotMeasured({ label = '未测量', className }: NotMeasuredProps) {
  return <span className={`not-measured${className ? ` ${className}` : ''}`}>{label}</span>;
}

export interface StatusPillProps {
  status:
    | 'pending'
    | 'running'
    | 'completed'
    | 'partial_failed'
    | 'failed'
    | 'draft'
    | 'pending_review'
    | 'accepted'
    | 'overridden'
    | 'deferred'
    | 'rejected';
  children?: ReactNode;
  className?: string;
}

export function StatusPill({ status, children, className }: StatusPillProps) {
  const label =
    children ??
    ({
      pending: '等待中',
      running: '运行中',
      completed: '已完成',
      partial_failed: '部分失败',
      failed: '失败',
      draft: '草稿',
      pending_review: '待复核',
      accepted: '已采纳',
      overridden: '已重排',
      deferred: '已延后',
      rejected: '已驳回',
    } as const)[status];
  return (
    <span className={`status-pill status-pill--${status}${className ? ` ${className}` : ''}`}>
      {label}
    </span>
  );
}
