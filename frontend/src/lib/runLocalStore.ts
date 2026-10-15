/**
 * Local-only run reference cache.
 *
 * Backend does not expose "list analysis/clustering/insight runs for a project"
 * endpoints in Phase 7 work package A scope. We refuse to fake that history.
 * Instead we record, for the active tab, a small list of run_ids the user has
 * started or loaded here, plus their last known status. The cache is purely
 * local (sessionStorage) — clearing the tab wipes it, and it is never used
 * to claim data we have not actually fetched.
 */

export type RunKind = 'analysis' | 'clustering' | 'insight';

export interface LocalRunRef {
  runId: string;
  kind: RunKind;
  projectId: string;
  startedAt: string;
  status: 'pending' | 'running' | 'completed' | 'partial_failed' | 'failed';
  parentRunId?: string;
}

const STORAGE_PREFIX = 'insightflow:run-refs:';

function storageKey(projectId: string): string {
  return `${STORAGE_PREFIX}${projectId}`;
}

function readRefs(projectId: string): LocalRunRef[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.sessionStorage.getItem(storageKey(projectId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is LocalRunRef => Boolean(item && typeof item === 'object'));
  } catch {
    return [];
  }
}

function writeRefs(projectId: string, refs: LocalRunRef[]): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(storageKey(projectId), JSON.stringify(refs));
  } catch {
    // ignore quota errors
  }
}

export function recordLocalRun(ref: LocalRunRef): void {
  const refs = readRefs(ref.projectId);
  const next = [
    ref,
    ...refs.filter((r) => !(r.runId === ref.runId && r.kind === ref.kind)),
  ].slice(0, 20);
  writeRefs(ref.projectId, next);
}

export function updateLocalRun(
  projectId: string,
  kind: RunKind,
  runId: string,
  status: LocalRunRef['status'],
): void {
  const refs = readRefs(projectId);
  let touched = false;
  const next = refs.map((r) => {
    if (r.runId === runId && r.kind === kind) {
      touched = true;
      return { ...r, status };
    }
    return r;
  });
  if (touched) writeRefs(projectId, next);
}

export function listLocalRuns(projectId: string): LocalRunRef[] {
  return readRefs(projectId);
}
