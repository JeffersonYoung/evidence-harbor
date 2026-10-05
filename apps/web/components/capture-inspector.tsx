'use client';
import { useEffect, useState } from 'react';
import { Download, Archive } from 'lucide-react';
import { api, errorText, displayDate, type Capture } from '@/lib/api';
import { ErrorBox, Loading, Modal, Status } from './ui';

export function CaptureDownload({ captureId }: { captureId: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <div>
      <button
        className="button secondary small"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError('');
          try {
            const response = await fetch(`/api/captures/${encodeURIComponent(captureId)}/raw`, {
              cache: 'no-store',
            });
            if (!response.ok) {
              const payload = await response.json().catch(() => ({}));
              throw new Error(
                response.status === 403
                  ? '当前账户没有下载这份原件的权限。'
                  : typeof payload.detail === 'string'
                    ? payload.detail
                    : '原件下载失败，请检查登录状态后重试。',
              );
            }
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url;
            link.download = `capture-${captureId}.bin`;
            document.body.appendChild(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Download size={14} />
        {busy ? '正在下载原件…' : '下载留存原件'}
      </button>
      {error && <ErrorBox>{error}</ErrorBox>}
    </div>
  );
}

export function CaptureDetails({ capture }: { capture: Capture }) {
  return (
    <div className="asset-detail">
      <p className="scope-note">
        <Archive size={16} />
        {capture.processing_ready
          ? '原件已归档，已有解析结果。解析完成不代表已通读全文。'
          : '原件已归档，尚无可用解析结果；不能作为已就绪的研究文档或证据。'}
      </p>
      <CaptureDownload captureId={capture.id} />
      <dl className="asset-metadata">
        <dt>采集时间</dt>
        <dd>{displayDate(capture.fetched_at)}</dd>
        <dt>格式 / 大小</dt>
        <dd>
          {capture.media_type} · {capture.byte_size.toLocaleString('zh-CN')} 字节
        </dd>
        <dt>原件 SHA-256</dt>
        <dd>{capture.content_hash}</dd>
        <dt>采集标识</dt>
        <dd>{capture.id}</dd>
        <dt>来源标识</dt>
        <dd>{capture.source_id}</dd>
      </dl>
      <h3>处理记录</h3>
      {capture.processing_operations.length ? (
        capture.processing_operations.map((op) => (
          <div className="asset-history-item" key={op.id}>
            <Status value={op.status} />
            <p className="asset-id">{op.id}</p>
            {op.error && <ErrorBox>{op.error}</ErrorBox>}
          </div>
        ))
      ) : (
        <p className="muted">没有可展示的处理记录。</p>
      )}
      <details className="asset-history">
        <summary>不可变采集元数据与观察记录</summary>
        <pre>
          {JSON.stringify(
            { metadata: capture.metadata_json, observations: capture.observations },
            null,
            2,
          )}
        </pre>
      </details>
      <details className="asset-history">
        <summary>解析版本 · {capture.representations.length}</summary>
        <pre>{JSON.stringify(capture.representations, null, 2)}</pre>
      </details>
    </div>
  );
}

export default function CaptureInspector({
  captureId,
  onClose,
}: {
  captureId: string;
  onClose: () => void;
}) {
  const [capture, setCapture] = useState<Capture | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    setCapture(null);
    setError('');
    api<Capture>(`/captures/${encodeURIComponent(captureId)}`)
      .then((data) => active && setCapture(data))
      .catch((e) => active && setError(errorText(e)));
    return () => {
      active = false;
    };
  }, [captureId, reload]);
  return (
    <Modal title="留存原件" kicker="ARCHIVED CAPTURE / 采集档案" onClose={onClose} wide>
      {error ? (
        <ErrorBox retry={() => setReload((n) => n + 1)}>{error}</ErrorBox>
      ) : capture ? (
        <CaptureDetails capture={capture} />
      ) : (
        <Loading label="读取采集档案" />
      )}
    </Modal>
  );
}
