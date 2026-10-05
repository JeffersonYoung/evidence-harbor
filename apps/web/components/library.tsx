'use client';
import { useEffect, useState } from 'react';
import { Search, FileText, Link2, ArrowUpRight, Plus, LibraryBig, X } from 'lucide-react';
import {
  type Document,
  type Source,
  type SearchResult,
  post,
  errorText,
  displayDate,
  safeHttpUrl,
} from '@/lib/api';
import { Empty, ErrorBox, Loading } from './ui';
import ScholarlyLeads from './scholarly-leads';
export default function Library({
  projectId,
  role,
  documents,
  sources,
  onAdd,
  onRead,
}: {
  projectId: string;
  role: string;
  documents: Document[];
  sources: Source[];
  onAdd: () => void;
  onRead: (id: string, blockId?: string, quote?: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [kind, setKind] = useState('all');
  const [pane, setPane] = useState<'documents' | 'leads'>('documents');
  useEffect(() => {
    let alive = true;
    if (!query.trim() || pane !== 'documents') {
      setResults(null);
      setLoading(false);
      setError('');
      return;
    }
    setLoading(true);
    const timer = setTimeout(
      () =>
        post<{ results: SearchResult[] }>('/search', {
          project_id: projectId,
          query: query.trim(),
          limit: 30,
        })
          .then((data) => {
            if (alive) {
              setResults(data.results);
              setError('');
            }
          })
          .catch((e) => alive && setError(errorText(e)))
          .finally(() => alive && setLoading(false)),
      350,
    );
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [projectId, query, pane]);
  const docs = documents.filter((d) => kind === 'all' || d.kind === kind);
  return (
    <section className="library-view">
      <div className="view-heading">
        <div>
          <span className="eyebrow">YOUR SOURCE OF TRUTH</span>
          <h1>资料库</h1>
          <p>让每一份资料，都成为可以再次找到的依据。</p>
        </div>
        <button className="button primary" onClick={onAdd}>
          <Plus size={16} />
          添加资料
        </button>
      </div>
      <div className="inline-tabs asset-tabs" aria-label="资料目录">
        <button
          className={pane === 'documents' ? 'selected' : ''}
          aria-pressed={pane === 'documents'}
          onClick={() => setPane('documents')}
        >
          可阅读资料 · {documents.length}
        </button>
        <button
          className={pane === 'leads' ? 'selected' : ''}
          aria-pressed={pane === 'leads'}
          onClick={() => setPane('leads')}
        >
          学术线索
        </button>
      </div>
      {pane === 'leads' ? (
        <ScholarlyLeads key={projectId} projectId={projectId} role={role} onRead={onRead} />
      ) : (
        <>
          <p className="scope-note">
            此处为已解析的资料与已发布报告。解析完成不代表论文已通读；学术全文留存状态请查看「学术线索」。
          </p>
          <div className="library-toolbar">
            <label className="search-box">
              <Search size={17} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索资料正文与证据…"
                aria-label="搜索项目资料"
              />
              {query && (
                <button aria-label="清空搜索" onClick={() => setQuery('')}>
                  <X size={15} />
                </button>
              )}
            </label>
            <select aria-label="资料类型" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="all">全部类型</option>
              <option value="source">原始资料</option>
              <option value="report">研究报告</option>
            </select>
          </div>
          {error && <ErrorBox>{error}</ErrorBox>}
          {loading ? (
            <Loading label="检索项目资料" />
          ) : results !== null ? (
            <>
              <div className="list-caption">找到 {results.length} 个相关片段 · 已保存正文检索</div>
              {results.length ? (
                results.map((r, index) => (
                  <button
                    key={`${r.block_id}-${index}`}
                    className="search-result"
                    onClick={() => onRead(r.document_id, r.block_id)}
                  >
                    <div>
                      <span>
                        <FileText size={15} />
                        {r.title}
                      </span>
                      <ArrowUpRight size={18} />
                    </div>
                    <p>{r.text}</p>
                    <small>
                      原文定位 <span>·</span> 相关度 {Number(r.score).toFixed(2)}
                    </small>
                  </button>
                ))
              ) : (
                <Empty icon={<Search size={25} />} title="没有找到相关内容">
                  换个词试试，或添加更多与研究问题相关的资料。
                </Empty>
              )}
            </>
          ) : docs.length ? (
            <div className="document-list">
              <div className="document-list-header">
                <span>名称</span>
                <span>类型</span>
                <span>添加时间</span>
                <span />
              </div>
              {docs.map((doc) => {
                const source = sources.find((s) => s.id === doc.source_id);
                const href = source && safeHttpUrl(source.canonical_uri);
                return (
                  <div className="document-row" key={doc.id}>
                    <button className="document-name" onClick={() => onRead(doc.id)}>
                      <span
                        className={`document-icon ${doc.kind === 'report' ? 'report-icon' : ''}`}
                      >
                        {source?.kind === 'url' ? <Link2 size={19} /> : <FileText size={19} />}
                      </span>
                      <span>
                        <strong>{doc.title}</strong>
                        <small>
                          {href
                            ? new URL(href).hostname
                            : doc.kind === 'report'
                              ? `已发布报告 · 版本 v${doc.version}`
                              : '已保存原始快照'}
                        </small>
                      </span>
                    </button>
                    <span className="type-chip">
                      {doc.kind === 'report'
                        ? '报告'
                        : source?.kind === 'url'
                          ? '网页'
                          : source?.kind === 'file'
                            ? '文件'
                            : '文本'}
                    </span>
                    <span className="date-cell">{displayDate(doc.created_at)}</span>
                    <button
                      className="icon-button"
                      aria-label={`阅读 ${doc.title}`}
                      onClick={() => onRead(doc.id)}
                    >
                      <ArrowUpRight size={18} />
                    </button>
                  </div>
                );
              })}
            </div>
          ) : (
            <Empty
              icon={<LibraryBig size={26} />}
              title={kind === 'all' ? '把第一份资料放进来' : '还没有这一类资料'}
              action={
                <button className="button secondary" onClick={onAdd}>
                  <Plus size={15} />
                  添加资料
                </button>
              }
            >
              网页、文档和笔记，将在这里汇聚。内容会经过解析，并保留稳定的证据定位。
            </Empty>
          )}
        </>
      )}
    </section>
  );
}
