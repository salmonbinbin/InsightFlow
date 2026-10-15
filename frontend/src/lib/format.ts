/**
 * Small shared helpers used across UI components. Keep them pure and side
 * effect free so they remain cheap to import in tests.
 */

import type { AnalysisStatus, Sentiment } from './api/types';

export function classifyError(err: { status: number; message: string } | null): string {
  if (!err) return '请求失败，请稍后再试。';
  if (err.status === 0) {
    return '无法连接到服务，请确认后端进程或网络代理。';
  }
  if (err.status >= 500) {
    return '服务暂不可用，请稍后再试。';
  }
  return err.message || '请求失败。';
}

export function isTerminalAnalysisStatus(status: AnalysisStatus): boolean {
  return (
    status === 'completed' ||
    status === 'failed' ||
    status === 'partial_failed'
  );
}

export function isPendingAnalysisStatus(status: AnalysisStatus): boolean {
  return status === 'pending' || status === 'running';
}

/**
 * The backend's run.status field is an open string (the database enum may
 * grow over time). The UI only knows about the five canonical RunStatus
 * values; any unknown value is treated as "running" so the user can still
 * see the row rather than seeing a blank badge.
 */
export function normaliseRunStatus(
  status: string,
): 'pending' | 'running' | 'completed' | 'partial_failed' | 'failed' {
  if (
    status === 'pending' ||
    status === 'running' ||
    status === 'completed' ||
    status === 'partial_failed' ||
    status === 'failed'
  ) {
    return status;
  }
  return 'running';
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  if (!Number.isFinite(value)) return '—';
  if (Number.isInteger(value)) return value.toLocaleString('en-US');
  return value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  return `${(value * 100).toFixed(1)}%`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  const diffSec = (Date.now() - d.getTime()) / 1000;
  if (diffSec < 60) return '刚刚';
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 604800) return `${Math.floor(diffSec / 86400)} 天前`;
  return formatDateTime(value);
}

export function sentimentLabel(s: Sentiment): string {
  switch (s) {
    case 'positive':
      return '正向';
    case 'neutral':
      return '中性';
    case 'negative':
      return '负向';
    default:
      return s;
  }
}

export function priorityLabel(p: 'P0' | 'P1' | 'P2' | 'P3'): string {
  switch (p) {
    case 'P0':
      return '紧急';
    case 'P1':
      return '重要';
    case 'P2':
      return '一般';
    case 'P3':
      return '观察';
    default:
      return p;
  }
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

export function unique<T, K extends string | number>(items: T[], key: (t: T) => K): T[] {
  const seen = new Set<K>();
  const result: T[] = [];
  for (const item of items) {
    const k = key(item);
    if (!seen.has(k)) {
      seen.add(k);
      result.push(item);
    }
  }
  return result;
}
