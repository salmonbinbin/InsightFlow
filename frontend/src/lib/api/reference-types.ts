export interface ReferenceTemplate {
  project_id: string;
  source_run_id: string;
  input_count: number;
  csv_text: string;
  filename: string;
}
export interface ReferenceReviewPage {
  project_id: string;
  run_id: string;
  stage: string;
  source_input_count: number | null;
  reviewed_count: number;
  total: number;
  items: Array<{
    feedback_id: string;
    reviewer: string;
    expected: { feedback_type: string | null; severity: number | null; cluster_label: string | null };
    actual: { feedback_type?: string; severity?: number; cluster_ids?: string[] } | null;
    passed: boolean | null;
  }>;
}
