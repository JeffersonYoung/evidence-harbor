'use client';
import { useCallback, useEffect, useState } from 'react';
import {
  ArrowRight,
  Check,
  Code2,
  FlaskConical,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';
import { api, post, type Document, type Operation, errorText, isPending } from '@/lib/api';
import { Modal, Field, Loading, ErrorBox, Status, Empty } from './ui';
type Pipeline = {
  id: string;
  name: string;
  config_json: Record<string, unknown>;
  config_hash: string;
  created_at: string;
};
type Watch = {
  id: string;
  title: string;
  locator: string;
  interval_seconds: number;
  enabled: boolean;
  schedule_version?: string;
  source_type?: string;
  research_on_change?: boolean;
  research_question?: string;
  classification?: string;
};
const DEFAULT_PIPELINE = `version: 1\nstages:\n  - name: raw_verify\n    options:\n      max_bytes: 26214400\n  - name: extract\n    options:\n      html_parser: auto\n      pdf_parser: auto\n  - name: normalize_privacy\n    options:\n      redact_emails: false\n      redact_phones: false\n  - name: source_map\n    options:\n      max_block_chars: 3000\n  - name: quality\n    options:\n      min_characters: 1\n      min_word_tokens: 1\n      max_replacement_ratio: 0.05\n  - name: lexical_index\n    options:\n      minimum_token_length: 1\n`;
export default function Settings({
  projectId,
  documents,
  onClose,
  onOperation,
}: {
  projectId: string;
  documents: Document[];
  onClose: () => void;
  onOperation: (o: Operation) => void;
}) {
  const [tab, setTab] = useState('diagnostics');
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null);
  const [registry, setRegistry] = useState<Record<string, unknown> | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [watches, setWatches] = useState<Watch[]>([]);
  const [config, setConfig] = useState(DEFAULT_PIPELINE);
  const [name, setName] = useState('默认解析流水线');
  const [pipelineId, setPipelineId] = useState('');
  const [captureId, setCaptureId] = useState('');
  const [trial, setTrial] = useState<Operation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [watchType, setWatchType] = useState('http');
  const [researchOnChange, setResearchOnChange] = useState(false);
  const load = useCallback(async () => {
    setError('');
    try {
      const [d, r, p, w] = await Promise.all([
        api<Record<string, unknown>>('/diagnostics'),
        api<Record<string, unknown>>('/pipeline-registry'),
        projectId
          ? api<{ items: Pipeline[] }>(`/pipelines?project_id=${projectId}`)
          : Promise.resolve({ items: [] }),
        projectId
          ? api<{ items: Watch[] }>(`/source-watches?project_id=${projectId}`)
          : Promise.resolve({ items: [] }),
      ]);
      setDiagnostics(d);
      setRegistry(r);
      setPipelines(p.items);
      setWatches(w.items);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoaded(true);
    }
  }, [projectId]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!trial || !isPending(trial.status)) return;
    let stopped = false;
    const timer = setInterval(async () => {
      try {
        const next = await api<Operation>('/operations/' + trial.id);
        if (!stopped) setTrial(next);
      } catch (e) {
        if (!stopped) setError(errorText(e));
      }
    }, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [trial]);
  async function patchWatch(w: Watch, changes: Record<string, unknown>) {
    setBusy(true);
    setError('');
    try {
      await api(`/source-watches/${w.id}`, { method: 'PATCH', body: JSON.stringify(changes) });
      await load();
      setNotice('监控配置已保存，等待调度器同步');
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal wide title="研究系统与流水线" kicker="WORKSPACE SETTINGS / 系统管理" onClose={onClose}>
      <div className="inline-tabs settings-tabs">
        {[
          ['diagnostics', '系统诊断'],
          ['pipelines', '解析流水线'],
          ['watches', '来源监控'],
        ].map(([id, label]) => (
          <button
            key={id}
            className={tab === id ? 'selected' : ''}
            onClick={() => {
              setTab(id);
              setError('');
              setNotice('');
            }}
          >
            {label}
          </button>
        ))}
      </div>
      {error && <ErrorBox retry={load}>{error}</ErrorBox>}
      {notice && (
        <p className="settings-notice" role="status">
          <Check size={14} />
          {notice}
        </p>
      )}
      {!loaded ? (
        <Loading label="读取系统配置" />
      ) : tab === 'diagnostics' ? (
        <>
          <p className="dialog-intro">
            以下是当前服务的真实诊断信息。外部模型与向量检索仅在管理员显式启用后可用。
          </p>
          {diagnostics && (
            <>
              <div className="diagnostic-summary">
                <ShieldCheck size={20} />
                <div>
                  <strong>服务已响应</strong>
                  <p>服务状态与工作流配置以当前返回为准</p>
                </div>
              </div>
              <pre className="diagnostics-json">{JSON.stringify(diagnostics, null, 2)}</pre>
            </>
          )}
        </>
      ) : !projectId ? (
        <Empty title="先选择一个项目">解析配置和来源监控都属于具体研究项目。</Empty>
      ) : tab === 'pipelines' ? (
        <>
          <p className="dialog-intro">
            修改声明式 YAML /
            JSON，保存不可变版本，并在原始快照上试跑。试跑只比较结果，不会覆盖当前资料。
          </p>
          <div className="pipeline-saved">
            <label className="field">
              <span>已保存的配置版本</span>
              <select
                value={pipelineId}
                onChange={(e) => {
                  setPipelineId(e.target.value);
                  const selected = pipelines.find((p) => p.id === e.target.value);
                  if (selected) {
                    setName(selected.name);
                    setConfig(JSON.stringify(selected.config_json, null, 2));
                  }
                }}
              >
                <option value="">选择一个版本，或新建配置</option>
                {pipelines.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} · {p.config_hash.slice(0, 8)}
                  </option>
                ))}
              </select>
            </label>
            <details className="registry-view">
              <summary>
                <Code2 size={14} />
                查看允许的阶段和参数
              </summary>
              <pre className="diagnostics-json">{JSON.stringify(registry, null, 2)}</pre>
            </details>
          </div>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError('');
              try {
                const p = await post<Pipeline>('/pipelines', {
                  project_id: projectId,
                  name,
                  config,
                });
                setPipelineId(p.id);
                await load();
                setNotice('新的流水线版本已保存，原有版本保持不变');
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <Field label="配置名称">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={120}
              />
            </Field>
            <Field
              label="流水线 YAML / JSON"
              hint="仅接受注册阶段和受限参数，不支持脚本、代码或导入。"
            >
              <textarea
                className="code-editor"
                value={config}
                onChange={(e) => setConfig(e.target.value)}
                rows={13}
                spellCheck={false}
                required
              />
            </Field>
            <button className="button secondary small" disabled={busy}>
              {busy ? <LoaderCircle className="spin" size={14} /> : <Plus size={14} />}保存为新版本
            </button>
          </form>
          <div className="pipeline-trial">
            <h3>
              <FlaskConical size={16} />
              在原始快照上试跑
            </h3>
            <div className="trial-controls">
              <label className="sr-only" htmlFor="trial-capture">
                试跑资料
              </label>
              <select
                id="trial-capture"
                value={captureId}
                onChange={(e) => setCaptureId(e.target.value)}
              >
                <option value="">选择已导入的资料</option>
                {documents
                  .filter((d) => d.capture_id)
                  .map((d) => (
                    <option value={d.capture_id} key={d.id}>
                      {d.title}
                    </option>
                  ))}
              </select>
              <button
                className="button primary small"
                disabled={busy || !pipelineId || !captureId}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    const op = await post<Operation>(`/pipelines/${pipelineId}/test-runs`, {
                      capture_id: captureId,
                    });
                    setTrial(op);
                    onOperation(op);
                  } catch (e) {
                    setError(errorText(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <Play size={14} />
                运行样本
              </button>
            </div>
            {!pipelineId && <p className="muted small-copy">请先保存或选择一个配置版本</p>}
            {trial && (
              <div className="trial-result">
                <div className="section-heading">
                  <strong>样本处理结果</strong>
                  <Status value={trial.status} />
                </div>
                {trial.error && <ErrorBox>{trial.error}</ErrorBox>}
                {trial.result_json && (
                  <>
                    <p className="subtle-note">未发布 · 当前资料没有被覆盖</p>
                    <details open>
                      <summary>质量、门禁与警告</summary>
                      <pre>
                        {JSON.stringify(
                          {
                            quality: trial.result_json.quality,
                            gates: trial.result_json.gates,
                            warnings: trial.result_json.warnings,
                            parser_version: trial.result_json.parser_version,
                          },
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                    <details open>
                      <summary>与当前表示的差异</summary>
                      <pre className="diff-view">
                        {Array.isArray(trial.result_json.diff)
                          ? trial.result_json.diff.join('\n')
                          : JSON.stringify(trial.result_json.diff, null, 2)}
                      </pre>
                    </details>
                  </>
                )}
              </div>
            )}
          </div>
        </>
      ) : (
        <>
          <p className="dialog-intro">
            定期检查公开来源的新内容。只有启用 Temporal worker
            与调度器后，周期任务才会自动运行；保存配置不代表调度已生效。
          </p>
          <form
            className="watch-form"
            onSubmit={async (e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              setBusy(true);
              setError('');
              try {
                await post<Watch>('/source-watches', {
                  project_id: projectId,
                  locator: f.get('locator'),
                  title: f.get('title') || '',
                  interval_seconds: Number(f.get('interval')),
                  enabled: f.get('enabled') === 'on',
                  source_type: watchType,
                  allowed_domains: String(f.get('allowed_domains') || '')
                    .split(/[\s,]+/)
                    .filter(Boolean),
                  max_items: Number(f.get('max_items') || 50),
                  research_on_change: researchOnChange,
                  research_question: String(f.get('research_question') || ''),
                  classification: f.get('classification') || 'internal',
                  pipeline_config:
                    pipelines.find((p) => p.id === f.get('pipeline'))?.config_json || {},
                });
                await load();
                setNotice('来源监控配置已保存，等待调度器同步');
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <div className="form-grid">
              <Field label="来源类型">
                <select value={watchType} onChange={(e) => setWatchType(e.target.value)}>
                  <option value="http">单个网页</option>
                  <option value="rss">RSS / Atom 订阅</option>
                  <option value="sitemap">网站地图 Sitemap</option>
                </select>
              </Field>
              <Field label="资料分级">
                <select name="classification" defaultValue="internal">
                  <option value="public">公开资料</option>
                  <option value="internal">内部资料（默认）</option>
                  <option value="sensitive">敏感资料</option>
                </select>
              </Field>
            </div>
            <Field label="来源网址">
              <input
                type="url"
                name="locator"
                required
                placeholder="https://example.com/research"
              />
            </Field>
            <div className="form-grid">
              <Field label="来源名称">
                <input name="title" placeholder="例如：每周研究简报" />
              </Field>
              <Field label="检查间隔（秒）">
                <input
                  name="interval"
                  type="number"
                  min="300"
                  max="31536000"
                  defaultValue="86400"
                  required
                />
              </Field>
            </div>
            {watchType !== 'http' && (
              <div className="form-grid">
                <Field
                  label="允许的域名（逗号分隔）"
                  hint="限制订阅条目的抓取范围；留空使用服务端安全策略。"
                >
                  <input name="allowed_domains" placeholder="example.com, research.example.com" />
                </Field>
                <Field label="每次最多条目">
                  <input name="max_items" type="number" min="1" max="100" defaultValue="50" />
                </Field>
              </div>
            )}
            <Field label="解析配置">
              <select name="pipeline">
                <option value="">服务端默认流水线</option>
                {pipelines.map((p) => (
                  <option value={p.id} key={p.id}>
                    {p.name} · {p.config_hash.slice(0, 8)}
                  </option>
                ))}
              </select>
            </Field>
            <label className="checkbox-label">
              <input name="enabled" type="checkbox" defaultChecked />
              <span>启用监控（最短间隔 5 分钟）</span>
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={researchOnChange}
                onChange={(e) => setResearchOnChange(e.target.checked)}
              />
              <span>检测到新内容后，自动运行本地研究并生成待审核提案</span>
            </label>
            {researchOnChange && (
              <Field label="持续研究问题" hint="仅生成提案，不会自动发布报告。">
                <textarea
                  name="research_question"
                  required
                  rows={2}
                  placeholder="持续跟踪哪些变化？"
                />
              </Field>
            )}
            <button className="button primary small" disabled={busy}>
              <Plus size={14} />
              添加来源监控
            </button>
          </form>
          <div className="watch-list">
            {watches.length ? (
              watches.map((w) => (
                <article key={w.id}>
                  <div className="section-heading">
                    <h3>{w.title || w.locator}</h3>
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={w.enabled}
                        disabled={busy}
                        onChange={(e) => patchWatch(w, { enabled: e.target.checked })}
                      />
                      <span>{w.enabled ? '启用' : '暂停'}</span>
                    </label>
                  </div>
                  <p className="watch-url">
                    {w.source_type || 'http'} · {w.locator}
                  </p>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={w.research_on_change || false}
                      disabled={busy}
                      onChange={(e) => patchWatch(w, { research_on_change: e.target.checked })}
                    />
                    <span>内容变化后生成研究提案</span>
                  </label>
                  {w.research_on_change && (
                    <Field label="持续研究问题">
                      <input
                        key={w.research_question}
                        defaultValue={w.research_question || ''}
                        onBlur={(e) => {
                          if (e.target.value !== w.research_question)
                            patchWatch(w, { research_question: e.target.value });
                        }}
                      />
                    </Field>
                  )}
                  <div className="watch-actions">
                    <label>
                      间隔（秒）
                      <input
                        aria-label={`${w.title || w.locator}的检查间隔`}
                        key={w.interval_seconds}
                        type="number"
                        min="300"
                        max="31536000"
                        defaultValue={w.interval_seconds}
                        disabled={busy}
                        onBlur={(e) => {
                          const value = Number(e.target.value);
                          if (value !== w.interval_seconds && value >= 300 && value <= 31536000)
                            patchWatch(w, { interval_seconds: value });
                        }}
                      />
                    </label>
                    <span className="micro-label">
                      {w.schedule_version ? '调度配置已同步' : '待调度器同步'}
                    </span>
                    <button
                      className="text-button"
                      disabled={busy || !w.enabled}
                      onClick={async () => {
                        setBusy(true);
                        setError('');
                        try {
                          const result = await post<{ operation_id?: string; status: string }>(
                            `/source-watches/${w.id}/check`,
                            {},
                          );
                          if (result.operation_id) {
                            const op = await api<Operation>('/operations/' + result.operation_id);
                            onOperation(op);
                          }
                          setNotice(
                            result.operation_id
                              ? '检查任务已排队，等待工作器处理'
                              : '来源监控已暂停',
                          );
                        } catch (e) {
                          setError(errorText(e));
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      <RefreshCw size={13} />
                      立即检查
                    </button>
                  </div>
                </article>
              ))
            ) : (
              <p className="small-copy muted">当前项目还没有来源监控。</p>
            )}
          </div>
        </>
      )}
      <div className="dialog-actions">
        <button className="button secondary" onClick={load}>
          <RefreshCw size={14} />
          刷新状态
        </button>
        <button className="button primary" onClick={onClose}>
          完成
        </button>
      </div>
    </Modal>
  );
}
