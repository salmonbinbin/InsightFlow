/** User-facing labels only; API values and stored content remain unchanged. */
const LABELS: Record<string, string> = {
  draft: '草稿', needs_validation: '待核实', pending_review: '待复核',
  reviewing: '复核中', approved: '已批准', archived: '已归档',
  accepted: '已采纳', overridden: '已调整', rejected: '已驳回', deferred: '已延后',
  pending: '等待中', running: '进行中', completed: '已完成', partial_failed: '部分完成', failed: '失败',
  classification: '反馈分类', clustering: '反馈聚类', insight: '洞察生成',
  analysis: '反馈分析', pipeline: '完整流程', end_to_end: '完整流程',
  praise: '表扬', feature_request: '功能建议', bug_report: '问题反馈', pain_point: '痛点', question: '提问', other: '其他',
  bug: '问题反馈', performance_issue: '性能问题', positive_feedback: '正向反馈',
  representative: '代表反馈', supporting: '支持证据', counter: '反向证据',
  manual: '人工标注', human_reviewed: '已人工复核', human_verified: '已人工复核',
  train: '训练集', validation: '验证集', test: '测试集', all: '全部样本',
  confirmed: '已核对', not_verified: '未验证',
  positive: '正向', neutral: '中性', negative: '负向', mixed: '混合',
  insufficient_window: '数据时间范围不足', pending_pm_input: '待填写业务价值',
  free: '免费用户', paid: '付费用户',
};

