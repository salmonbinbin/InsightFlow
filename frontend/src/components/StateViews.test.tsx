import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { EmptyState, ErrorState, LoadingState, NotMeasured, StatusPill } from './StateViews';

describe('StateViews', () => {
  it('renders LoadingState with aria-busy and live region', () => {
    render(<LoadingState />);
    const busy = document.querySelector('[aria-busy="true"]');
    expect(busy).toBeTruthy();
    expect(busy?.getAttribute('aria-live')).toBe('polite');
    expect(document.querySelector('.state-loading')).toBeTruthy();
  });

  it('renders EmptyState with title and description', () => {
    render(<EmptyState title="没东西" description="导入点数据吧" />);
    expect(screen.getByText('没东西')).toBeTruthy();
    expect(screen.getByText('导入点数据吧')).toBeTruthy();
  });

  it('renders ErrorState with retry button when onRetry is set', () => {
    const onRetry = vi.fn();
    render(<ErrorState message="服务挂了" onRetry={onRetry} />);
    const btn = screen.getByRole('button', { name: '重试' });
    fireEvent.click(btn);
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('renders NotMeasured with a default label', () => {
    render(<NotMeasured />);
    expect(screen.getByText('未测量')).toBeTruthy();
  });

  it('renders StatusPill with the right modifier class', () => {
    render(<StatusPill status="completed" />);
    const pill = document.querySelector('.status-pill--completed');
    expect(pill).toBeTruthy();
  });
});
