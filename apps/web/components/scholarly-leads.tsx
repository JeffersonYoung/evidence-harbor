'use client';
import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, BookOpen, ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
import {
  api,
  post,
  ApiError,
  errorText,
  displayDate,
  safeHttpUrl,
  type DiscoveredWork,
  type DiscoveredPage,
  type ScholarlyMetadata,
} from '@/lib/api';
import { Empty, ErrorBox, Field, Loading, Modal, SafeText } from './ui';
import { CaptureDownload } from './capture-inspector';

const PAGE_SIZE = 50;
export function WorkScope({ work }: { work: DiscoveredWork }) {
  return (
    <div className="asset-badges">
      <span className={`asset-chip ${work.fulltext_ready ? 'asset-ready' : ''}`}>
        {work.fulltext_ready
          ? '全文已留存可用'
          : work.content_scope === 'abstract'
            ? '仅摘要'
            : '仅元数据'}
      </span>
      <span className="asset-chip">未确认通读</span>
      <span className="asset-chip">
        {work.evidence_eligible
          ? work.fulltext_ready
            ? '可从留存正文取证'
            : '取证范围仅限留存摘要'
          : '尚不可作为证据'}
      </span>
    </div>
  );
}

function MetadataDisplay({
  metadata,
  includeAbstract = false,
}: {
  metadata: ScholarlyMetadata;
  includeAbstract?: boolean;
}) {
  return (
    <dl className="asset-metadata">
      <dt>标题</dt>
      <dd>{metadata.title}</dd>
      <dt>作者</dt>
      <dd>{metadata.authors.join('、') || '未提供'}</dd>
      <dt>年份</dt>
      <dd>{metadata.year ?? '未提供'}</dd>
      <dt>期刊 / 会议</dt>
      <dd>{metadata.venue || '未提供'}</dd>
      {includeAbstract && (
        <>
          <dt>供应方原始摘要</dt>
          <dd>
            <SafeText text={metadata.abstract || '未提供'} />
          </dd>
        </>
      )}
    </dl>
  );
}

function SourceLink({ url, children }: { url?: string | null; children: React.ReactNode }) {
  const href = url && safeHttpUrl(url);
  return href ? (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children} <ArrowUpRight size={13} />
    </a>
  ) : null;
}

