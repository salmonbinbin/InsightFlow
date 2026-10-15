'use client';

import type { ReactNode } from 'react';
import { StatusPill } from './StateViews';
import { formatDateTime, formatNumber } from '../lib/format';

export interface RunCardProps {
  title: string;
  description?: string;
  status: 'pending' | 'running' | 'completed' | 'partial_failed' | 'failed';
  startedAt?: string | null;
  finishedAt?: string | null;
  processedCount?: number | null;
  inputCount?: number | null;
  successCount?: number | null;
  failedCount?: number | null;
  extraCounters?: Array<{ label: string; value: ReactNode }>;
  runningLabel?: string;
  pendingLabel?: string;
  completedLabel?: string;
  partialFailedLabel?: string;
  failedLabel?: string;
  actions?: ReactNode;
  meta?: ReactNode;
  errorMessage?: string | null;
}

/**
 * Visual card for a long-running operation. Critical: backend analysis POSTs
 * are currently SYNCHRONOUS in InsightFlow, so we never fake a progress bar
 * here. The counters shown come from the actual AnalysisRun / ClusterRun /
 * InsightRun payload (the property names mirror the backend schema
 * `processed_count`, `input_count`, `success_count`, `failed_count`,
 * `started_at`, `completed_at`, `error_message`).
 */
export function RunCard(props: RunCardProps) {
  const {
    title,
    description,
    status,
    startedAt,
    finishedAt,
    processedCount,
    inputCount,
    successCount,
    failedCount,
    extraCounters,
    runningLabel = '正在执行',
    pendingLabel = '排队中',
    completedLabel = '执行完成',
    partialFailedLabel = '部分失败',
    failedLabel = '执行失败',
    actions,
    meta,
    errorMessage,
  } = props;

  const statusLabel =
    status === 'running'
      ? runningLabel
      : status === 'pending'
        ? pendingLabel
        : status === 'completed'
          ? completedLabel
          : status === 'partial_failed'
            ? partialFailedLabel
            : failedLabel;

  return (
    <section className="run-card" data-status={status}>
      <header className="run-card-header">
        <div className="run-card-heading">
          <h3 className="run-card-title">{title}</h3>
          {description ? <p className="run-card-description">{description}</p> : null}
        </div>
        <div className="run-card-status">
          <StatusPill status={status}>{statusLabel}</StatusPill>
        </div>
      </header>
      <div className="run-card-counters">
        <Counter label="处理" value={formatNumber(processedCount ?? null)} />
        <Counter label="输入" value={formatNumber(inputCount ?? null)} />
        <Counter label="成功" value={formatNumber(successCount ?? null)} />
        <Counter label="失败" value={formatNumber(failedCount ?? null)} />
        {extraCounters?.map((c) => (
          <Counter key={c.label} label={c.label} value={c.value} />
        ))}
      </div>
      <footer className="run-card-footer">
        <div className="run-card-meta">
          {startedAt ? <span>开始：{formatDateTime(startedAt)}</span> : null}
          {finishedAt ? <span>结束：{formatDateTime(finishedAt)}</span> : null}
          {!startedAt && !finishedAt ? <span>尚未开始</span> : null}
          {meta}
        </div>
        <div className="run-card-actions">
          {actions}
          {status === 'failed' && errorMessage ? (
            <span className="run-card-error" role="alert">
              {errorMessage}
            </span>
          ) : null}
        </div>
      </footer>
    </section>
  );
}

function Counter({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="run-card-counter">
      <div className="run-card-counter-label">{label}</div>
      <div className="run-card-counter-value">{value}</div>
    </div>
  );
}
