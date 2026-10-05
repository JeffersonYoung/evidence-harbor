'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronDown,
  CircleHelp,
  Compass,
  FileText,
  FlaskConical,
  FolderOpen,
  LibraryBig,
  LoaderCircle,
  LogOut,
  Menu,
  MessageCircleQuestion,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  X,
} from 'lucide-react';
import {
  type Project,
  type Operation,
  type Run,
  type Document,
  type Proposal,
  type Evidence,
  api,
  post,
  errorText,
  ApiError,
  isPending,
} from '@/lib/api';
import { Empty, ErrorBox, Loading, Modal, Field } from './ui';
import { CreateProject, IngestForm, ResearchForm, ProposalForm } from './forms';
import Library from './library';
import Report from './report';
import Questions from './questions';
import ActivityView from './activity';
import Reader from './reader';
import Settings from './settings';
type Tab = 'report' | 'library' | 'questions' | 'activity';
type ReaderState = { id: string; blockId?: string; quote?: string; startOffset?: number };
function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <span />
        <span />
        <span />
      </span>
      <span>
        <strong>证据港</strong>
        <small>EVIDENCEHARBOR</small>
      </span>
    </div>
  );
}
function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <main className="login-page">
      <div className="login-panel">
        <Brand />
        <span className="eyebrow">YOUR PRIVATE RESEARCH WORKSPACE</span>
        <h1>
          欢迎回到
          <br />
          有据可循的世界。
        </h1>
        <p>登录你的工作空间，继续未完成的探索。</p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const data = new FormData(e.currentTarget);
            setBusy(true);
            setError('');
            try {
              await post('/auth/login', {
                username: data.get('username'),
                password: data.get('password'),
              });
              onLoggedIn();
            } catch (e) {
              setError(
                e instanceof ApiError && e.status === 401
                  ? '账户或密码不正确，请重新输入'
                  : errorText(e),
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          <Field label="账户">
            <input autoComplete="username" name="username" required autoFocus />
          </Field>
          <Field label="密码">
            <input type="password" autoComplete="current-password" name="password" required />
          </Field>
          {error && <ErrorBox>{error}</ErrorBox>}
          <button className="button primary" disabled={busy}>
            {busy ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}
            登录工作空间
          </button>
        </form>
        <div className="subtle-note">
          <ShieldCheck size={15} />
          由管理员创建账户 · 会话凭证不对浏览器脚本开放
        </div>
      </div>
      <div className="login-art" aria-hidden="true">
        <span className="orbit orbit-one" />
        <span className="orbit orbit-two" />
        <span className="orbit orbit-three" />
        <span className="art-point point-one" />
        <span className="art-point point-two" />
        <div>
          <QuoteMark />
          <p>
            每一个结论，
            <br />
            都有一个可信的起点。
          </p>
          <span>COLLECT. QUESTION. CONNECT.</span>
        </div>
      </div>
    </main>
  );
}
function QuoteMark() {
  return <span className="quote-mark">“</span>;
}
export default function Workspace() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [projectLoading, setProjectLoading] = useState(false);
  const [error, setError] = useState('');
  const [authRequired, setAuthRequired] = useState(false);
  const [role, setRole] = useState('');
  const [tab, setTab] = useState<Tab>('report');
  const [modal, setModal] = useState<
    'project' | 'ingest' | 'research' | 'proposal' | 'diagnostics' | null
  >(null);
  const [reader, setReader] = useState<ReaderState | null>(null);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [question, setQuestion] = useState('');
  const [target, setTarget] = useState<Document | undefined>();
  const [toast, setToast] = useState('');
  const [mobileNav, setMobileNav] = useState(false);
  const [exporting, setExporting] = useState(false);
  const selectedRef = useRef('');
  selectedRef.current = projectId;
  const notify = (text: string) => setToast(text);
  const handleError = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) setAuthRequired(true);
    else setError(errorText(e));
  }, []);
  const loadProjects = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api<Project[]>('/projects');
      setProjects(result);
      setAuthRequired(false);
      setProjectId((prev) => (result.some((p) => p.id === prev) ? prev : result[0]?.id || ''));
      api<{ role?: string }>('/auth/me')
        .then((u) => setRole(u.role || ''))
        .catch(() => {});
    } catch (e) {
      handleError(e);
    } finally {
      setLoading(false);
    }
  }, [handleError]);
  const loadProject = useCallback(
    async (silent = false) => {
      if (!projectId) return;
      if (!silent) setProjectLoading(true);
      try {
        const data = await api<Project>(`/projects/${projectId}`);
        if (selectedRef.current !== projectId) return;
        setProject(data);
        if (data.operations) setOperations(data.operations);
        setRuns(data.runs || []);
        setError('');
      } catch (e) {
        if (selectedRef.current === projectId) handleError(e);
      } finally {
        if (selectedRef.current === projectId) setProjectLoading(false);
      }
    },
    [projectId, handleError],
  );
  useEffect(() => {
    loadProjects();
  }, [loadProjects]);
  useEffect(() => {
    setProject(null);
    setOperations([]);
    setRuns([]);
    setReader(null);
    setError('');
    loadProject();
  }, [loadProject]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 5000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    const pendingOps = operations.filter((o) => isPending(o.status));
    const pendingRuns = runs.filter((r) => isPending(r.status));
    if (authRequired || !projectId || (!pendingOps.length && !pendingRuns.length)) return;
    let stopped = false;
    let inFlight = false;
    const timer = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const [nextOps, nextRuns] = await Promise.all([
          Promise.all(pendingOps.map((o) => api<Operation>(`/operations/${o.id}`))),
          Promise.all(pendingRuns.map((r) => api<Run>(`/research-runs/${r.id}`))),
        ]);
        if (stopped) return;
        setOperations((prev) => prev.map((o) => nextOps.find((n) => n.id === o.id) || o));
        setRuns((prev) => prev.map((r) => nextRuns.find((n) => n.id === r.id) || r));
        if ([...nextOps, ...nextRuns].some((o) => !isPending(o.status))) await loadProject(true);
      } catch (e) {
        if (!stopped) handleError(e);
      } finally {
        inFlight = false;
      }
    }, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [operations, runs, projectId, loadProject, handleError, authRequired]);
  async function openEvidence(id: string) {
    try {
      const e = await api<Evidence>(`/evidence/${id}`);
      const docId =
        e.document_id ||
        e.document?.id ||
        project?.documents?.find((d) => d.capture_id === e.capture_id)?.id;
      if (!docId) {
        setError('这条证据的原始文档不在当前项目中，无法定位。');
        return;
      }
      setReader({ id: docId, blockId: e.block_id, quote: e.quote, startOffset: e.start_offset });
    } catch (e) {
      handleError(e);
    }
  }
  async function exportProject() {
    setExporting(true);
    setError('');
    try {
      const r = await fetch(`/api/projects/${projectId}/export`, { cache: 'no-store' });
      if (!r.ok) {
        const e = await r.json().catch(() => ({ detail: '导出失败' }));
        throw new ApiError(r.status, typeof e.detail === 'string' ? e.detail : '导出失败', e);
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${project?.name || 'project'}-evidence-harbor.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify('项目导出包已准备好，下载已开始');
    } catch (e) {
      handleError(e);
    } finally {
      setExporting(false);
    }
  }
  const documents = project?.documents || [];
  const proposals = project?.proposals || [];
  const questions = project?.questions || [];
  const sources = project?.sources || [];
  const activeCount =
    operations.filter((o) => isPending(o.status)).length +
    runs.filter((r) => isPending(r.status)).length;
  const reviewCount = proposals.filter(
    (p) => p.status !== 'published' && p.status !== 'rejected',
  ).length;
  const startResearch = (text = '') => {
    setQuestion(text);
    setModal('research');
  };
  const tabs: [Tab, string, typeof FileText, number?][] = [
    ['report', '报告', FileText, reviewCount],
    ['library', '资料库', LibraryBig, documents.length],
    ['questions', '问题', MessageCircleQuestion, questions.length],
    ['activity', '运行记录', Activity, activeCount],
  ];
  if (authRequired) return <Login onLoggedIn={loadProjects} />;
  return (
    <div className="workspace">
      <a className="skip-link" href="#main-content">
        跳到主内容
      </a>
      {mobileNav && (
        <button className="nav-scrim" aria-label="关闭导航" onClick={() => setMobileNav(false)} />
      )}
      <aside className={`sidebar ${mobileNav ? 'mobile-open' : ''}`}>
        <Brand />
        <div className="workspace-label">
          <span className="workspace-avatar">W</span>
          <div>
            <strong>研究工作空间</strong>
            <small>
              {role
                ? { admin: '管理员', editor: '编辑者', researcher: '研究员', reader: '只读成员' }[
                    role
                  ] || role
                : '私人研究 · 精确溯源'}
            </small>
          </div>
          <span className="connection-dot" title="工作空间" />
        </div>
        <div className="sidebar-section-title">
          <span>我的项目</span>
          <button
            className="icon-button"
            title="新建项目"
            aria-label="新建项目"
            onClick={() => {
              setModal('project');
              setMobileNav(false);
            }}
          >
            <Plus size={17} />
          </button>
        </div>
        <nav className="project-nav" aria-label="项目导航">
          {projects.map((p) => (
            <button
              key={p.id}
              className={projectId === p.id ? 'selected' : ''}
              onClick={() => {
                setProjectId(p.id);
                setTab('report');
                setMobileNav(false);
              }}
            >
              <FolderOpen size={17} />
              <span>{p.name}</span>
              {projectId === p.id && <span className="nav-active-dot" />}
            </button>
          ))}
          {!loading && !projects.length && (
            <p className="sidebar-empty">
              一个清晰的问题，
              <br />
              就是新项目的开始。
            </p>
          )}
        </nav>
        <button
          className="new-project-button"
          onClick={() => {
            setModal('project');
            setMobileNav(false);
          }}
        >
          <Plus size={16} />
          新建研究项目
        </button>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <Compass size={21} />
            <p>
              收集有价值的资料，
              <br />
              留下有依据的洞见。
            </p>
          </div>
          <button onClick={() => setModal('diagnostics')}>
            <Settings2 size={17} />
            系统与流水线
            <span className="mini-dot" />
          </button>
          {role && (
            <button
              onClick={async () => {
                try {
                  await post('/auth/logout', {});
                  setAuthRequired(true);
                  setProjects([]);
                  setProject(null);
                  setRole('');
                } catch (e) {
                  handleError(e);
                }
              }}
            >
              <LogOut size={16} />
              退出账户
            </button>
          )}
          <div className="sidebar-copyright">
            EVIDENCEHARBOR <span>·</span> 证据港
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              onClick={() => setMobileNav(true)}
            >
              <Menu size={20} />
            </button>
            <span>工作空间</span>
            <span className="slash">/</span>
            <strong>
              {project?.name || projects.find((p) => p.id === projectId)?.name || '开始探索'}
            </strong>
          </div>
          <div className="topbar-actions">
            {activeCount > 0 && (
              <button className="processing-badge" onClick={() => setTab('activity')}>
                <LoaderCircle size={13} className="spin" />
                {activeCount} 项处理中
              </button>
            )}
            <button
              className="icon-button"
              aria-label="系统诊断"
              title="系统诊断"
              onClick={() => setModal('diagnostics')}
            >
              <CircleHelp size={19} />
            </button>
            <span className="topbar-avatar">{role === 'admin' ? 'A' : '研'}</span>
          </div>
        </header>
        {projectId && (
          <div className="project-header">
            <div className="project-heading">
              <div className="project-symbol">
                <FolderOpen size={23} />
              </div>
              <div>
                <h2>{project?.name || '正在加载项目'}</h2>
                <p>{project?.description || '围绕一个问题，构建你的证据与洞见。'}</p>
              </div>
              <button
                className="button secondary small export-button"
                disabled={exporting}
                onClick={exportProject}
              >
                {exporting ? (
                  <LoaderCircle size={15} className="spin" />
                ) : (
                  <ArrowDownToLine size={15} />
                )}
                导出项目
              </button>
            </div>
            <div className="tab-row">
              <nav className="project-tabs" aria-label="项目视图">
                {tabs.map(([id, label, Icon, count]) => (
                  <button
                    className={tab === id ? 'selected' : ''}
                    onClick={() => setTab(id)}
                    key={id}
                    aria-current={tab === id ? 'page' : undefined}
                  >
                    <Icon size={16} />
                    {label}
                    {!!count && (
                      <span className={id === 'report' ? 'attention-count' : ''}>{count}</span>
                    )}
                  </button>
                ))}
              </nav>
              <button className="button primary small" onClick={() => setModal('ingest')}>
                <Plus size={15} />
                添加资料
              </button>
            </div>
          </div>
        )}
        <main id="main-content" className="main-content">
          {error && (
            <ErrorBox retry={() => (projectId ? loadProject() : loadProjects())}>{error}</ErrorBox>
          )}
          {loading ? (
            <Loading label="连接工作空间" />
          ) : !projectId ? (
            <div className="welcome">
              <span className="eyebrow">A HOME FOR YOUR RESEARCH</span>
              <h1>
                让每一次探索，
                <br />
                都有据可循。
              </h1>
              <p>
                把资料、问题、证据与报告，放在同一个地方。
                <br />
                从一个你真正想回答的问题开始。
              </p>
              <button className="button primary" onClick={() => setModal('project')}>
                <Plus size={17} />
                创建第一个研究项目
              </button>
              <div className="welcome-principles">
                {[
                  [LibraryBig, '资料有归处', '网页、文件与笔记，统一收纳'],
                  [Search, '证据有出处', '精确到原文片段，随时回溯'],
                  [ShieldCheck, '结论有分寸', '先审核，再发布，保留版本'],
                ].map(([Icon, title, text]) => (
                  <div key={String(title)}>
                    <Icon size={22} />
                    <strong>{String(title)}</strong>
                    <p>{String(text)}</p>
                  </div>
                ))}
              </div>
            </div>
          ) : projectLoading && !project ? (
            <Loading label="载入项目" />
          ) : project ? (
            <>
              {tab === 'report' && (
                <Report
                  documents={documents}
                  proposals={proposals}
                  onRefresh={() => loadProject(true)}
                  onDraft={(doc) => {
                    setTarget(doc);
                    setModal('proposal');
                  }}
                  onRead={(id) => setReader({ id })}
                  onEvidence={openEvidence}
                  onResearch={() => startResearch()}
                  onLibrary={() => setTab('library')}
                />
              )}{' '}
              {tab === 'library' && (
                <Library
                  key={projectId}
                  projectId={projectId}
                  role={role}
                  documents={documents}
                  sources={sources}
                  onAdd={() => setModal('ingest')}
                  onRead={(id, blockId, quote) => setReader({ id, blockId, quote })}
                />
              )}{' '}
              {tab === 'questions' && (
                <Questions
                  projectId={projectId}
                  questions={questions}
                  onRefresh={() => loadProject(true)}
                  onResearch={startResearch}
                  onEvidence={openEvidence}
                />
              )}{' '}
              {tab === 'activity' && (
                <ActivityView
                  key={projectId}
                  role={role}
                  operations={operations}
                  runs={runs}
                  onOperation={(o) => {
                    setOperations((prev) => [o, ...prev.filter((p) => p.id !== o.id)]);
                    loadProject(true);
                  }}
                  onReport={() => setTab('report')}
                />
              )}
            </>
          ) : null}
        </main>
        <footer className="workspace-footer">
          <span>从资料到洞见，每一步都可追溯</span>
          <span>
            <span />
            EVIDENCEHARBOR
          </span>
        </footer>
      </div>
      {toast && (
        <div className="toast" role="status">
          <Check size={17} />
          {toast}
          <button aria-label="关闭通知" onClick={() => setToast('')}>
            <X size={15} />
          </button>
        </div>
      )}
      {modal === 'project' && (
        <CreateProject
          onClose={() => setModal(null)}
          onCreated={(p) => {
            setProjects((prev) => [p, ...prev]);
            setProjectId(p.id);
            setTab('report');
            setModal(null);
            notify('新项目已创建，添加第一份资料吧');
          }}
        />
      )}
      {modal === 'ingest' && projectId && (
        <IngestForm
          projectId={projectId}
          onClose={() => setModal(null)}
          onCreated={(o) => {
            setOperations((prev) => [o, ...prev.filter((i) => i.id !== o.id)]);
            setModal(null);
            setTab('activity');
            notify('资料已提交，正在后台处理');
            if (!isPending(o.status)) loadProject(true);
          }}
        />
      )}
      {modal === 'research' && projectId && (
        <ResearchForm
          projectId={projectId}
          initialQuestion={question}
          onClose={() => setModal(null)}
          onCreated={(r) => {
            setRuns((prev) => [r, ...prev.filter((i) => i.id !== r.id)]);
            setModal(null);
            setTab('activity');
            notify('研究任务已提交');
            if (!isPending(r.status)) loadProject(true);
          }}
        />
      )}
      {modal === 'proposal' && projectId && (
        <ProposalForm
          projectId={projectId}
          target={target}
          evidence={project?.evidence || []}
          onClose={() => setModal(null)}
          onCreated={() => {
            setModal(null);
            setTab('report');
            loadProject(true);
            notify('提案已保存，审核通过后即可发布');
          }}
        />
      )}
      {modal === 'diagnostics' && (
        <Settings
          projectId={projectId}
          documents={documents}
          onClose={() => setModal(null)}
          onOperation={(o) => setOperations((prev) => [o, ...prev.filter((p) => p.id !== o.id)])}
        />
      )}{' '}
      {reader && projectId && (
        <Reader
          key={`${reader.id}-${reader.blockId || ''}`}
          documentId={reader.id}
          projectId={projectId}
          focusBlockId={reader.blockId}
          focusQuote={reader.quote}
          focusStartOffset={reader.startOffset}
          onClose={() => setReader(null)}
          onEvidence={() => {
            notify('证据片段已保存，原始位置已绑定');
            loadProject(true);
          }}
        />
      )}
    </div>
  );
}
