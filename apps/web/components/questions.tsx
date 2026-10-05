'use client';
import { useState } from 'react';
import { Plus, ArrowUpRight, MessageCircleQuestion, Search, LoaderCircle } from 'lucide-react';
import { type Question, post, errorText } from '@/lib/api';
import { Empty, ErrorBox, Status, SafeText } from './ui';
export default function Questions({
  projectId,
  questions,
  onRefresh,
  onResearch,
  onEvidence,
}: {
  projectId: string;
  questions: Question[];
  onRefresh: () => void;
  onResearch: (text: string) => void;
  onEvidence: (id: string) => void;
}) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <section>
      <div className="view-heading">
        <div>
          <span className="eyebrow">ASK BETTER QUESTIONS</span>
          <h1>研究问题</h1>
          <p>把还不知道的事情写下来，下一步会更清楚。</p>
        </div>
        <span className="count-summary">{questions.length} 个问题</span>
      </div>
      <form
        className="question-composer"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError('');
          try {
            await post<Question>('/questions', { project_id: projectId, text: text.trim() });
            setText('');
            onRefresh();
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <MessageCircleQuestion size={21} />
        <label className="sr-only" htmlFor="new-question">
          新研究问题
        </label>
        <input
          id="new-question"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="有什么问题，值得进一步研究？"
          required
          maxLength={10000}
        />
        <button className="button primary small" disabled={busy || !text.trim()}>
          {busy ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />}添加问题
        </button>
      </form>
      {error && <ErrorBox>{error}</ErrorBox>}
      {questions.length ? (
        <div className="question-list">
          {questions.map((q, index) => (
            <article key={q.id}>
              <div className="question-number">Q{String(index + 1).padStart(2, '0')}</div>
              <div className="question-copy">
                <div className="section-heading">
                  <h3>{q.text}</h3>
                  <Status value={q.status} />
                </div>
                {q.answer ? (
                  <SafeText text={q.answer} />
                ) : (
                  <p className="muted">尚未形成回答 · 可以从当前资料开始研究</p>
                )}
                <div className="question-actions">
                  <div className="evidence-pills">
                    {q.evidence_ids?.map((id, i) => (
                      <button key={id} onClick={() => onEvidence(id)}>
                        证据 {i + 1}
                        <ArrowUpRight size={12} />
                      </button>
                    ))}
                  </div>
                  <button className="text-button" onClick={() => onResearch(q.text)}>
                    <Search size={14} />
                    研究这个问题 <ArrowUpRight size={14} />
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <Empty icon={<MessageCircleQuestion size={26} />} title="留一个问题，给下一步">
          研究问题会保存在项目中。带着明确的问题检索资料，更容易发现证据的边界。
        </Empty>
      )}
    </section>
  );
}
