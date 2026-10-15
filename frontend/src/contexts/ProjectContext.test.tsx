import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { ProjectContextProvider, useProjectContext } from './ProjectContext';
import { MockInsightFlowClient } from '../lib/api/mock';
import type { Project } from '../lib/api/types';

const projectA: Project = {
  id: 'proj_a',
  name: 'A',
  product_name: 'Product A',
};
const projectB: Project = {
  id: 'proj_b',
  name: 'B',
  product_name: 'Product B',
};

interface HarnessProps {
  client: MockInsightFlowClient;
}

function Harness({ client }: HarnessProps) {
  const ctx = useProjectContext();
  return (
    <div>
      <span data-testid="active">{ctx.activeProjectId ?? 'none'}</span>
      <span data-testid="count">{ctx.projects.length}</span>
      <button type="button" onClick={() => ctx.selectProject('proj_b')}>
        switch
      </button>
      <button
        type="button"
        onClick={() =>
          void ctx.createProject({ name: 'C', product_name: 'Product C' })
        }
      >
        create
      </button>
    </div>
  );
}

describe('ProjectContext', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('starts with the first project active when none is stored', async () => {
    const client = new MockInsightFlowClient();
    client.projects = [projectA, projectB];
    render(
      <ProjectContextProvider client={client as unknown as never}>
        <Harness client={client} />
      </ProjectContextProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('active').textContent).toBe('proj_a'));
    expect(screen.getByTestId('count').textContent).toBe('2');
  });

  it('switches active project and persists to localStorage', async () => {
    const client = new MockInsightFlowClient();
    client.projects = [projectA, projectB];
    render(
      <ProjectContextProvider client={client as unknown as never}>
        <Harness client={client} />
      </ProjectContextProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('active').textContent).toBe('proj_a'));
    act(() => {
      screen.getByText('switch').click();
    });
    await waitFor(() =>
      expect(screen.getByTestId('active').textContent).toBe('proj_b'),
    );
    expect(window.localStorage.getItem('insightflow:active-project')).toBe('proj_b');
  });

  it('creates a project and immediately activates it', async () => {
    const client = new MockInsightFlowClient();
    client.projects = [projectA];
    render(
      <ProjectContextProvider client={client as unknown as never}>
        <Harness client={client} />
      </ProjectContextProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('active').textContent).toBe('proj_a'));
    await act(async () => {
      screen.getByText('create').click();
    });
    await waitFor(() => expect(screen.getByTestId('count').textContent).toBe('2'));
    await waitFor(() =>
      expect(screen.getByTestId('active').textContent).toBe('proj_2'),
    );
  });
});
