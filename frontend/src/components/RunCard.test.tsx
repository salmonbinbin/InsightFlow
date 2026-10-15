import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RunCard } from './RunCard';

describe('RunCard', () => {
  it('shows counters from real data without a fake progress bar', () => {
    render(
      <RunCard
        title="分类 Run"
        status="completed"
        processedCount={42}
        inputCount={50}
        successCount={40}
        failedCount={2}
        startedAt="2025-10-01T01:00:00Z"
        finishedAt="2025-10-01T01:05:00Z"
      />,
    );
    expect(screen.getByText('分类 Run')).toBeTruthy();
    expect(screen.getByText('40')).toBeTruthy();
    expect(screen.getByText('50')).toBeTruthy();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('renders partial_failed status without a fake progress bar', () => {
    render(
      <RunCard
        title="洞察 Run"
        status="partial_failed"
        processedCount={5}
        inputCount={10}
        successCount={4}
        failedCount={1}
      />,
    );
    expect(screen.getByText('部分失败')).toBeTruthy();
  });

  it('exposes error message on failure', () => {
    render(
      <RunCard
        title="聚类 Run"
        status="failed"
        processedCount={10}
        inputCount={100}
        successCount={5}
        failedCount={5}
        errorMessage="分类数据不完整"
      />,
    );
    expect(screen.getByText('分类数据不完整')).toBeTruthy();
  });
});
