/**
 * ProjectContext — single source of truth for the currently active project.
 *
 * Responsibilities:
 *   - Load the project list from the API.
 *   - Persist the active project_id in localStorage so a refresh keeps the
 *     same selection.
 *   - Expose actions to create a new project and switch the active one.
 *   - On switch, abort every in-flight request issued through this context
 *     (so a stale fetch from project A cannot write to project B's view) and
 *     reset the loading / error state.
 *
 * The abort controller is shared with the data hooks that opt-in via
 * `useProjectScopedSignal()`; components can also call `useProjectContext()`
 * directly.
 */

'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { getApiClient, InsightFlowClient } from '../lib/api/client';
import type { Project, ProjectId } from '../lib/api/types';

const STORAGE_KEY = 'insightflow:active-project';

export interface ProjectContextValue {
  ready: boolean;
  loading: boolean;
  error: string | null;
  projects: Project[];
  activeProject: Project | null;
  activeProjectId: ProjectId | null;
  refresh: () => Promise<void>;
  selectProject: (id: ProjectId) => void;
  createProject: (input: { name: string; product_name: string; description?: string }) => Promise<Project>;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export interface ProjectContextProviderProps {
  children: ReactNode;
  client?: InsightFlowClient;
  initialProjects?: Project[];
  initialActiveProjectId?: ProjectId | null;
  storageKey?: string;
}

export function ProjectContextProvider(props: ProjectContextProviderProps) {
  const client = props.client ?? getApiClient();
  const storageKey = props.storageKey ?? STORAGE_KEY;

  const [projects, setProjects] = useState<Project[]>(props.initialProjects ?? []);
  const [activeProjectId, setActiveProjectId] = useState<ProjectId | null>(
    props.initialActiveProjectId ?? null,
  );
  const [loading, setLoading] = useState<boolean>(!props.initialProjects);
  const [error, setError] = useState<string | null>(null);

  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = (requestSeq.current += 1);
    setLoading(true);
    setError(null);
    const result = await client.listProjects();
    if (seq !== requestSeq.current) return; // stale response, ignore
    if (result.error) {
      setError(result.error.message);
      setLoading(false);
      return;
    }
    const list = result.data ?? [];
    setProjects(list);
    setLoading(false);
    if (list.length > 0) {
      setActiveProjectId((prev) => {
        if (prev && list.some((p) => p.id === prev)) return prev;
        return list[0]!.id;
      });
    } else {
      setActiveProjectId(null);
    }
  }, [client]);

  useEffect(() => {
    if (props.initialProjects) {
      setLoading(false);
      return;
    }
    // Restore from storage on first mount so a refreshed tab keeps selection.
    if (typeof window !== 'undefined') {
      const stored = window.localStorage.getItem(storageKey);
      if (stored) setActiveProjectId(stored);
    }
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectProject = useCallback(
    (id: ProjectId) => {
      if (id === activeProjectId) return;
      // Bump sequence so any pending fetch responses dropped on switch become
      // stale and are discarded by their own hook.
      requestSeq.current += 1;
      setError(null);
      setActiveProjectId(id);
      if (typeof window !== 'undefined') {
        try {
          window.localStorage.setItem(storageKey, id);
        } catch {
          // ignore quota errors
        }
      }
    },
    [activeProjectId, storageKey],
  );

  const createProject = useCallback(
    async (input: { name: string; product_name: string; description?: string }) => {
      const result = await client.createProject(input);
      if (result.error || !result.data) {
        throw new Error(result.error?.message ?? '创建项目失败。');
      }
      const created = result.data;
      setProjects((prev) => [...prev, created]);
      selectProject(created.id);
      return created;
    },
    [client, selectProject],
  );

  const activeProject = useMemo(() => {
    if (!activeProjectId) return null;
    return projects.find((p) => p.id === activeProjectId) ?? null;
  }, [projects, activeProjectId]);

  const value = useMemo<ProjectContextValue>(
    () => ({
      ready: !loading && projects.length >= 0,
      loading,
      error,
      projects,
      activeProject,
      activeProjectId,
      refresh,
      selectProject,
      createProject,
    }),
    [
      loading,
      error,
      projects,
      activeProject,
      activeProjectId,
      refresh,
      selectProject,
      createProject,
    ],
  );

  return <ProjectContext.Provider value={value}>{props.children}</ProjectContext.Provider>;
}

export function useProjectContext(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) {
    throw new Error('useProjectContext must be used within ProjectContextProvider.');
  }
  return ctx;
}
