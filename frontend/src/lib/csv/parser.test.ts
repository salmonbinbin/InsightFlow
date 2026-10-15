import { describe, it, expect } from 'vitest';
import {
  parseDelimited,
  parseFeedbackCsv,
  MAX_FILE_SIZE_BYTES,
  MAX_ROW_COUNT,
} from './parser';

const SAMPLE = `feedback_id,content,source,user_id,user_type,created_at
fb_001,登录页面在 Safari 浏览器无法正常显示,support,u_1,customer,2025-09-01T10:00:00Z
fb_002,"导出按钮点击后没有反应, 多次尝试",support,u_2,customer,2025-09-01T11:00:00Z
fb_003,希望增加深色模式,feature_request,u_3,partner,2025-09-02T09:00:00Z
`;

describe('parseDelimited', () => {
  it('splits simple rows', () => {
    const rows = parseDelimited('a,b,c\n1,2,3\n', ',');
    expect(rows).toEqual([
      ['a', 'b', 'c'],
      ['1', '2', '3'],
    ]);
  });

  it('honors quoted fields with commas and newlines', () => {
    const csv = 'a,b\n"x,y","line1\nline2"\n';
    const rows = parseDelimited(csv, ',');
    expect(rows[0]).toEqual(['a', 'b']);
    expect(rows[1]).toEqual(['x,y', 'line1\nline2']);
  });

  it('decodes escaped double-quote pairs', () => {
    const csv = 'a\n"He said ""hi"""\n';
    const rows = parseDelimited(csv, ',');
    expect(rows[1]?.[0]).toBe('He said "hi"');
  });
});

describe('parseFeedbackCsv', () => {
  it('parses a well-formed CSV into rows', async () => {
    const result = await parseFeedbackCsv(SAMPLE);
    expect(result.errors).toEqual([]);
    expect(result.headers).toContain('feedback_id');
    expect(result.rows).toHaveLength(3);
    expect(result.rows[0]?.feedback_id).toBe('fb_001');
    expect(result.rows[1]?.content).toBe('导出按钮点击后没有反应, 多次尝试');
    expect(result.rows[2]?.source).toBe('feature_request');
  });

  it('flags a missing required header', async () => {
    const result = await parseFeedbackCsv('id,content\nx,y\n');
    expect(result.errors.some((e) => e.type === 'header')).toBe(true);
  });

  it('flags rows missing required fields', async () => {
    const result = await parseFeedbackCsv(
      'feedback_id,content\n,missing id\nmissing content,\n',
    );
    expect(result.errors.some((e) => e.type === 'row')).toBe(true);
  });

  it('rejects an empty string', async () => {
    const result = await parseFeedbackCsv('');
    expect(result.errors.some((e) => e.type === 'empty')).toBe(true);
  });

  it('strips a UTF-8 BOM', async () => {
    const result = await parseFeedbackCsv('﻿feedback_id,content\nfb_x,hello\n');
    expect(result.errors).toEqual([]);
    expect(result.rows[0]?.feedback_id).toBe('fb_x');
  });

  it('exposes contract limits', () => {
    expect(MAX_FILE_SIZE_BYTES).toBe(10 * 1024 * 1024);
    expect(MAX_ROW_COUNT).toBe(10000);
  });
});
