'use client';

import { useCallback, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import Link from 'next/link';
import { useProjectContext } from '../../contexts/ProjectContext';
import { getApiClient } from '../../lib/api/client';
import { useProjectScopedFetch } from '../../lib/useProjectScopedFetch';
import { EmptyState, ErrorState, LoadingState } from '../../components/StateViews';
import {
  MAX_FILE_SIZE_BYTES,
  MAX_ROW_COUNT,
  parseFeedbackCsv,
  type CsvParseError,
  type ParsedFeedbackRow,
} from '../../lib/csv/parser';
import { classifyError, formatNumber } from '../../lib/format';
import type { FeedbackImportSummary } from '../../lib/api/types';

interface UploadState {
  file: File;
  rows: ParsedFeedbackRow[];
  parseErrors: CsvParseError[];
  parsing: boolean;
}

type SubmitState =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'success'; summary: FeedbackImportSummary }
  | { kind: 'error'; message: string };

const MAX_PREVIEW_ROWS = 10;

export default function UploadPage() {
  const { activeProjectId } = useProjectContext();
  const { signal } = useProjectScopedFetch();
  const client = getApiClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<UploadState | null>(null);
  const [submit, setSubmit] = useState<SubmitState>({ kind: 'idle' });
  const [dragging, setDragging] = useState(false);

  const handleFiles = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const file = files[0]!;
    setSubmit({ kind: 'idle' });
    setState({ file, rows: [], parseErrors: [], parsing: true });
    if (file.size > MAX_FILE_SIZE_BYTES) {
      setState({
        file,
        rows: [],
        parsing: false,
        parseErrors: [
          {
            type: 'max_size',
            message: `文件 ${file.name} 大小 ${formatBytes(file.size)} 超过 10 MiB 上限。`,
          },
        ],
      });
      return;
    }
    const result = await parseFeedbackCsv(file);
    setState({
      file,
      rows: result.rows,
      parseErrors: result.errors,
      parsing: false,
    });
  }, []);

  const handleSelect = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      void handleFiles(e.target.files);
    },
    [handleFiles],
  );

  const handleDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragging(false);
      void handleFiles(e.dataTransfer.files);
    },
    [handleFiles],
  );

  const handleSubmit = useCallback(async () => {
    if (!activeProjectId) return;
    if (!state || state.rows.length === 0) return;
    if (!signal) return;
    const mySignal = signal;
    setSubmit({ kind: 'submitting' });
    const res = await client.importFeedback(activeProjectId, state.file, mySignal);
    if (mySignal.aborted) return;
    if (res.error || !res.data) {
      setSubmit({ kind: 'error', message: classifyError(res.error) });
      return;
    }
    setSubmit({ kind: 'success', summary: res.data });
  }, [activeProjectId, client, signal, state]);

  if (!activeProjectId) {
    return <EmptyState title="请先选择项目" description="在右上角选择或创建一个项目后再上传。" />;
  }

  return (
    <div className="page page-upload">
      <header className="page-header">
        <span className="eyebrow">上传 CSV</span>
        <h1 className="page-title">导入原始反馈</h1>
        <p className="page-subtitle">
          文件大小不超过 10 MiB；必填列 feedback_id、content；
          可选列 source、user_id、user_type、created_at。最大 10,000 行。
        </p>
      </header>

      <div
        className={`drop-zone${dragging ? ' is-dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <div className="drop-zone-icon" aria-hidden>
          ⤴
        </div>
        <div className="drop-zone-text">
          <strong>拖拽 CSV 文件到此</strong>
          <span>或</span>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => fileInputRef.current?.click()}
          >
            选择文件
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,text/csv"
          className="drop-zone-input"
          onChange={handleSelect}
        />
      </div>

      {state ? (
        <section className="panel">
          <header className="panel-header">
            <h2 className="section-title">{state.file.name}</h2>
            <span className="panel-hint">
              {formatBytes(state.file.size)} · {state.rows.length} 行有效
            </span>
          </header>
          {state.parsing ? (
            <LoadingState />
          ) : (
            <>
              {state.parseErrors.length > 0 ? (
                <div className="alert alert-warn" role="alert">
                  <strong>解析提示：</strong>
                  <ul className="bullet-list">
                    {state.parseErrors.map((e, i) => (
                      <li key={i}>{e.message}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {state.rows.length > MAX_ROW_COUNT ? (
                <div className="alert alert-warn" role="alert">
                  超过 {MAX_ROW_COUNT} 行上限，仅展示前 {MAX_ROW_COUNT} 行；后端将拒绝整批。
                </div>
              ) : null}
              {state.rows.length === 0 ? (
                <EmptyState
                  title="没有可导入的有效行"
                  description="请检查必填列与行内容是否完整。"
                />
              ) : (
                <div className="data-table-wrapper">
                  <table className="preview-table" role="table">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>feedback_id</th>
                        <th>content (截断)</th>
                        <th>source</th>
                        <th>user_id</th>
                        <th>created_at</th>
                      </tr>
                    </thead>
                    <tbody>
                      {state.rows.slice(0, MAX_PREVIEW_ROWS).map((row) => (
                        <tr key={row.row_number}>
                          <td>{row.row_number}</td>
                          <td>{row.feedback_id}</td>
                          <td className="col-content">
                            {row.content.length > 100
                              ? `${row.content.slice(0, 100)}…`
                              : row.content}
                          </td>
                          <td>{row.source || '—'}</td>
                          <td>{row.user_id || '—'}</td>
                          <td>{row.created_at || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {state.rows.length > MAX_PREVIEW_ROWS ? (
                    <div className="preview-hint">
                      仅展示前 {MAX_PREVIEW_ROWS} / {formatNumber(state.rows.length)} 行
                    </div>
                  ) : null}
                </div>
              )}
              <footer className="upload-actions">
                {submit.kind === 'success' ? (
                  <div className="alert alert-info" role="status">
                    已导入 {submit.summary.imported} 条；跳过 {submit.summary.skipped} 条（合计 {submit.summary.total}）。
                    <Link className="btn btn-ghost btn-sm" href="/feedback">
                      查看反馈列表
                    </Link>
                  </div>
                ) : null}
                {submit.kind === 'error' ? (
                  <ErrorState
                    title="导入失败"
                    message={submit.message}
                    onRetry={() => void handleSubmit()}
                  />
                ) : null}
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={
                    state.rows.length === 0 ||
                    submit.kind === 'submitting' ||
                    state.rows.length > MAX_ROW_COUNT
                  }
                  onClick={() => void handleSubmit()}
                >
                  {submit.kind === 'submitting'
                    ? '导入中…'
                    : `导入 ${formatNumber(state.rows.length)} 条`}
                </button>
              </footer>
            </>
          )}
        </section>
      ) : null}
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
