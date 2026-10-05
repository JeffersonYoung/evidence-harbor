'use client';
import { useState, useEffect } from 'react';
import {
  Activity,
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  Layers,
  CircleDot,
  RefreshCw,
} from 'lucide-react';
import {
  type Operation,
  type Run,
  type RunEvent,
  api,
  post,
  displayDate,
  errorText,
} from '@/lib/api';
import { Status, Empty, Loading, ErrorBox } from './ui';
function RunDetails({ id }: { id: string }) {
  const [events, setEvents] = useState<RunEvent[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    api<RunEvent[] | { events: RunEvent[] }>(`/research-runs/${id}/events`)
      .then((d) => active && setEvents(Array.isArray(d) ? d : d.events))
      .catch((e) => active && setError(errorText(e)));
    return () => {
      active = false;
    };
  }, [id]);
  return (
    <div className="run-detail">
      {error ? (
        <ErrorBox>{error}</ErrorBox>
      ) : events === null ? (
        <Loading label="读取研究事件" />
      ) : events.length ? (
        events.map((e) => (
          <div className="event" key={e.id || e.sequence}>
            <CircleDot size={13} />
            <div>
              <strong>{e.type}</strong>
              <span>{displayDate(e.created_at)}</span>
              <pre>{JSON.stringify(e.data, null, 2)}</pre>
            </div>
          </div>
        ))
      ) : (
        <p className="muted">当前还没有研究事件。</p>
      )}
    </div>
  );
}
export default function ActivityView({
  operations,
  runs,
  onReport,
  onOperation,
}: {
  operations: Operation[];
  runs: Run[];
  onReport: () => void;
  onOperation: (o: Operation) => void;
}) {
  const [expanded, setExpanded] = useState('');
  const [filter, setFilter] = useState('all');
  const [retryError, setRetryError] = useState('');
  const [retrying, setRetrying] = useState('');
  const entries = [
    ...operations.map((o) => ({ ...o, entryType: 'operation' as const })),
    ...runs.map((r) => ({ ...r, entryType: 'run' as const })),
  ]
    .filter((i) => filter === 'all' || i.entryType === filter)
    .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());
  return (
    <section>
      <div className="view-heading">
        <div>
          <span className="eyebrow">EVERY STEP, ACCOUNTED FOR</span>
          <h1>运行记录</h1>
          <p>从资料导入到提案生成，每一步都留有记录。</p>
        </div>
        <span className="live-indicator">
          <span />
          进行中的任务自动更新
        </span>
      </div>
      {retryError && <ErrorBox>{retryError}</ErrorBox>}
      <div className="inline-tabs">
        {[
          ['all', '全部记录'],
          ['operation', '资料导入'],
          ['run', '研究任务'],
        ].map(([id, name]) => (
          <button
            className={filter === id ? 'selected' : ''}
            key={id}
            onClick={() => setFilter(id)}
          >
            {name}
          </button>
        ))}
      </div>
      {entries.length ? (
        <div className="activity-list">
          {entries.map((entry) => (
            <article key={`${entry.entryType}-${entry.id}`} className="activity-item">
              <button
                className="activity-summary"
                aria-expanded={expanded === entry.id}
                onClick={() => setExpanded(expanded === entry.id ? '' : entry.id)}
              >
                <span className="activity-icon">
                  {entry.entryType === 'run' ? <FlaskConical size={18} /> : <Layers size={18} />}
                </span>
                <span className="activity-name">
                  <strong>
                    {entry.entryType === 'run'
                      ? (entry as Run).question
                      : (entry as Operation).kind === 'research'
                        ? '研究操作'
                        : '资料导入'}
                  </strong>
                  <small>
                    {displayDate(entry.created_at)} · {entry.id.slice(0, 8)}
                  </small>
                </span>
                <Status value={entry.status} />
                {expanded === entry.id ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
              </button>
              {expanded === entry.id && (
                <div>
                  {entry.error && <ErrorBox>{entry.error}</ErrorBox>}
                  {entry.entryType === 'run' ? (
                    <>
                      <RunDetails id={entry.id} />
                      {entry.status === 'succeeded' || entry.status === 'completed' ? (
                        <button className="text-button activity-link" onClick={onReport}>
                          查看研究提案 <ArrowUpRight size={14} />
                        </button>
                      ) : null}
                    </>
                  ) : (
                    <div className="run-detail">
                      <span className="micro-label">OPERATION RESULT</span>
                      {entry.status === 'failed' && (
                        <button
                          className="button secondary small"
                          disabled={retrying === entry.id}
                          onClick={async () => {
                            setRetrying(entry.id);
                            setRetryError('');
                            try {
                              const op = await post<Operation>(`/operations/${entry.id}/retry`, {});
                              onOperation(op);
                            } catch (e) {
                              setRetryError(errorText(e));
                            } finally {
                              setRetrying('');
                            }
                          }}
                        >
                          <RefreshCw size={13} />
                          重试这项操作
                        </button>
                      )}
                      <pre>
                        {JSON.stringify(entry.result_json || { status: entry.status }, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              )}
            </article>
          ))}
        </div>
      ) : (
        <Empty icon={<Activity size={27} />} title="研究的足迹，会留在这里">
          添加第一份资料或开始一次研究后，可以在这里查看处理进度、结果与失败原因。
        </Empty>
      )}
    </section>
  );
}
