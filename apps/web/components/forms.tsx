'use client';
import { useState, useEffect } from 'react';
import {
  ArrowRight,
  UploadCloud,
  Link2,
  AlignLeft,
  LoaderCircle,
  Plus,
  ShieldCheck,
} from 'lucide-react';
import {
  type Project,
  type Operation,
  type Run,
  type Proposal,
  type Evidence,
  api,
  post,
  errorText,
} from '@/lib/api';
import { Modal, Field, ErrorBox } from './ui';
export function CreateProject({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (project: Project) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <Modal title="给你的研究，一个起点" kicker="NEW PROJECT / 新建项目" onClose={onClose}>
      <p className="dialog-intro">围绕一个主题，收集资料、提出问题，逐步形成有据可循的结论。</p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          setBusy(true);
          setError('');
          try {
            onCreated(
              await post<Project>('/projects', {
                name: String(f.get('name')).trim(),
                description: String(f.get('description')).trim(),
              }),
            );
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Field label="项目名称">
          <input
            name="name"
            required
            maxLength={255}
            placeholder="例如：下一代知识工作方式"
            autoFocus
          />
        </Field>
        <Field label="研究目标（选填）">
          <textarea
            name="description"
            rows={3}
            placeholder="这项研究，想回答什么？"
            maxLength={10000}
          />
        </Field>
        {error && <ErrorBox>{error}</ErrorBox>}
        <div className="dialog-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            取消
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}创建项目
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function IngestForm({
  projectId,
  onClose,
  onCreated,
}: {
  projectId: string;
  onClose: () => void;
  onCreated: (op: Operation) => void;
}) {
  const [mode, setMode] = useState('url');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [filename, setFilename] = useState('');
  const [key] = useState(() => crypto.randomUUID());
  const [pipelines, setPipelines] = useState<
    { id: string; name: string; config_json: Record<string, unknown> }[]
  >([]);
  const [selectedPipeline, setSelectedPipeline] = useState('');
  useEffect(() => {
    let active = true;
    api<{ items: { id: string; name: string; config_json: Record<string, unknown> }[] }>(
      `/pipelines?project_id=${projectId}`,
    )
      .then((r) => active && setPipelines(r.items))
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [projectId]);
  return (
    <Modal title="把资料带进来" kicker="ADD MATERIAL / 添加资料" onClose={onClose}>
      <p className="dialog-intro">原始资料会保留为不可变快照，每条证据都能追溯到具体出处。</p>
      <div className="segmented" role="tablist" aria-label="添加资料方式">
        {[
          ['url', '网页链接', Link2],
          ['text', '粘贴文本', AlignLeft],
          ['file', '上传文件', UploadCloud],
        ].map(([id, label, Icon]) => (
          <button
            type="button"
            role="tab"
            aria-selected={mode === id}
            className={mode === id ? 'selected' : ''}
            key={String(id)}
            onClick={() => {
              setMode(String(id));
              setError('');
            }}
            disabled={busy}
          >
            <Icon size={16} />
            {String(label)}
          </button>
        ))}
      </div>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          setBusy(true);
          setError('');
          try {
            let op: Operation;
            if (mode === 'file') {
              f.set('project_id', projectId);
              op = await api<Operation>('/ingestions/upload', {
                method: 'POST',
                body: f,
                headers: { 'Idempotency-Key': key },
              });
            } else {
              op = await post<Operation>(
                '/ingestions',
                {
                  project_id: projectId,
                  type: mode,
                  title: f.get('title') || undefined,
                  classification: f.get('classification') || 'internal',
                  ...(selectedPipeline
                    ? {
                        pipeline_config: pipelines.find((p) => p.id === selectedPipeline)
                          ?.config_json,
                      }
                    : {}),
                  ...(mode === 'url' ? { url: f.get('url') } : { text: f.get('text') }),
                },
                { 'Idempotency-Key': key },
              );
            }
            onCreated(op);
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Field label="资料标题（选填）">
          <input name="title" placeholder="为这份资料起一个容易找到的名字" maxLength={500} />
        </Field>
        {mode === 'url' ? (
          <Field label="网页地址" hint="支持可公开访问的 HTTP / HTTPS 网页；私有网络地址会被阻止。">
            <input
              name="url"
              type="url"
              required
              placeholder="https://example.com/article"
              autoFocus
            />
          </Field>
        ) : mode === 'text' ? (
          <Field label="资料正文">
            <textarea
              name="text"
              rows={8}
              required
              placeholder="粘贴文章、访谈记录或自己的研究笔记…"
              maxLength={2000000}
            />
          </Field>
        ) : (
          <label className="dropzone">
            <UploadCloud size={30} />
            <strong>{filename || '选择一份资料'}</strong>
            <span>PDF、DOCX、HTML、TXT、Markdown · 最大 20 MB</span>
            <input
              name="file"
              type="file"
              required
              accept=".pdf,.docx,.txt,.md,.markdown,.html,.htm,.csv,.json,text/plain,text/html,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              onChange={(e) => {
                const file = e.target.files?.[0];
                setFilename(file?.name || '');
                if (file && file.size > 20 * 1024 * 1024) {
                  setError('文件超过 20 MB，请选择更小的文件');
                  e.target.value = '';
                } else setError('');
              }}
            />
          </label>
        )}
        {mode !== 'file' && pipelines.length > 0 && (
          <Field label="解析流水线">
            <select value={selectedPipeline} onChange={(e) => setSelectedPipeline(e.target.value)}>
              <option value="">服务端默认流水线</option>
              {pipelines.map((p) => (
                <option value={p.id} key={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
        )}
        <Field label="资料分级" hint="默认为内部资料；敏感资料的外部模型使用受服务端策略限制。">
          <select name="classification" defaultValue="internal">
            <option value="public">公开资料</option>
            <option value="internal">内部资料（默认）</option>
            <option value="sensitive">敏感资料</option>
          </select>
        </Field>
        <div className="subtle-note">
          <ShieldCheck size={15} />
          内容将由服务端安全解析，不会执行原始网页脚本
        </div>
        {error && <ErrorBox>{error}</ErrorBox>}
        <div className="dialog-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            取消
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}开始导入
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function ResearchForm({
  projectId,
  onClose,
  onCreated,
  initialQuestion = '',
}: {
  projectId: string;
  onClose: () => void;
  onCreated: (run: Run) => void;
  initialQuestion?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [provider, setProvider] = useState('local');
  return (
    <Modal title="带着问题，向前一步" kicker="RESEARCH / 开始研究" onClose={onClose}>
      <p className="dialog-intro">
        从项目中已有的资料出发，提取相关证据，形成一份可审核的研究提案。
      </p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          setBusy(true);
          setError('');
          try {
            onCreated(
              await post<Run>('/research-runs', {
                project_id: projectId,
                question: String(f.get('question')).trim(),
                provider,
                ...(f.get('model') ? { model: f.get('model') } : {}),
                reasoning_effort: f.get('reasoning_effort') || undefined,
                max_documents: Number(f.get('max_documents')),
                max_searches: Number(f.get('max_searches')),
                max_tool_calls: Number(f.get('max_tool_calls')),
                max_duration_seconds: Number(f.get('max_duration_seconds')),
                max_output_tokens: Number(f.get('max_output_tokens')),
                max_cost_usd: provider === 'local' ? 0 : Number(f.get('max_cost_usd')),
              }),
            );
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Field label="你想研究的问题">
          <textarea
            name="question"
            rows={4}
            required
            defaultValue={initialQuestion}
            placeholder="例如：这些资料对我们的核心假设提供了哪些支持与反证？"
            maxLength={10000}
            autoFocus
          />
        </Field>
        <Field label="研究方式">
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="local">本地证据检索 · 不调用外部模型</option>
            <option value="openai">OpenAI · 需管理员启用</option>
            <option value="openai-compatible">OpenAI-compatible · 需管理员启用</option>
          </select>
        </Field>
        {provider !== 'local' && (
          <>
            <div className="form-grid">
              <Field label="模型名称">
                <input name="model" placeholder="使用服务端默认模型" maxLength={120} />
              </Field>
              <Field label="推理强度">
                <select name="reasoning_effort" defaultValue="medium">
                  {['none', 'minimal', 'low', 'medium', 'high', 'xhigh'].map((v) => (
                    <option value={v} key={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Field label="本次费用上限（USD）" hint="会按服务端配置校验；外部调用须设置非零额度。">
              <input
                name="max_cost_usd"
                type="number"
                min="0.01"
                max="100"
                step="0.01"
                required
                defaultValue="0.10"
              />
            </Field>
            <label className="checkbox-label">
              <input type="checkbox" required />
              <span>同意将本次研究涉及的资料发送到管理员配置的模型服务</span>
            </label>
          </>
        )}
        <details className="advanced-settings">
          <summary>研究范围与预算</summary>
          <div className="form-grid">
            <Field label="最多检索次数">
              <input name="max_searches" type="number" min="1" max="10" defaultValue="1" required />
            </Field>
            <Field label="最多资料数">
              <input
                name="max_documents"
                type="number"
                min="1"
                max="30"
                defaultValue="8"
                required
              />
            </Field>
            <Field label="最多工具调用">
              <input
                name="max_tool_calls"
                type="number"
                min="3"
                max="100"
                defaultValue="16"
                required
              />
            </Field>
            <Field label="最长运行秒数">
              <input
                name="max_duration_seconds"
                type="number"
                min="5"
                max="900"
                defaultValue="180"
                required
              />
            </Field>
            <Field label="最大输出 tokens">
              <input
                name="max_output_tokens"
                type="number"
                min="128"
                max="16000"
                defaultValue="2000"
                required
              />
            </Field>
          </div>
        </details>
        <div className="info-panel">
          <span className="micro-label">
            {provider === 'local' ? 'LOCAL EVIDENCE MODE' : 'EXTERNAL MODEL MODE'}
          </span>
          <strong>{provider === 'local' ? '本地证据检索' : '受限的外部模型调用'}</strong>
          <p>
            {provider === 'local'
              ? '使用已导入资料，生成可追溯的证据汇编。当前模式不调用外部大模型，不会自动发布结论。'
              : '只使用服务端已配置的连接，限制资料、时长与费用。调用结果仍需人工核查和发布。'}
          </p>
        </div>
        {error && <ErrorBox>{error}</ErrorBox>}
        <div className="dialog-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            取消
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}开始研究
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function ProposalForm({
  projectId,
  target,
  evidence,
  onClose,
  onCreated,
}: {
  projectId: string;
  target?: { id: string; version: number; title: string; content: string };
  evidence: Evidence[];
  onClose: () => void;
  onCreated: (p: Proposal) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  return (
    <Modal
      wide
      title={target ? '起草报告修订' : '起草一份报告'}
      kicker="PROPOSAL / 待审核提案"
      onClose={onClose}
    >
      <p className="dialog-intro">
        每份提案至少关联一条可核查的证据。审核后再发布，现有报告不会被直接覆盖。
      </p>
      {!evidence.length ? (
        <div className="info-panel">
          <strong>先保存一条证据</strong>
          <p>
            在资料库打开一份文档，点击段落旁的引用按钮，保存原文片段；也可以运行研究任务，自动形成带有证据的提案。
          </p>
          <div className="dialog-actions">
            <button className="button secondary" onClick={onClose}>
              知道了
            </button>
          </div>
        </div>
      ) : (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            if (!selected.length) {
              setError('请至少选择一条证据');
              return;
            }
            if (!String(f.get('content')).includes(String(f.get('claim')))) {
              setError('核心结论需要原样出现在正文中，请从正文复制对应句子。');
              return;
            }
            setBusy(true);
            setError('');
            try {
              onCreated(
                await post<Proposal>('/proposals', {
                  project_id: projectId,
                  title: f.get('title'),
                  content: f.get('content'),
                  claims: [
                    {
                      text: f.get('claim'),
                      evidence_ids: selected,
                      scope: f.get('scope') || '',
                      limitations: String(f.get('limitations') || '')
                        .split('\n')
                        .filter(Boolean),
                    },
                  ],
                  ...(target
                    ? { target_document_id: target.id, base_version: target.version }
                    : {}),
                }),
              );
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Field label="报告标题">
            <input name="title" required defaultValue={target?.title} maxLength={500} />
          </Field>
          <Field label="正文" hint="支持纯文本与 Markdown 结构，HTML 将始终按文本显示。">
            <textarea
              name="content"
              rows={8}
              required
              defaultValue={target?.content}
              placeholder="# 核心发现\n\n写下你的研究结论与依据…"
            />
          </Field>
          <Field label="核心结论" hint="从正文复制一句完整结论，保持文字完全一致。">
            <textarea
              name="claim"
              rows={2}
              required
              placeholder="这份报告最重要的、可以被证据支撑的一条结论"
            />
          </Field>
          <div className="field">
            <span>关联证据（至少一条）</span>
            <div className="evidence-choices">
              {evidence.map((ev) => (
                <label key={ev.id} className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={selected.includes(ev.id)}
                    onChange={(e) =>
                      setSelected(
                        e.target.checked
                          ? [...selected, ev.id]
                          : selected.filter((id) => id !== ev.id),
                      )
                    }
                  />
                  <span>{ev.quote}</span>
                </label>
              ))}
            </div>
          </div>
          <div className="form-grid">
            <Field label="适用范围（选填）">
              <input name="scope" placeholder="这条结论适用于什么情境？" />
            </Field>
            <Field label="限制与不足（每行一条）">
              <textarea name="limitations" rows={2} placeholder="还有什么无法从这些证据中得出？" />
            </Field>
          </div>
          {target && (
            <p className="subtle-note">
              基于版本 v{target.version} · 发布时将校验版本，避免覆盖他人的更新
            </p>
          )}
          {error && <ErrorBox>{error}</ErrorBox>}
          <div className="dialog-actions">
            <button type="button" className="button secondary" onClick={onClose}>
              取消
            </button>
            <button className="button primary" disabled={busy || !selected.length}>
              {busy ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}保存为提案
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
