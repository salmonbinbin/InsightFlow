'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { useProjectContext } from '../contexts/ProjectContext';
import { LoadingState, ErrorState, EmptyState } from './StateViews';

export interface ProjectSwitcherProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Modal for picking the active project or creating a new one. Mounted via
 * portal-free controlled mode so the test renderer can use it without an
 * actual DOM portal.
 */
export function ProjectSwitcher({ open, onClose }: ProjectSwitcherProps) {
  const { projects, activeProjectId, selectProject, createProject, loading, error } =
    useProjectContext();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [productName, setProductName] = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setCreating(false);
      setName('');
      setProductName('');
      setDescription('');
      setSubmitError(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  if (!open) return null;

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!name.trim() || !productName.trim()) {
      setSubmitError('项目名称和产品名称必填。');
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      await createProject({
        name: name.trim(),
        product_name: productName.trim(),
        description: description.trim() || undefined,
      });
      onClose();
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : '创建失败。');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="modal modal--sm"
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-switcher-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id="project-switcher-title" className="modal-title">
            切换项目
          </h2>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="modal-body">
          {creating ? (
            <form onSubmit={handleSubmit} className="form-stack">
              <label className="form-field">
                <span className="form-label">项目名称</span>
                <input
                  className="form-input"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  maxLength={120}
                  autoFocus
                />
              </label>
              <label className="form-field">
                <span className="form-label">产品名称</span>
                <input
                  className="form-input"
                  type="text"
                  value={productName}
                  onChange={(e) => setProductName(e.target.value)}
                  required
                  maxLength={120}
                />
              </label>
              <label className="form-field">
                <span className="form-label">描述（可选）</span>
                <textarea
                  className="form-textarea"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={3}
                  maxLength={2000}
                />
              </label>
              {submitError ? (
                <div className="alert alert-danger" role="alert">
                  {submitError}
                </div>
              ) : null}
              <div className="modal-actions">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setCreating(false)}
                  disabled={submitting}
                >
                  返回列表
                </button>
                <button type="submit" className="btn btn-primary" disabled={submitting}>
                  {submitting ? '创建中…' : '创建并切换'}
                </button>
              </div>
            </form>
          ) : (
            <div className="project-list">
              {loading ? (
                <LoadingState />
              ) : error ? (
                <ErrorState message={error} />
              ) : projects.length === 0 ? (
                <EmptyState
                  title="暂无项目"
                  description="先创建一个项目，把原始反馈数据接入进来。"
                  action={
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => setCreating(true)}
                    >
                      创建项目
                    </button>
                  }
                />
              ) : (
                <>
                  <ul className="project-list-items">
                    {projects.map((p) => (
                      <li
                        key={p.id}
                        className={`project-list-item${p.id === activeProjectId ? ' is-active' : ''}`}
                      >
                        <button
                          type="button"
                          className="project-list-button"
                          onClick={() => {
                            selectProject(p.id);
                            onClose();
                          }}
                        >
                          <span className="project-list-name">{p.name}</span>
                          <span className="project-list-product">{p.product_name}</span>
                          {p.description ? (
                            <span className="project-list-description">{p.description}</span>
                          ) : null}
                        </button>
                      </li>
                    ))}
                  </ul>
                  <div className="modal-actions">
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={() => setCreating(true)}
                    >
                      新建项目
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
