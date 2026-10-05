'use client';
import { useEffect, useState, useRef } from 'react';
import {
  BookOpen,
  Quote,
  Copy,
  Check,
  LoaderCircle,
  History,
  ShieldCheck,
  LockKeyhole,
  Download,
} from 'lucide-react';
import {
  type Document,
  type Block,
  type Evidence,
  api,
  post,
  errorText,
  displayDate,
} from '@/lib/api';
import { Modal, Loading, ErrorBox, SafeText, HighlightedQuote } from './ui';
type Revision = { version: number; created_at?: string; title?: string; content?: string };
export default function Reader({
  documentId,
  projectId,
  focusBlockId,
  focusQuote,
  focusStartOffset,
  onClose,
  onEvidence,
}: {
  documentId: string;
  projectId: string;
  focusBlockId?: string;
  focusQuote?: string;
  focusStartOffset?: number;
  onClose: () => void;
  onEvidence: (e: Evidence) => void;
}) {
  const [doc, setDoc] = useState<Document | null>(null);
  const [error, setError] = useState('');
  const [versions, setVersions] = useState<Revision[]>([]);
  const [version, setVersion] = useState<number | undefined>();
  const [chosen, setChosen] = useState<Block | null>(null);
  const [quote, setQuote] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState('');
  const [copied, setCopied] = useState(false);
  const [lockBusy, setLockBusy] = useState(false);
  const focusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    let active = true;
    setError('');
    setDoc(null);
    api<Document>(`/documents/${documentId}/content${version ? `?version=${version}` : ''}`)
      .then((d) => {
        if (active) {
          setDoc(d);
          setChosen(null);
          setQuote('');
        }
      })
      .catch((e) => active && setError(errorText(e)));
    return () => {
      active = false;
    };
  }, [documentId, version]);
  useEffect(() => {
    let active = true;
    api<Revision[] | { versions: Revision[] }>(`/documents/${documentId}/versions`)
      .then((v) => active && setVersions(Array.isArray(v) ? v : v.versions))
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [documentId]);
  useEffect(() => {
    if (doc && focusBlockId) {
      const timer = setTimeout(
        () => focusRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }),
        120,
      );
      return () => clearTimeout(timer);
    }
  }, [doc, focusBlockId]);
  async function save() {
    if (!chosen || !quote.trim()) return;
    setSaving(true);
    setError('');
    try {
      const evidence = await post<Evidence>('/evidence', {
        project_id: projectId,
        block_id: chosen.id,
        quote: quote.trim(),
      });
      onEvidence(evidence);
      setSaved(evidence.id);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  }
  return (
    <Modal
      wide
      title={doc?.title || '打开资料'}
      kicker="DOCUMENT READER / 资料阅读器"
      onClose={onClose}
    >
      {error && <ErrorBox>{error}</ErrorBox>}
      {!doc && !error ? (
        <Loading label="读取原文" />
      ) : (
        doc && (
          <>
            <div className="reader-meta">
              <span>
                <BookOpen size={14} />
                {doc.kind === 'report' ? '研究报告' : '原始资料'}
              </span>
              <span>{displayDate(doc.created_at)}</span>
              <label className="version-control">
                <History size={14} />
                <span className="sr-only">文档版本</span>
                <select
                  aria-label="文档版本"
                  value={version || doc.version}
                  onChange={(e) => setVersion(Number(e.target.value))}
                >
                  {(versions.length ? versions : [{ version: doc.version }]).map((v) => (
                    <option key={v.version} value={v.version}>
                      版本 v{v.version}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="icon-button"
                aria-label="复制正文"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(doc.content);
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1800);
                  } catch {
                    setError('浏览器无法访问剪贴板，请手动选择正文复制');
                  }
                }}
              >
                {copied ? <Check size={16} /> : <Copy size={16} />}
              </button>
            </div>
            {doc.capture_id && (
              <a
                className="text-button raw-download"
                href={`/api/captures/${doc.capture_id}/raw`}
                download
              >
                <Download size={14} />
                下载原始文件
              </a>
            )}
            <div className="reader-body">
              {doc.blocks?.length ? (
                doc.blocks.map((block, index) => (
                  <section
                    key={block.id}
                    ref={(el) => {
                      if (block.id === focusBlockId) focusRef.current = el;
                    }}
                    className={`reader-block ${focusBlockId === block.id ? 'highlighted' : ''} ${chosen?.id === block.id ? 'chosen' : ''}`}
                  >
                    <div className="block-gutter">
                      <span>{String(index + 1).padStart(2, '0')}</span>
                      <button
                        title="引用这一段"
                        aria-label={`引用第 ${index + 1} 段`}
                        onClick={() => {
                          setChosen(block);
                          setQuote(block.text);
                          setSaved('');
                        }}
                      >
                        <Quote size={14} />
                      </button>
                    </div>
                    <div className="block-copy">
                      {focusBlockId === block.id &&
                      focusQuote &&
                      block.text.includes(focusQuote) ? (
                        <HighlightedQuote
                          text={block.text}
                          quote={focusQuote}
                          startOffset={focusStartOffset}
                        />
                      ) : (
                        <SafeText text={block.text} />
                      )}
                    </div>
                  </section>
                ))
              ) : (
                <SafeText text={doc.content || '此版本暂无正文。'} />
              )}
            </div>
            {chosen && (
              <div className="quote-composer">
                <div className="section-heading">
                  <h3>
                    <Quote size={16} />
                    保存证据片段
                  </h3>
                  <button className="text-button" onClick={() => setChosen(null)}>
                    取消
                  </button>
                </div>
                <label className="sr-only" htmlFor="selected-quote">
                  引用原文
                </label>
                <textarea
                  id="selected-quote"
                  value={quote}
                  onChange={(e) => {
                    setQuote(e.target.value);
                    setSaved('');
                  }}
                  rows={3}
                />
                <div className="quote-actions">
                  <span>可缩短为原文中的连续片段；保存时会验证出处</span>
                  <button
                    className="button primary small"
                    disabled={saving || !!saved || !quote.trim()}
                    onClick={save}
                  >
                    {saving ? (
                      <LoaderCircle size={14} className="spin" />
                    ) : saved ? (
                      <Check size={14} />
                    ) : (
                      <Quote size={14} />
                    )}{' '}
                    {saved ? '证据已保存' : '保存证据'}
                  </button>
                </div>
              </div>
            )}
            {doc.kind === 'report' && (
              <details className="section-locks">
                <summary>
                  <LockKeyhole size={14} />
                  人工保护章节
                </summary>
                <p>勾选后，提案不得修改这些章节。仅在最新版本上调整保护设置。</p>
                {Array.from(
                  new Set(
                    Array.from(doc.content.matchAll(/^#{1,6}\s+(.+?)\s*#*\s*$/gm)).map((m) => m[1]),
                  ),
                ).map((heading) => (
                  <label className="checkbox-label" key={heading}>
                    <input
                      type="checkbox"
                      disabled={
                        lockBusy || !!(version && versions[0] && version !== versions[0].version)
                      }
                      checked={doc.locked_sections?.includes(heading) || false}
                      onChange={async (e) => {
                        setLockBusy(true);
                        setError('');
                        try {
                          const sections = e.target.checked
                            ? [...(doc.locked_sections || []), heading]
                            : (doc.locked_sections || []).filter((h) => h !== heading);
                          const next = await api<Document>(`/documents/${documentId}/locks`, {
                            method: 'PATCH',
                            body: JSON.stringify({ sections }),
                          });
                          setDoc((prev) =>
                            prev ? { ...prev, locked_sections: next.locked_sections } : prev,
                          );
                        } catch (e) {
                          setError(errorText(e));
                        } finally {
                          setLockBusy(false);
                        }
                      }}
                    />
                    <span>{heading}</span>
                  </label>
                ))}
              </details>
            )}
            <div className="reader-footer">
              <ShieldCheck size={14} />
              原始快照保留 · 内容以安全文本呈现
            </div>
          </>
        )
      )}
    </Modal>
  );
}
