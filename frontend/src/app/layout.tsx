import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { ProjectContextProvider } from '../contexts/ProjectContext';
import { AppShell } from '../components/AppShell';
import '../styles/globals.css';

export const metadata: Metadata = {
  title: 'InsightFlow — 反馈到产品的中转站',
  description:
    '从用户反馈出发，识别高频痛点、形成产品机会、并产出可执行的 PRD。',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <ProjectContextProvider>
          <AppShell>{children}</AppShell>
        </ProjectContextProvider>
      </body>
    </html>
  );
}
