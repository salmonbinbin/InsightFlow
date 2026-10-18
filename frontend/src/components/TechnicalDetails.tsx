import type { ReactNode } from 'react';

/** Keep traceability available without making it the primary reading path. */
export function TechnicalDetails({ children, label = '查看运行详情' }: { children: ReactNode; label?: string }) {
  return (
    <details className="technical-details">
      <summary>{label}</summary>
      <div className="technical-details-body">{children}</div>
    </details>
  );
}
