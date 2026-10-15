'use client';

import { useEffect, useState } from 'react';
import { useProjectContext } from '../contexts/ProjectContext';

/**
 * Data hook helper — returns a per-project AbortSignal that is aborted
 * automatically when the active project changes. Pass ``signal`` into the
 * API client so in-flight fetches never bleed across projects.
 *
 * Usage:
 *   const { signal } = useProjectScopedFetch();
 *   useEffect(() => {
 *     if (!signal) return;
 *     client.listFeedback(activeProject.id, { limit: 25 }, signal).then(...)
 *   }, [activeProject?.id, signal]);
 *
 * The signal is exposed as ``useState`` (not ``useRef``) because the
 * initial render needs to observe the controller right after the
 * provider picks an active project — otherwise the page's own
 * ``useEffect`` reads ``signal === null`` and silently returns without
 * ever firing a request, which is the classic "stuck on LoadingState"
 * regression this hook exists to prevent.
 *
 * Returns ``signal: null`` while no project is active (initial paint
 * with zero projects, or after the user clears selection). On a project
 * switch the previous controller is aborted before a new one is exposed,
 * so any fetch that raced the switch rejects with ``AbortError`` rather
 * than writing to the new project's view.
 */

export interface ProjectScopedSignal {
  signal: AbortSignal | null;
}

export function useProjectScopedFetch(): ProjectScopedSignal {
  const { activeProjectId } = useProjectContext();
  // useState (not useRef) — the controller must trigger a re-render so
  // downstream useEffects that depend on `signal` actually fire on the
  // first commit, not on the second.
  const [controller, setController] = useState<AbortController | null>(null);

  useEffect(() => {
    if (!activeProjectId) {
      // No active project: abort any stray controller and expose null.
      setController((prev) => {
        if (prev && !prev.signal.aborted) prev.abort();
        return null;
      });
      return;
    }
    const next = new AbortController();
    setController((prev) => {
      if (prev && !prev.signal.aborted) prev.abort();
      return next;
    });
    return () => {
      if (!next.signal.aborted) next.abort();
    };
  }, [activeProjectId]);

  return { signal: controller?.signal ?? null };
}