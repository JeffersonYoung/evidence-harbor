'use client';
import { useState, useEffect } from 'react';
import {
  ArrowUpRight,
  FileText,
  Plus,
  Quote,
  Check,
  ShieldCheck,
  AlertTriangle,
  GitBranch,
  LoaderCircle,
  PenLine,
  BookOpen,
  ArrowRight,
} from 'lucide-react';
import { type Document, type Proposal, post, errorText, ApiError, displayDate } from '@/lib/api';
import { Empty, SafeText, ErrorBox, Status } from './ui';
export default function Report({
  documents,
  proposals,
  onRefresh,
  onDraft,
  onRead,
  onEvidence,
  onResearch,
  onLibrary,
}: {
  documents: Document[];
  proposals: Proposal[];
  onRefresh: () => void;
  onDraft: (target?: Document) => void;
  onRead: (id: string) => void;
  onEvidence: (id: string) => void;
  onResearch: () => void;
  onLibrary: () => void;
}) {
  const reports = documents.filter((d) => d.kind === 'report');
  const pending = proposals.filter((p) => p.status !== 'published' && p.status !== 'rejected');
  const [selection, setSelection] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [accepted, setAccepted] = useState<number[]>([]);
  const current =
    pending.find((p) => p.id === selection) ||
    reports.find((d) => d.id === selection) ||
    pending[0] ||
    reports[0];
  const isProposal = current && 'claims' in current;
  useEffect(() => {
    setAccepted(current && 'claims' in current ? current.claims.map((_, i) => i) : []);
  }, [current?.id]);
  async function publish(p: Proposal) {
    setBusy(true);
    setError('');
    setConflict(false);
    try {
      const doc = await post<Document>(`/proposals/${p.id}/publish`, {
        ...(p.base_version != null ? { expected_version: p.base_version } : {}),
        ...(accepted.length !== p.claims.length ? { accepted_claim_indices: accepted } : {}),
      });
      setSelection(doc.id);
      onRefresh();
    } catch (e) {
      setError(errorText(e));
      setConflict(e instanceof ApiError && e.status === 409);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="report-view">
      <div className="view-heading">
        <div>
          <span className="eyebrow">FROM EVIDENCE TO INSIGHT</span>
          <h1>研究报告</h1>
          <p>将碎片连成线索，让结论有据可循。</p>
        </div>
        <button className="button secondary" onClick={() => onDraft()}>
          <PenLine size={16} />
          起草报告
        </button>
      </div>
      {current ? (
        <>
          {pending.length > 0 && (
            <div className="review-banner">
              <div className="review-symbol">
                <GitBranch size={19} />
              </div>
              <div>
                <strong>{pending.length} 份提案等待你的审核</strong>
                <p>查看研究内容与证据，确认后发布到项目。</p>
              </div>
              <button
                className="text-button"
                onClick={() => {
                  setSelection(pending[0].id);
                  setError('');
                }}
              >
                查看提案 <ArrowRight size={16} />
              </button>
            </div>
          )}
          <div className="report-layout">
            <article className="report-paper">
              <div className="paper-meta">
                <span>{isProposal ? '待审核提案' : '已发布研究报告'}</span>
                <span>{displayDate(current.created_at)}</span>
              </div>
              <h2>{current.title}</h2>
              <div className="paper-byline">
                <span className="tiny-mark">EH</span>
                <span>EvidenceHarbor 研究工作空间</span>
                <span>·</span>
                {isProposal ? (
                  <Status value={current.status} />
                ) : (
                  <span>版本 v{(current as Document).version}</span>
                )}
              </div>
              <SafeText text={current.content} />
              {isProposal && (
                <div className="claims-section">
                  <h3>
                    结论与证据 <small className="review-hint">勾选接受的结论</small>
                    <span>{(current as Proposal).claims.length}</span>
                  </h3>
                  {(current as Proposal).claims.length ? (
                    (current as Proposal).claims.map((claim, index) => (
                      <div className="claim" key={index}>
                        <label className="claim-number">
                          <input
                            aria-label={`接受第 ${index + 1} 条结论`}
                            type="checkbox"
                            checked={accepted.includes(index)}
                            onChange={(e) =>
                              setAccepted(
                                e.target.checked
                                  ? [...accepted, index]
                                  : accepted.filter((i) => i !== index),
                              )
                            }
                          />
                          {String(index + 1).padStart(2, '0')}
                        </label>
                        <div>
                          <p>{claim.text}</p>
                          {claim.scope && (
                            <small className="claim-scope">适用范围：{claim.scope}</small>
                          )}
                          {claim.as_of && (
                            <small className="claim-scope">截至：{claim.as_of}</small>
                          )}
                          {claim.limitations?.map((l, i) => (
                            <p key={i} className="claim-limitation">
                              限制：{l}
                            </p>
                          ))}
                          <div className="evidence-pills">
                            {claim.evidence_ids.map((id, i) => (
                              <button key={id} onClick={() => onEvidence(id)}>
                                <Quote size={12} />
                                {claim.evidence_links?.find((l) => l.evidence_id === id)
                                  ?.relation === 'contradicting'
                                  ? '反证'
                                  : claim.evidence_links?.find((l) => l.evidence_id === id)
                                        ?.relation === 'limiting'
                                    ? '限制'
                                    : '证据'}{' '}
                                {i + 1}
                                <ArrowUpRight size={12} />
                              </button>
                            ))}
                            {!claim.evidence_ids.length && (
                              <span className="warning-text">这条结论还没有关联证据</span>
                            )}
                          </div>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="subtle-note">
                      <AlertTriangle size={15} />
                      这份手动草稿没有关联证据，请在发布前独立核查内容
                    </div>
                  )}
                </div>
              )}
              {error && (
                <ErrorBox>
                  {conflict ? (
                    <>
                      <strong>
                        {/lock|protect|section/i.test(error)
                          ? '受保护章节阻止了发布'
                          : '报告已有新版本，发布已被阻止'}
                      </strong>
                      <p>
                        {/lock|protect|section/i.test(error)
                          ? '提案修改了人工保护的章节。请保留原文重新起草，或在版本阅读器中核查章节保护设置；原提案仍然保留。'
                          : '这份提案基于较早版本。请查看当前报告，并以最新版本重新起草修订；原提案仍然保留。'}
                      </p>
                      <button
                        className="text-button"
                        onClick={() => {
                          onRefresh();
                          const target = reports.find(
                            (d) => d.id === (current as Proposal).target_document_id,
                          );
                          if (target) onRead(target.id);
                        }}
                      >
                        查看当前版本 <ArrowUpRight size={14} />
                      </button>
                    </>
                  ) : (
                    error
                  )}
                </ErrorBox>
              )}
              <footer className="paper-footer">
                {isProposal ? (
                  <>
                    <span>
                      <ShieldCheck size={16} />
                      审核通过后才会写入正式文档
                      {(current as Proposal).base_version != null
                        ? ` · 基于 v${(current as Proposal).base_version}`
                        : ''}
                    </span>
                    <button
                      className="button secondary small"
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        setError('');
                        try {
                          await post(`/proposals/${current.id}/reject`, {});
                          setSelection('');
                          onRefresh();
                        } catch (e) {
                          setError(errorText(e));
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      拒绝提案
                    </button>
                    <button
                      className="button primary"
                      disabled={busy || conflict || !accepted.length}
                      onClick={() => publish(current as Proposal)}
                    >
                      {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}
                      审核并发布
                      {accepted.length !== (current as Proposal).claims.length
                        ? `（${accepted.length} 条）`
                        : null}
                    </button>
                  </>
                ) : (
                  <>
                    <span>
                      <ShieldCheck size={16} />
                      已保存到项目 · 版本历史可追溯
                    </span>
                    <div className="button-group">
                      <button className="button secondary small" onClick={() => onRead(current.id)}>
                        <BookOpen size={14} />
                        版本历史
                      </button>
                      <button
                        className="button secondary small"
                        onClick={() => onDraft(current as Document)}
                      >
                        <PenLine size={14} />
                        起草修订
                      </button>
                    </div>
                  </>
                )}
              </footer>
            </article>
            <aside className="report-index">
              <span className="micro-label">项目文档</span>
              {pending.map((p) => (
                <button
                  className={current.id === p.id ? 'selected' : ''}
                  onClick={() => {
                    setSelection(p.id);
                    setError('');
                    setConflict(false);
                  }}
                  key={p.id}
                >
                  <GitBranch size={15} />
                  <span>
                    {p.title}
                    <small>待审核提案</small>
                  </span>
                </button>
              ))}
              {reports.map((d) => (
                <button
                  className={current.id === d.id ? 'selected' : ''}
                  onClick={() => {
                    setSelection(d.id);
                    setError('');
                    setConflict(false);
                  }}
                  key={d.id}
                >
                  <FileText size={15} />
                  <span>
                    {d.title}
                    <small>已发布 · v{d.version}</small>
                  </span>
                </button>
              ))}
              <div className="aside-note">
                <Quote size={20} />
                <p>
                  好的研究，始于问题。
                  <br />
                  可信的结论，始于证据。
                </p>
                <button className="text-button" onClick={onLibrary}>
                  回到资料库 <ArrowUpRight size={14} />
                </button>
              </div>
            </aside>
          </div>
        </>
      ) : (
        <div className="report-empty-grid">
          <div className="report-empty-paper">
            <span className="paper-number">01 / START WITH A QUESTION</span>
            <div className="empty-illustration">
              <div />
              <div />
              <div />
              <Quote size={30} />
            </div>
            <h2>
              你的下一项洞见，
              <br />
              从这里开始。
            </h2>
            <p>
              添加资料，提出一个值得回答的问题。
              <br />
              证据港会把相关出处整理为提案，等你审核。
            </p>
            <div className="button-group">
              <button className="button primary" onClick={onResearch}>
                开始研究 <ArrowRight size={16} />
              </button>
              <button className="button secondary" onClick={onLibrary}>
                查看资料库
              </button>
            </div>
            <div className="empty-paper-bottom">
              <ShieldCheck size={15} />
              每一个结论，都应该有一个可追溯的起点
            </div>
          </div>
          <aside className="workflow-aside">
            <span className="micro-label">一个更清晰的研究流程</span>
            {[
              ['01', '收集资料', '保存网页、文件与笔记的原始快照'],
              ['02', '提出问题', '从现有资料中检索与定位证据'],
              ['03', '审核结论', '核查引用，确认提案，再发布报告'],
            ].map(([n, t, s]) => (
              <div key={n}>
                <span>{n}</span>
                <h3>{t}</h3>
                <p>{s}</p>
              </div>
            ))}
            <button className="text-button" onClick={() => onDraft()}>
              <Plus size={15} />
              也可以从手动起草开始
            </button>
          </aside>
        </div>
      )}
    </section>
  );
}