export function MetadataReviewForm({
  work,
  onSaved,
  onReload,
}: {
  work: DiscoveredWork;
  onSaved: (work: DiscoveredWork) => void;
  onReload: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [abstractEdited, setAbstractEdited] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const submitting = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return (
    <form
      className="metadata-review-form"
      onSubmit={async (event) => {
        event.preventDefault();
        if (submitting.current || conflict) return;
        const data = new FormData(event.currentTarget);
        const title = String(data.get('title') || '').trim();
        const authors = String(data.get('authors') || '')
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean);
        const yearText = String(data.get('year') || '').trim();
        const venue = String(data.get('venue') || '').trim();
        const abstract = String(data.get('abstract') ?? work.abstract);
        const changes: Partial<ScholarlyMetadata> = {};
        if (abstractEdited && abstract !== work.abstract) {
          if (!abstract.trim()) {
            setError('摘要更正必须提供非空替换文本；不能在此清空原摘要。');
            return;
          }
          changes.abstract = abstract;
        }
        if (title !== work.title) changes.title = title;
        if (JSON.stringify(authors) !== JSON.stringify(work.authors)) changes.authors = authors;
        if (yearText && Number(yearText) !== work.year) changes.year = Number(yearText);
        if (venue && venue !== (work.venue || '')) changes.venue = venue;
        setError('');
        if (
          changes.authors &&
          (!changes.authors.length ||
            changes.authors.length > 200 ||
            changes.authors.some((name) => name.length > 500))
        ) {
          setError('更正作者时需填写 1–200 位作者，每位不超过 500 字符。');
          return;
        }
        if ((work.year !== null && !yearText) || (work.venue && !venue)) {
          setError('当前接口不支持清空已有年份或期刊，请填写更正值。');
          return;
        }
        if (!Object.keys(changes).length) {
          setError('请至少更正一项元数据。');
          return;
        }
        const sourceUrl = String(data.get('source_url') || '').trim();
        if (!safeHttpUrl(sourceUrl)) {
          setError('请提供可核验的 http 或 https 来源链接。');
          return;
        }
        submitting.current = true;
        setBusy(true);
        try {
          const updated = await post<DiscoveredWork>(
            `/discovered-works/${encodeURIComponent(work.id)}/metadata-reviews`,
            {
              expected_revision: work.review_revision,
              changes,
              reason: String(data.get('reason') || '').trim(),
              source_url: sourceUrl,
            },
          );
          if (mounted.current) onSaved(updated);
        } catch (e) {
          if (mounted.current) {
            setConflict(e instanceof ApiError && e.status === 409);
            setError(
              e instanceof ApiError && e.status === 409
                ? '元数据已被其他编辑更新。请重新加载最新版本，再核对并提交更正。'
                : errorText(e),
            );
          }
        } finally {
          submitting.current = false;
          if (mounted.current) setBusy(false);
        }
      }}
    >
      <p className="dialog-intro">
        更正会追加审核记录，并保留供应方原始信息。DOI / arXiv 标识不在此处修改。
      </p>
      <Field label="标题">
        <input name="title" defaultValue={work.title} required maxLength={2000} />
      </Field>
      <Field label="作者（每行一位）">
        <textarea name="authors" defaultValue={work.authors.join('\n')} rows={3} />
      </Field>
      <div className="asset-form-grid">
        <Field label="年份">
          <input name="year" type="number" min={1000} max={2200} defaultValue={work.year ?? ''} />
        </Field>
        <Field label="期刊 / 会议">
          <input name="venue" maxLength={1000} defaultValue={work.venue || ''} />
        </Field>
      </div>
      <Field label="摘要（元数据）">
        <textarea
          name="abstract"
          onChange={() => setAbstractEdited(true)}
          aria-label="摘要（元数据）"
          defaultValue={work.abstract}
          maxLength={100000}
          rows={6}
        />
      </Field>
      <p className="scope-note">
        摘要更正仅改变目录展示，不认证全文阅读或期刊／预印本版本等价。请在理由中注明来源版本与限制。
      </p>
      <Field label="更正依据链接">
        <input name="source_url" type="url" required placeholder="https://…" />
      </Field>
      <Field label="更正理由">
        <textarea name="reason" minLength={5} maxLength={10000} required rows={3} />
      </Field>
      {error && <ErrorBox>{error}</ErrorBox>}
      <div className="button-group">
        {conflict && (
          <button className="button secondary" type="button" onClick={onReload}>
            重新加载最新版本
          </button>
        )}
        <button className="button primary" disabled={busy || conflict}>
          {busy ? '正在保存…' : '保存元数据更正'}
        </button>
      </div>
    </form>
  );
}

