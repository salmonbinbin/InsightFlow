import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ProjectContextProvider, useProjectContext } from '../contexts/ProjectContext';
import { useProjectScopedFetch } from './useProjectScopedFetch';

/**
 * Phase 7 B1 — three properties pinned by this suite:
 *
 *   1. Initial project triggers a usable signal. The OLD ``useRef``
 *      design mutated ``ref.current`` inside an effect and exposed it
 *      via a synchronous read; the consumer's first render always
 *      observed ``signal === null`` and the page got stuck on
 *      LoadingState. The new hook exposes the controller as ``useState``
 *      so the first post-mount render carries the controller and
 *      downstream useEffects actually fire a request.
 *
 *   2. Project switch aborts the previous controller. ``signal.aborted``
 *      must flip to ``true`` synchronously inside the switch handler so
 *      any in-flight fetch rejects rather than landing on the new
 *      project.
 *
 *   3. The NEW signal is a distinct object from the OLD one. Pages that
 *      capture the signal at request time can compare against the live
 *      one to drop late-arriving responses from the previous project.
 */

function makeWrapper(
  initialActive: string | null,
  initialProjects: Array<{ id: string; name: string; product_name: string }> = [
    { id: 'proj_a', name: 'A', product_name: 'Product A' },
    { id: 'proj_b', name: 'B', product_name: 'Product B' },
  ],
) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <ProjectContextProvider
        initialProjects={initialProjects}
        initialActiveProjectId={initialActive}
      >
        {children}
      </ProjectContextProvider>
    );
  };
}

function useSwitchProbe() {
  const ctx = useProjectContext();
  const { signal } = useProjectScopedFetch();
  return { signal, pick: ctx.selectProject, active: ctx.activeProjectId };
}

describe('useProjectScopedFetch', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns a non-null signal on first render when an active project is set', async () => {
    const Wrapper = makeWrapper('proj_a');
    const { result } = renderHook(() => useSwitchProbe(), { wrapper: Wrapper });
    // The state-publishing effect runs after the first render. The
    // second render carries the live controller; the OLD ``useRef``
    // design never reached this state because ref mutations don't
    // trigger renders.
    await waitFor(() => {
      expect(result.current.signal).not.toBeNull();
    });
    expect(result.current.signal?.aborted).toBe(false);
    expect(result.current.active).toBe('proj_a');
  });

  it('returns null when there is no active project', async () => {
    const Wrapper = makeWrapper(null);
    const { result } = renderHook(() => useSwitchProbe(), { wrapper: Wrapper });
    await waitFor(() => {
      expect(result.current.signal).toBeNull();
    });
  });

  it('aborts the previous controller and exposes a fresh signal on switch', async () => {
    const Wrapper = makeWrapper('proj_a');
    const { result } = renderHook(() => useSwitchProbe(), { wrapper: Wrapper });

    await waitFor(() => expect(result.current.signal).not.toBeNull());
    const firstSignal = result.current.signal!;
    expect(firstSignal.aborted).toBe(false);

    act(() => {
      result.current.pick('proj_b');
    });

    await waitFor(() => expect(firstSignal.aborted).toBe(true));
    await waitFor(() => {
      expect(result.current.signal).not.toBeNull();
      expect(result.current.signal).not.toBe(firstSignal);
      expect(result.current.signal?.aborted).toBe(false);
    });
    expect(result.current.active).toBe('proj_b');
  });

  it('aborts every prior signal after multiple switches', async () => {
    const Wrapper = makeWrapper('proj_a');
    const { result } = renderHook(() => useSwitchProbe(), { wrapper: Wrapper });

    await waitFor(() => expect(result.current.signal).not.toBeNull());
    const signalA = result.current.signal!;

    act(() => {
      result.current.pick('proj_b');
    });
    await waitFor(() => expect(signalA.aborted).toBe(true));
    const signalB = result.current.signal!;

    act(() => {
      result.current.pick('proj_a');
    });
    await waitFor(() => expect(signalB.aborted).toBe(true));
    const signalC = result.current.signal!;
    expect(signalC).not.toBe(signalB);
    expect(signalC).not.toBe(signalA);
    expect(signalC.aborted).toBe(false);
    expect(result.current.active).toBe('proj_a');
  });
});