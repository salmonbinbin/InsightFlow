/**
 * ResearchAnswer — isolated component tests.
 *
 * Covers the Phase 8 v7 extractive contract surface:
 *   - the original question is rendered verbatim;
 *   - per-item status / polarity / adopted attempt surface
 *     as Chinese labels;
 *   - the quote uses the saved source text (NOT the model
 *     title and NOT the internal evidence id);
 *   - the gap is rendered through a fixed Chinese label,
 *     not the raw code token;
 *   - the analysis block distinguishes inference vs
 *     recommendation by claim_kind;
 *   - supporting facts are filtered to
 *     same-Run + claim_kind='fact' + status='confirmed';
 *   - a conflicting polarity is rendered as
 *     "程序支持与否定冲突", NEVER as positive;
 *   - the page never claims "已语义复核" — the
 *     manual-semantic-review boundary is preserved.
 *
 * The tests build a synthetic ``ResearchAnswerV7`` /
 * ``ResearchFindingRead`` payload — no real backend, no
 * shared mock client — so a regression in the v7 page
 * cannot be masked by a stale harness.
 */

import { describe, expect, it } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { ResearchAnswer } from './ResearchAnswer';
import type {
  ResearchAnswerV7,
  ResearchFindingRead,
} from '../lib/api/types';

const SAMPLE_FACT: ResearchFindingRead = {
  id: 'fact-1',
  run_id: 'run-1',
  category: 'competitor',
  title: 'Competitor price page',
  description: '公开网页说明：标准版 14 天试用，企业版单独询价。',
  confidence: 0.9,
  status: 'confirmed',
  claim_kind: 'fact',
  source_refs: [
    {
      provider_source_id: 'src-1',
      url: 'https://trialhero.example/pricing',
      title: 'TrialHero Pricing',
    },
  ],
  created_at: '2026-10-18T00:00:00Z',
};

const SAMPLE_FACT_2: ResearchFindingRead = {
  ...SAMPLE_FACT,
  id: 'fact-2',
  description: '另一段官方支持截图。',
  source_refs: [
    {
      provider_source_id: 'src-2',
      url: 'https://trialhero.example/support',
      title: 'TrialHero Support',
    },
  ],
};

const SAMPLE_UNVERIFIED: ResearchFindingRead = {
  ...SAMPLE_FACT,
  id: 'unverified-1',
  status: 'not_verified',
  source_refs: null,
};

const SAMPLE_FOREIGN: ResearchFindingRead = {
  ...SAMPLE_FACT,
  id: 'foreign-1',
  run_id: 'foreign-run',
};

const SAMPLE_ANSWER: ResearchAnswerV7 = {
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
          finding_id: SAMPLE_FACT.id,
          evidence_id: 'ev-1',
          text: '标准版 14 天试用，企业版单独询价。',
          provider_source_id: 'src-1',
          url: 'https://trialhero.example/pricing',
        },
      ],
      gap: '',
      adopted_attempt: 2,
    },
    {
      requirement_id: 'req-refund',
      question: '是否提供自动续费关闭路径？',
      kind: 'procedure',
      status: 'partial',
      answer_polarity: 'conflicting',
      quotes: [
        {
          finding_id: SAMPLE_FACT_2.id,
          evidence_id: 'ev-2',
          text: '账户 → 订阅 → 关闭自动续费。',
          provider_source_id: 'src-2',
          url: 'https://trialhero.example/support',
        },
      ],
      gap: 'procedure',
      adopted_attempt: 1,
    },
    {
      requirement_id: 'req-batch',
      question: '是否支持批量删除？',
      kind: 'capability',
      status: 'not_verified',
      answer_polarity: 'negative',
      quotes: [],
      gap: 'evidence_missing; quantity',
      adopted_attempt: null,
    },
  ],
  unresolved: [
    {
      id: 'u-1',
      text: '原问题中的「离线」条件未在分析中复述。',
      reason: 'condition_omitted',
    },
  ],
  analyses: [
    {
      finding_id: 'analysis-1',
      title: '样本差异',
      description:
        '场景（输入）：SMB 场景\n原文：标准版 14 天试用，企业版单独询价。\n推断：以上是同一研究问题下不同要求的原文对照；不能据此断言效果或优劣。',
      claim_kind: 'inference',
      supporting_finding_ids: [SAMPLE_FACT.id, 'missing-fact'],
      semantic_review: 'NOT_MEASURED',
    },
    {
      finding_id: 'analysis-2',
      title: '待验证机会',
      description:
        '原文：账户 → 订阅 → 关闭自动续费。\n建议（待验证）：在结账页直接展示自动续费关闭按钮；这是测试提案，不是已证实的产品事实。',
      claim_kind: 'recommendation',
      supporting_finding_ids: [SAMPLE_FACT_2.id, SAMPLE_UNVERIFIED.id, SAMPLE_FOREIGN.id],
      semantic_review: 'NOT_MEASURED',
    },
  ],
  semantic_review: 'NOT_MEASURED',
};

