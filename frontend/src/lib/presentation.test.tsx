import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TechnicalDetails } from '../components/TechnicalDetails';
import { displayLabel, metricLabel, sectionLabel, userMessage, warningLabel, scoreExplanation, topicLabel, metricNote, readableSystemText, documentText } from './presentation';

describe('面向用户的信息展示', () => {
  it('keeps technical detail available but collapsed by default', () => {
    render(<TechnicalDetails><code>source-record-123</code></TechnicalDetails>);
    const summary = screen.getByText('查看运行详情');
    expect(summary.closest('details')?.open).toBe(false);
    expect(screen.getByText('source-record-123')).toBeTruthy();
  });

  it('does not turn unknown or missing states into success', () => {
    expect(displayLabel('approved')).toBe('已批准');
    expect(displayLabel('future_status')).toBe('待确认');
    expect(displayLabel(null)).toBe('—');
    expect(metricLabel('intent_exact_match')).toBe('意图匹配率');
  });

  it('keeps useful validation guidance without exposing implementation paths', () => {
    expect(userMessage('evidence_ids 引用未在允许的来源内')).toBe('反馈编号 引用未在允许的来源内');
    expect(userMessage('Traceback /Users/private/service.py')).not.toMatch(/Traceback|\/Users/);
    expect(userMessage('内容已更新，请刷新后重试。')).toContain('刷新');
  });

  it('identifies review sections without implying verified content', () => {
    expect(sectionLabel('risks[2]')).toBe('风险（第3项）');
    expect(warningLabel('speculative_risk')).toContain('推测');
    expect(sectionLabel('new_internal_field')).toBe('相关内容');
  });

  it('blocks diagnostics even when an error includes Chinese text', () => {
    for (const message of [
      "执行失败：effective_weights={'user_coverage': '0.3035714285714285714'}",
      '服务失败：ConnectionError at backend/app/services/client.py:42',
      '保存失败：internal_error=provider_failure',
      '请求失败：{"project_id":"internal-project","status":"failed"}',
      '请求失败：{"external_id":"internal-row"}',
      '执行失败：score=3.89 | pending=True',
    ]) {
      expect(userMessage(message)).not.toMatch(/effective_weights|user_coverage|ConnectionError|backend\/|internal_error|provider_failure|project_id|internal-row|score=|pending=/);
    }
  });

  it('extracts scorer prose without recomputing or changing the stored explanation', () => {
    const raw = "calculation_version=opportunity-score-v1 | effective_weights={'severity': '0.3035714285714285714'} | total_score=3.89 | user_value_reasoning=2条PDF上传反馈（pdf_upload），涉及>20MB文件，原因尚未验证。";
    expect(scoreExplanation(raw)).toBe('2条PDF上传反馈，涉及>20MB文件，原因尚未验证。');
    expect(raw).toContain('effective_weights=');
    expect(scoreExplanation('证据不足，不能判断影响范围。')).toBe('证据不足，不能判断影响范围。');
  });

  it('does not expose incomplete or nested serialized explanations', () => {
    expect(scoreExplanation('calculation_version=v2 | feedback_count=2')).not.toMatch(/calculation_version|feedback_count/);
    expect(scoreExplanation('calculation_version=v2 | user_value_reasoning=执行失败：payload={"id":"x"}')).not.toMatch(/payload|"id"/);
    expect(scoreExplanation(null)).toContain('暂无可读的评分说明');
  });

  it('shows business topic names instead of model storage tags', () => {
    expect(topicLabel('pdf_upload')).toBe('PDF 上传');
    expect(topicLabel('image_upload')).toBe('图片上传');
    expect(topicLabel('new_internal_tag')).toBe('待整理');
    expect(displayLabel('mixed')).toBe('混合');
    expect(readableSystemText('unknown', '尚待确认')).toBe('尚待确认');
    expect(readableSystemText('0', '尚待确认')).toBe('0');
    const note = '将 module=pdf_upload 且反馈涉及上传失败的条目纳入追踪。';
    expect(metricNote(note)).toBe('将 主题为「PDF 上传」 且反馈涉及上传失败的条目纳入追踪。');
    expect(note).toContain('module=pdf_upload');
  });
  it('translates stored topic names inside authored prose without dropping facts', () => {
    const text = '来源为 U002（F004），模块归属均为 pdf_upload，反馈类型均为 bug。20MB 是用户原话，尚待核实。';
    expect(documentText(text)).toBe('来源为 U002（F004），模块归属均为 PDF 上传，反馈类型均为 问题反馈。20MB 是用户原话，尚待核实。');
    expect(text).toContain('pdf_upload');
    expect(documentText('PDF 调试说明：bug 尚待定位；https://example.com/pdf_upload')).toBe('PDF 调试说明：bug 尚待定位；https://example.com/pdf_upload');
  });

});