/** Applies only to application explanations/diagnostics, never feedback or source quotes. */
function isInternalDiagnostic(value: string): boolean {
  return /(?:\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b|\b[a-z]\w*\s*=|\b(?:Traceback|SQLAlchemy|ConnectionError|TypeError|ValidationError)\b|(?:\/(?:Users|home|private|tmp|var)\/|\/projects\/|\/api\/)|\b(?:backend|frontend|app)\/[\w/.-]+|\b[\w/.-]+\.(?:py|tsx?|jsx?):\d+|[\[{]\s*["'][^"'\r\n]+["']\s*:|```|\b(?:SELECT\b.+\bFROM|INSERT\s+INTO|UPDATE\b.+\bSET)\b)/i.test(value);
}

export function readableSystemText(value: string | null | undefined, fallback: string): string {
  const text = value?.trim();
  return text && !/^(?:unknown|undefined|null|none|n\/a|tbd)$/i.test(text) && !isInternalDiagnostic(text) ? text : fallback;
}

/** The v1 scorer appends the explanation after a deterministic audit prefix. */
export function scoreExplanation(value: string | null | undefined): string {
  const fallback = '暂无可读的评分说明，请结合评分明细和原始反馈判断。';
  let text = value?.trim() ?? '';
  if (/^calculation_version\s*=/.test(text)) {
    const marker = ' | user_value_reasoning=';
    const start = text.indexOf(marker);
    if (start < 0) return fallback;
    text = text.slice(start + marker.length);
  }
  // Parenthesised storage tags add no meaning to the surrounding human explanation.
  text = text.replace(/[（(]\s*[a-z][a-z0-9]*(?:_[a-z0-9]+)+\s*[)）]/gi, '');
  return readableSystemText(text, fallback);
}

export function topicLabel(value: string | null | undefined): string {
  const topics: Record<string, string> = {
    image_upload: '图片上传', pdf_upload: 'PDF 上传', export: '内容导出',
    memory: '偏好与记忆', response_generation: '回答生成', search: '搜索',
    summarization: '内容总结', unknown: '待识别',
  };
  return value ? topics[value] ?? displayLabel(value, '待整理') : '—';
}

/** Display known taxonomy tags in authored prose; preserve URLs and stored drafts. */
export function documentText(value: string): string {
  return value.split(/(https?:\/\/[^\s<>"'）)]+)/g).map((part, index) => {
    if (index % 2) return part;
    return part
      .replace(/\bmodule\s*=\s*([a-z][a-z0-9_]*)/gi, (_, topic: string) => `主题为「${topicLabel(topic)}」`)
      .replace(/\b(?:pdf_upload|image_upload|response_generation|feature_request|bug_report|performance_issue|positive_feedback)\b/g,
        tag => ['pdf_upload', 'image_upload', 'response_generation'].includes(tag) ? topicLabel(tag) : displayLabel(tag))
      .replace(/(反馈类型(?:均)?为\s*)bug\b/g, '$1问题反馈');
  }).join('');
}

export function metricNote(value: string): string {
  return documentText(value);
}

export function displayLabel(value: string | null | undefined, fallback = '待确认'): string {
  if (!value) return '—';
  return LABELS[value] ?? (/\p{Script=Han}/u.test(value) && !isInternalDiagnostic(value) ? value : fallback);
}

export function sourceLabel(value: string): string {
  const sources: Record<string, string> = {
    in_app: '应用内反馈', support: '客服反馈', app_store: '应用商店',
    community: '用户社区', interview: '用户访谈', email: '邮件',
    survey: '用户问卷', web: '网站', wechat: '微信', feishu: '飞书', unknown: '来源待确认',
  };
  return sources[value] ?? value;
}

const METRICS: Record<string, string> = {
  feedback_type_accuracy: '反馈类型准确率', feedback_type_macro_f1: '反馈分类综合得分',
  severity_exact_match: '严重度匹配率', cluster_count: '聚类数量',
  cluster_purity_micro: '整体聚类纯度', cluster_purity_macro: '平均聚类纯度',
  cluster_coverage: '聚类覆盖率', cluster_fragmentation_mean: '平均主题分散程度',
  cluster_over_merge_ratio: '过度合并占比', evidence_existence_rate: '证据引用存在率',
  pipeline_failure_stage_count: '流程失败阶段数', latency_p50_ms: '耗时中位数（毫秒）',
  latency_p95_ms: '耗时第95百分位（毫秒）', latency_mean_ms: '平均耗时（毫秒）',
  token_usage_total: '模型用量', cost_estimate_usd: '预估费用（美元）',
  type_accuracy: '类型准确率', type_match: '类型匹配率', type_exact_match: '类型匹配率',
  macro_f1: '分类综合得分', type_macro_f1: '分类综合得分',
  sentiment_accuracy: '情感准确率', sentiment_exact_match: '情感匹配率',
  severity_mae: '严重度平均误差', intent_exact_match: '意图匹配率',
  pipeline_success_rate: '流程成功率', schema_valid_rate: '格式有效率',
  evidence_validity: '证据有效率', evidence_validity_rate: '证据有效率',
  groundedness: '证据支持程度', groundedness_rate: '证据支持率',
  hallucination_rate: '无依据内容占比', insight_validity: '洞察有效率',
};

export function metricLabel(value: string): string {
  return METRICS[value] ?? displayLabel(value, '其他指标');
}

export function warningLabel(code: string): string {
  const kind = code.toLowerCase();
  if (kind.includes('speculative')) return '包含推测内容，请在批准前核对。';
  if (kind.includes('missing') || kind.includes('unknown')) return '部分内容缺少可核对的来源，请补充证据。';
  return '部分内容需要复核，请对照原始反馈确认。';
}

export function sectionLabel(value: string): string {
  const labels: Record<string, string> = { background: '背景', problem: '用户问题', target_user: '目标用户', user_story: '用户故事', product_goal: '产品目标', proposed_solution: '建议方案', functional_requirements: '功能需求', user_flow: '用户流程', edge_cases: '边界情况', acceptance_criteria: '验收标准', success_metrics: '成功指标', risks: '风险', mvp_scope: '首版范围', evidence_sources: '资料来源', evidence_references: '引用反馈' };
  const match = /^(\w+)(?:\[(\d+)\])?/.exec(value);
  const key = match?.[1];
  if (!match || !key || !labels[key]) return '相关内容';
  return labels[key] + (match[2] !== undefined ? `（第${Number(match[2]) + 1}项）` : '');
}

export function userMessage(message: string): string {
  if (!message) return '暂时无法完成此操作，请稍后重试。';
  const readable = message
    .replace(/evidence_ids|external_id/g, '反馈编号')
    .replace(/success_metrics/g, '成功指标')
    .replace(/target_user/g, '目标用户')
    .replace(/user_story/g, '用户故事')
    .replace(/feedback_id/g, '反馈编号')
    .replace(/user_id/g, '用户编号')
    .replace(/created_at/g, '反馈时间')
    .replace(/\bcontent\b/g, '反馈内容');
  if (!/\p{Script=Han}/u.test(message) || isInternalDiagnostic(readable) || /(?:HTTP\s*\d|schema_)/i.test(readable)) {
    return '暂时无法完成此操作，请检查填写内容后重试。';
  }
  return readable;
}
