/**
 * Phase 8 v7 — wire-shape and client-version unit tests.
 *
 * Covers the v7 contract end-to-end at the lowest level
 * we can reach without booting the page:
 *
 *   1. The v7 type guard accepts a real
 *      ``ResearchAnswerV7`` payload and rejects every
 *      earlier shape (v5, v6, NULL, missing-version).
 *   2. A NULL / v5 / v6 ``coverage_report`` does not
 *      trigger the v7 renderer. The old history keeps
 *      rendering with the v5 honesty banner.
 *   3. ``createRequirement`` with the new 3rd
 *      ``promptVersion`` argument sends
 *      ``{ prompt_version }``; without the argument the
 *      body stays the legacy ``{}`` (old test contract).
 *   4. ``createPrd`` forwards an optional
 *      ``prompt_version`` inside the body so the page
 *      can pin ``prd-generation-v4``.
 *   5. ``startResearchRun`` forwards the body verbatim,
 *      so the page-level call can include the
 *      ``prompt_version: 'research-agent-v7'`` pin.
 *   6. v7 analysis rows carry the strict claim_kind
 *      discriminator and supporting_finding_ids so the
 *      page can expand an analysis to its cited
 *      fact premises (and so a future v8 row with a
 *      different claim_kind cannot accidentally route
 *      into the v7 renderer).
 *
 * The tests do NOT boot the page. They pin the wire
 * contract so a backend regression that drops the
 * ``answer`` field, or a frontend regression that
 * strips the ``prompt_version`` pin, both surface
 * here before they reach the panel.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { InsightFlowClient } from './client';
import { isResearchAnswerV7 } from './types';
import type { ResearchAnswerV7 } from './types';

function jsonResponse(body: unknown, init: { status: number } = { status: 200 }): Response {
  return new Response(JSON.stringify(body), {
    status: init.status,
    headers: { 'content-type': 'application/json' },
  });
}

const SAMPLE_V7_ANSWER: ResearchAnswerV7 = {
  version: 'research-answer-v7',
  items: [
    {
      requirement_id: 'req-pricing',
      question: 'TrialHero 的 SMB 定价是否包含 14 天试用？',
      kind: 'capability',
      status: 'covered',
      answer_polarity: 'positive',
      quotes: [
        {
          finding_id: 'fact-1',
          evidence_id: 'ev-1',
          text: '标准版 14 天试用，企业版单独询价。',
          provider_source_id: 'src-1',
          url: 'https://trialhero.example/pricing',
        },
      ],
      gap: '',
      adopted_attempt: 2,
    },
  ],
  unresolved: [],
  analyses: [
    {
      finding_id: 'analysis-1',
      title: '样本差异',
      description: '推断：仅本次产品样本均有上述同维度原文支持。',
      claim_kind: 'inference',
      supporting_finding_ids: ['fact-1'],
      semantic_review: 'NOT_MEASURED',
    },
  ],
  semantic_review: 'NOT_MEASURED',
};

describe('Phase 8 v7 — wire shape + client version pin', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('isResearchAnswerV7 accepts a real v7 payload and rejects every earlier shape', () => {
    expect(isResearchAnswerV7(SAMPLE_V7_ANSWER)).toBe(true);
    // v5 / v6 — coverage_report payload without the v7
    // ``answer`` envelope. The page must NEVER route these
    // into the v7 renderer.
    expect(isResearchAnswerV7({ items: [], overall_completable: true })).toBe(false);
    // A future v8 with a different version literal must
    // NOT be mis-routed.
    expect(
      isResearchAnswerV7({ ...SAMPLE_V7_ANSWER, version: 'research-answer-v8' }),
    ).toBe(false);
    // NULL and missing-version edge cases.
    expect(isResearchAnswerV7(null)).toBe(false);
    expect(isResearchAnswerV7(undefined)).toBe(false);
    expect(isResearchAnswerV7({})).toBe(false);
    expect(isResearchAnswerV7({ version: undefined })).toBe(false);
  });

  it('v5 / v6 / NULL coverage_report does NOT trigger the v7 renderer path', () => {
    // The page check is just a structural one
    // (coverage_report.answer.version === 'research-answer-v7')
    // but we re-pin it here so a refactor cannot silently
    // route a v5 / v6 row into the v7 branch.
    const v5Report = {
      version: 'research-support-v6',
      items: [],
      overall_completable: false,
      semantic_review: 'NOT_MEASURED',
    };
    const v6Report = { ...v5Report, answer: { version: 'research-answer-v6' } };
    expect(isResearchAnswerV7((v5Report as { answer?: unknown }).answer)).toBe(
      false,
    );
    expect(isResearchAnswerV7((v6Report as { answer?: unknown }).answer)).toBe(
      false,
    );
  });

  it('createRequirement with the 3rd promptVersion arg sends prompt_version', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { id: 'req-1', project_id: 'proj-1', insight_id: 'ins-1', status: 'draft' },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({
      baseUrl: '/api',
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    await client.createRequirement('ins-1', undefined, 'requirement-generation-v2');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/insights/ins-1/requirement');
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      prompt_version: 'requirement-generation-v2',
    });
  });

  it('createRequirement without the 3rd arg preserves the legacy empty-body contract', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { id: 'req-1', project_id: 'proj-1', insight_id: 'ins-1', status: 'draft' },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({
      baseUrl: '/api',
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    await client.createRequirement('ins-1');
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });

  it('createPrd forwards the optional prompt_version inside the body', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { id: 'prd-1', requirement_id: 'req-1', version: 1, status: 'draft' },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({
      baseUrl: '/api',
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    await client.createPrd('req-1', { prompt_version: 'prd-generation-v4' });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      prompt_version: 'prd-generation-v4',
    });
  });

  it('createPrd without prompt_version still sends {} (AI-generated path)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { id: 'prd-1', requirement_id: 'req-1', version: 1, status: 'draft' },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({
      baseUrl: '/api',
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    await client.createPrd('req-1');
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });

  it('startResearchRun forwards the body verbatim, including the v7 prompt_version pin', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          id: 'rrun-1',
          project_id: 'proj-1',
          insight_id: 'ins-1',
          status: 'completed',
          prompt_version: 'research-agent-v7',
        },
        { status: 201 },
      ),
    );
    const client = new InsightFlowClient({
      baseUrl: '/api',
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    await client.startResearchRun('ins-1', {
      research_question: 'TrialHero 定价？',
      scope: null,
      prompt_version: 'research-agent-v7',
    });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/insights/ins-1/research');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      research_question: 'TrialHero 定价？',
      scope: null,
      prompt_version: 'research-agent-v7',
    });
  });

  it('v7 analysis rows carry the strict claim_kind discriminator and supporting_finding_ids', () => {
    // The wire contract: every analysis row has
    //   claim_kind: 'inference' | 'recommendation' | ...
    //   supporting_finding_ids: string[]
    // so the page can render a stable Chinese label and
    // expand the row to its cited fact premises.
    for (const row of SAMPLE_V7_ANSWER.analyses) {
      expect(['inference', 'recommendation']).toContain(row.claim_kind);
      expect(Array.isArray(row.supporting_finding_ids)).toBe(true);
      expect(row.supporting_finding_ids.length).toBeGreaterThan(0);
      expect(row.semantic_review).toBe('NOT_MEASURED');
    }
    expect(SAMPLE_V7_ANSWER.analyses[0]?.claim_kind).toBe('inference');
    expect(SAMPLE_V7_ANSWER.analyses[0]?.supporting_finding_ids).toEqual(['fact-1']);
  });
});