describe('Phase 8 v7 ResearchAnswer renderer', () => {
  it('renders the original question and per-item Chinese labels', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2]}
      />,
    );
    expect(
      screen.getByTestId('research-answer-v7-original-question').textContent,
    ).toMatch(/TrialHero 的 SMB 定价是否包含 14 天试用/);
    expect(
      screen.getByTestId('research-answer-v7-item-status-req-pricing').textContent,
    ).toMatch(/已覆盖/);
    expect(
      screen.getByTestId('research-answer-v7-item-polarity-req-pricing').textContent,
    ).toMatch(/有支持原文/);
    expect(
      screen.getByTestId('research-answer-v7-item-attempt-req-pricing').textContent,
    ).toMatch(/采用第 2 轮/);
  });

  it('uses the saved source span as the visible quote text, NOT the model title or evidence id', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2]}
      />,
    );
    const quote = screen.getByTestId('research-answer-v7-quote-text-req-pricing-0');
    // The model-supplied title and the internal evidence id
    // MUST NOT be promoted to the user-visible quote.
    expect(quote.textContent).toMatch(/原文：标准版 14 天试用/);
    expect(quote.textContent).not.toMatch(/Competitor price page/);
    expect(quote.textContent).not.toMatch(/ev-1/);
    const sourceId = screen.getByTestId(
      'research-answer-v7-quote-source-id-req-pricing-0',
    );
    // The provider_source_id IS surfaced (for traceability)
    // but only as a small annotation, not as a title.
    expect(sourceId.textContent).toMatch(/来源编号：src-1/);
    const link = screen.getByTestId('research-answer-v7-quote-link-req-pricing-0');
    expect(link.getAttribute('href')).toBe('https://trialhero.example/pricing');
  });

  it('translates the server gap code into a Chinese label, not the raw token', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2]}
      />,
    );
    const gap = screen.getByTestId('research-answer-v7-gap-req-batch');
    expect(gap.textContent).toMatch(/尚无原文支持/);
    expect(gap.textContent).toMatch(/原文未命中数量/);
    // The raw code must not appear standalone.
    expect(gap.textContent).not.toMatch(/evidence_missing/);
    expect(gap.textContent).not.toMatch(/\bquantity\b/);
  });

  it('renders conflicting polarity as 程序支持与否定冲突, NEVER as positive', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2]}
      />,
    );
    const polarity = screen.getByTestId(
      'research-answer-v7-item-polarity-req-refund',
    );
    expect(polarity.textContent).toMatch(/来源结论冲突/);
    // The conflicting row MUST NOT be mis-labelled.
    expect(polarity.textContent).not.toMatch(/^程序支持$/);
    expect(polarity.getAttribute('data-polarity') ?? '').toBe('conflicting');
  });

  it('keeps the manual-semantic-review boundary honest', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2]}
      />,
    );
    const meta = screen.getByTestId('research-answer-v7-meta').textContent ?? '';
    const semantic = screen.getByTestId('research-answer-v7-semantic').textContent ?? '';
    expect(meta).toMatch(/待人工复核/);
    expect(semantic).toMatch(/未验证内容不作为结论/);
    // The page must NEVER claim 已语义复核 / audited / reviewed
    // for a v7 run.
    expect(meta).not.toMatch(/已语义复核/);
    expect(semantic).not.toMatch(/已语义复核/);
  });

  it('distinguishes inference vs recommendation by claim_kind', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2]}
      />,
    );
    expect(
      screen.getByTestId('research-answer-v7-claim-kind-analysis-1').textContent,
    ).toMatch(/推断（基于事实前提）/);
    expect(
      screen.getByTestId('research-answer-v7-claim-kind-analysis-2').textContent,
    ).toMatch(/建议（待验证）/);
  });

  it('filters supporting facts to same-Run + fact + confirmed', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2, SAMPLE_UNVERIFIED, SAMPLE_FOREIGN]}
      />,
    );
    // The toggle is always rendered when supporting_finding_ids
    // is non-empty; the count label reflects the filtered set
    // over the declared count (declared ids are 2: ``fact-1``
    // + the missing one — we filter to ``fact-1`` only, the
    // missing id is silently dropped, and the foreign /
    // unverified ids are also gated out).
    const toggle = screen.getByTestId(
      'research-answer-v7-analysis-toggle-analysis-1',
    );
    expect(toggle.textContent).toMatch(/查看支持事实（1 \/ 2）/);
    // The missing-fact id is silently dropped.
    fireEvent.click(toggle);
    const supports = screen.getByTestId(
      'research-answer-v7-analysis-supports-analysis-1',
    );
    expect(supports.textContent).toMatch(/标准版 14 天试用/);
    expect(supports.textContent).not.toMatch(/missing-fact/);
  });

  it('drops unverified and foreign facts from the analysis support list', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2, SAMPLE_UNVERIFIED, SAMPLE_FOREIGN]}
      />,
    );
    const toggle = screen.getByTestId(
      'research-answer-v7-analysis-toggle-analysis-2',
    );
    // analysis-2 has 3 declared supports but only SAMPLE_FACT_2
    // is confirmed + same Run + claim_kind='fact'.
    expect(toggle.textContent).toMatch(/查看支持事实（1 \/ 3）/);
    fireEvent.click(toggle);
    const supports = within(
      screen.getByTestId(
        'research-answer-v7-analysis-supports-analysis-2',
      ),
    );
    // SAMPLE_FACT_2 — kept.
    expect(
      supports.getByTestId(
        'research-answer-v7-analysis-support-analysis-2-fact-2',
      ).textContent,
    ).toMatch(/另一段官方支持截图/);
    // SAMPLE_UNVERIFIED — dropped.
    expect(
      supports.queryByTestId(
        'research-answer-v7-analysis-support-analysis-2-unverified-1',
      ),
    ).toBeNull();
    // SAMPLE_FOREIGN — dropped.
    expect(
      supports.queryByTestId(
        'research-answer-v7-analysis-support-analysis-2-foreign-1',
      ),
    ).toBeNull();
  });

  it('surfaces the unresolved clause verbatim so the PM can audit the policy', () => {
    render(
      <ResearchAnswer
        runId="run-1"
        answer={SAMPLE_ANSWER}
        findings={[SAMPLE_FACT, SAMPLE_FACT_2]}
      />,
    );
    const unresolved = screen.getByTestId('research-answer-v7-unresolved');
    expect(unresolved.textContent).toMatch(/「离线」条件未在分析中复述/);
    // The raw reason code must NOT appear in the DOM —
    // the page maps it through a fixed Chinese label.
    expect(unresolved.textContent).not.toMatch(/condition_omitted/);
  });
});
