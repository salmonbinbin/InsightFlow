'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState, type ReactNode } from 'react';
import { useProjectContext } from '../contexts/ProjectContext';
import { ProjectSwitcher } from './ProjectSwitcher';
import { LoadingState } from './StateViews';

interface NavItem {
  label: string;
  href: string;
  match: (pathname: string | null) => boolean;
}

const NAV_ITEMS: NavItem[] = [
  { label: '概览', href: '/', match: (p) => p === '/' || p === null },
  { label: '反馈列表', href: '/feedback', match: (p) => p?.startsWith('/feedback') ?? false },
  { label: '分析', href: '/analysis', match: (p) => p?.startsWith('/analysis') ?? false },
  { label: '上传 CSV', href: '/upload', match: (p) => p?.startsWith('/upload') ?? false },
  // Phase 7 insights: real List + Detail pages backed by
  // /api/projects/{id}/insights; replaces the deprecated
  // /opportunities placeholder.
  { label: '洞察 / 机会评分', href: '/insights', match: (p) => p?.startsWith('/insights') ?? false },
  { label: '需求', href: '/requirements', match: (p) => p?.startsWith('/requirements') ?? false },
  { label: 'PRD', href: '/prds', match: (p) => p?.startsWith('/prds') ?? false },
  // Evaluation list is read-only — POST /evaluations is owned by
  // the Phase 1 / Phase 2 detail pages because the runner needs
  // the upstream source-run linkage that this list view does not
  // carry.
  { label: '评测', href: '/evaluation', match: (p) => p?.startsWith('/evaluation') ?? false },
];

export interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const pathname = usePathname();
  const { activeProject, loading, error } = useProjectContext();
  const [switcherOpen, setSwitcherOpen] = useState(false);

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="主导航">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            ◆
          </span>
          <span className="brand-name">InsightFlow</span>
        </div>
        <nav className="nav">
          <div className="nav-section-title">MVP</div>
          <ul className="nav-list">
            {NAV_ITEMS.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={`nav-link${item.match(pathname) ? ' is-active' : ''}`}
                  aria-current={item.match(pathname) ? 'page' : undefined}
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <div className="sidebar-footer">
          <span className="sidebar-footer-text">Phase 7 — 前端 MVP</span>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div className="topbar-left">
            <button
              type="button"
              className="project-switcher"
              onClick={() => setSwitcherOpen(true)}
              aria-haspopup="dialog"
            >
              <span className="project-switcher-label">当前项目</span>
              <span className="project-switcher-name">
                {loading && !activeProject ? '加载中…' : activeProject?.name ?? '尚未选择项目'}
              </span>
              <span className="project-switcher-product">
                {activeProject?.product_name ?? '—'}
              </span>
              <span className="project-switcher-caret" aria-hidden>
                ▾
              </span>
            </button>
            {error ? (
              <span className="topbar-warning" role="alert">
                {error}
              </span>
            ) : null}
          </div>
          <div className="topbar-right">
            <span className="topbar-env">Phase 7 · MVP</span>
          </div>
        </header>
        <main className="app-content">
          {loading && !activeProject ? (
            <LoadingState />
          ) : (
            children
          )}
        </main>
      </div>

      <ProjectSwitcher open={switcherOpen} onClose={() => setSwitcherOpen(false)} />
    </div>
  );
}