export function WorkDetails({
  work,
  role,
  onRead,
  onSaved,
  onReload,
}: {
  work: DiscoveredWork;
  role: string;
  onRead: (id: string) => void;
  onSaved: (work: DiscoveredWork) => void;
  onReload: () => void;
}) {
  const canReview = ['editor', 'admin'].includes(role);
  const accessLabels: Record<string, string> = {
    unknown: '未知',
    open_access: '开放获取',
    restricted: '受限',
    unavailable: '不可用',
  };
  return (
    <div className="asset-detail">
      <WorkScope work={work} />
      <p className="scope-note">
        线索与摘要不计作已读论文。全文可用仅表示已有经核验关联的留存资料，系统不认证通读完成。
      </p>
      <h3>当前展示元数据</h3>
      <p className="asset-review-status">
        {work.review_revision > 0
          ? `已更正 · 修订 ${work.review_revision}`
          : '供应方元数据 · 尚未审核'}
      </p>
      <MetadataDisplay metadata={work} />
      <p>
        供应方获取状态：{accessLabels[work.access_status] || work.access_status}（不代表采集成功）
      </p>
      <SourceLink url={work.source_url}>打开来源页面</SourceLink>
      {!!work.aliases?.length && (
        <details className="asset-history">
          <summary>DOI / arXiv 与去重标识</summary>
          <ul>
            {work.aliases.map((alias) => (
              <li className="asset-id" key={alias}>
                {alias}
              </li>
            ))}
          </ul>
        </details>
      )}
      {work.abstract && (
        <details className="asset-history" open={work.abstract_reviewed || undefined}>
          <summary>
            {work.abstract_reviewed
              ? '经审核的摘要展示（元数据，非全文证据）'
              : '供应方摘要（非全文）'}
          </summary>
          <SafeText text={work.abstract} />
          {work.abstract_reviewed && (
            <p className="scope-note">
              此处是摘要元数据，不是留存正文的证据锚点；不自动认证期刊／预印本版本等价，也不表示已经通读。
            </p>
          )}
          {work.abstract_review && (
            <div className="asset-history-item">
              <strong>摘要更正 · 修订 {work.abstract_review.revision}</strong>
              <p>{work.abstract_review.reason}</p>
              <SourceLink url={work.abstract_review.source_url}>摘要核验来源</SourceLink>
              {!!work.abstract_review.evidence_ids.length && (
                <p className="asset-id">核验依据：{work.abstract_review.evidence_ids.join('、')}</p>
              )}
            </div>
          )}
          {work.abstract_sha256 && (
            <p className="asset-id">摘要 SHA-256 · {work.abstract_sha256}</p>
          )}
        </details>
      )}
      <section className="asset-section">
        <h3>已关联留存资料 · {work.readings?.length || 0}</h3>
        {work.readings?.length ? (
          work.readings.map((reading) => (
            <article className="asset-history-item" key={reading.id}>
              <strong>{reading.content_scope === 'fulltext' ? '全文留存' : '摘要留存'}</strong>
              <p>{reading.review_note}</p>
              <div className="button-group">
                <button
                  className="button secondary small"
                  onClick={() => onRead(reading.document_id)}
                >
                  <BookOpen size={14} />
                  打开留存资料
                </button>
                <CaptureDownload captureId={reading.capture_id} />
              </div>
              <details className="asset-history">
                <summary>固定采集版本与来源绑定</summary>
                <pre>
                  {JSON.stringify(
                    {
                      capture_id: reading.capture_id,
                      representation_id: reading.representation_id,
                      provenance: reading.provenance_json,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </article>
          ))
        ) : (
          <p className="muted">
            尚未关联解析成功的摘要或全文。来源链接和开放获取标签不能替代留存原件。
          </p>
        )}
      </section>
      <details className="asset-history">
        <summary>供应方原始展示信息</summary>
        <MetadataDisplay metadata={work.provider_display || work} includeAbstract />
      </details>
      <details className="asset-history">
        <summary>原始来源与观察记录 · {work.observations?.length || 0}</summary>
        {work.observations?.map((observation) => (
          <article className="asset-history-item" key={observation.id}>
            <p>{displayDate(observation.created_at)}</p>
            <p className="asset-id">SHA-256 · {observation.payload_hash}</p>
            <pre>{JSON.stringify(observation.payload, null, 2)}</pre>
          </article>
        ))}
      </details>
      <details className="asset-history">
        <summary>元数据更正历史 · {work.metadata_reviews?.length || 0}</summary>
        {work.metadata_reviews?.length ? (
          work.metadata_reviews.map((review) => (
            <article className="asset-history-item" key={review.id}>
              <strong>
                修订 {review.revision} ·{' '}
                {review.actor_json.username || review.actor_json.user_id || '已授权编辑'}
                {review.actor_json.role && `（${review.actor_json.role}）`}
              </strong>
              <p>
                {displayDate(review.created_at)} · {review.reason}
              </p>
              <SourceLink url={review.source_url}>核验依据</SourceLink>
              {!!review.evidence_ids.length && (
                <p className="asset-id">证据：{review.evidence_ids.join('、')}</p>
              )}
              <pre>{JSON.stringify(review.overrides_json, null, 2)}</pre>
            </article>
          ))
        ) : (
          <p className="muted">暂无更正记录。</p>
        )}
      </details>
      {canReview ? (
        <details className="asset-history">
          <summary>更正展示元数据</summary>
          <MetadataReviewForm
            key={`${work.id}-${work.review_revision}`}
            work={work}
            onSaved={onSaved}
            onReload={onReload}
          />
        </details>
      ) : (
        <p className="muted">仅编辑与管理员可更正展示元数据。</p>
      )}
    </div>
  );
}

function WorkInspector({
  id,
  role,
  onClose,
  onRead,
  onSaved,
}: {
  id: string;
  role: string;
  onClose: () => void;
  onRead: (id: string) => void;
  onSaved: (work: DiscoveredWork) => void;
}) {
  const [work, setWork] = useState<DiscoveredWork | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let active = true;
    setWork(null);
    setError('');
    setSaved(false);
    api<DiscoveredWork>(`/discovered-works/${encodeURIComponent(id)}`)
      .then((data) => active && setWork(data))
      .catch((e) => active && setError(errorText(e)));
    return () => {
      active = false;
    };
  }, [id, reload]);
  return (
    <Modal wide title="学术线索档案" kicker="SCHOLARLY LEAD / 发现与核验" onClose={onClose}>
      {error ? (
        <ErrorBox retry={() => setReload((n) => n + 1)}>{error}</ErrorBox>
      ) : work ? (
        <>
          {saved && (
            <p role="status" className="scope-note">
              元数据更正已保存，原始观察与历史记录已保留。
            </p>
          )}
          <WorkDetails
            work={work}
            role={role}
            onRead={(documentId) => {
              onClose();
              onRead(documentId);
            }}
            onReload={() => setReload((n) => n + 1)}
            onSaved={(updated) => {
              setWork(updated);
              setSaved(true);
              onSaved(updated);
            }}
          />
        </>
      ) : (
        <Loading label="读取线索与来源记录" />
      )}
    </Modal>
  );
}

export default function ScholarlyLeads({
  projectId,
  role,
  onRead,
}: {
  projectId: string;
  role: string;
  onRead: (id: string) => void;
}) {
  const [page, setPage] = useState<DiscoveredPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    setPage(null);
    api<DiscoveredPage>(
      `/discovered-works?project_id=${encodeURIComponent(projectId)}&offset=${offset}&limit=${PAGE_SIZE}`,
    )
      .then((data) => active && setPage(data))
      .catch((e) => active && setError(errorText(e)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [projectId, offset, reload]);
  return (
    <div className="scholarly-leads">
      <p className="scope-note">
        这里收纳待核验的书目线索。元数据、摘要、已留存全文分别标注；条目数不代表已读论文数。
      </p>
      <div className="asset-page-heading">
        <p className="list-caption" aria-live="polite">
          {page
            ? `共 ${page.total.toLocaleString('zh-CN')} 条学术线索 · 每页 ${PAGE_SIZE} 条`
            : '学术线索目录'}
        </p>
        <button className="text-button" disabled={loading} onClick={() => setReload((n) => n + 1)}>
          <RefreshCw size={14} />
          刷新线索
        </button>
      </div>
      {error ? (
        <ErrorBox retry={() => setReload((n) => n + 1)}>{error}</ErrorBox>
      ) : loading ? (
        <Loading label="加载学术线索" />
      ) : page?.items.length ? (
        <div className="lead-list">
          {page.items.map((work) => (
            <article className="lead-card" key={work.id}>
              <button className="lead-title" onClick={() => setSelected(work.id)}>
                <strong>{work.title}</strong>
                <ArrowUpRight size={17} />
              </button>
              <p className="lead-byline">
                {work.authors.join('、') || '作者未提供'} · {work.year ?? '年份未提供'}
                {work.venue ? ` · ${work.venue}` : ''}
              </p>
              <WorkScope work={work} />
              <p className="asset-review-status">
                {work.review_revision > 0
                  ? `展示元数据已更正 · 修订 ${work.review_revision}`
                  : '供应方元数据待核验'}
              </p>
            </article>
          ))}
        </div>
      ) : (
        <Empty title="还没有学术线索">
          通过学术发现接口导入 DOI、arXiv 或书目信息后，可在这里查看来源、留存范围与核验记录。
        </Empty>
      )}
      {page && page.total > 0 && (
        <nav className="asset-pagination" aria-label="学术线索分页">
          <button
            className="button secondary small"
            disabled={loading || offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            <ChevronLeft size={15} />
            上一页
          </button>
          <span>
            {page.items.length ? `${page.offset + 1}–${page.offset + page.items.length}` : '0'} /{' '}
            {page.total.toLocaleString('zh-CN')}
          </span>
          <button
            className="button secondary small"
            disabled={loading || page.next_offset === null}
            onClick={() => page.next_offset !== null && setOffset(page.next_offset)}
          >
            下一页
            <ChevronRight size={15} />
          </button>
        </nav>
      )}
      {selected && (
        <WorkInspector
          key={selected}
          id={selected}
          role={role}
          onClose={() => setSelected('')}
          onRead={onRead}
          onSaved={(work) =>
            setPage((current) =>
              current
                ? {
                    ...current,
                    items: current.items.map((item) => (item.id === work.id ? work : item)),
                  }
                : current,
            )
          }
        />
      )}
    </div>
  );
}
