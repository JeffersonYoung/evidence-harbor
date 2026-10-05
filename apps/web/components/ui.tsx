'use client';
import { useEffect, useRef, type ReactNode } from 'react';
import { X, LoaderCircle, Check, AlertCircle, Clock3, ArrowUpRight, FileText } from 'lucide-react';
import { isPending } from '@/lib/api';
export function Status({ value }: { value: string }) {
  const status: Record<string, string> = {
    pending: '排队中',
    running: '处理中',
    retrying: '重试中',
    queued: '排队中',
    succeeded: '已完成',
    completed: '已完成',
    failed: '失败',
    open: '待研究',
    answered: '已有回答',
    published: '已发布',
    draft: '草稿',
    ready: '待审核',
    rejected: '已拒绝',
  };
  return (
    <span className={`status status-${value}`}>
      {isPending(value) ? (
        <LoaderCircle size={12} className="spin" />
      ) : ['succeeded', 'completed', 'published', 'answered'].includes(value) ? (
        <Check size={12} />
      ) : value === 'failed' ? (
        <AlertCircle size={12} />
      ) : (
        <Clock3 size={12} />
      )}{' '}
      {status[value] || value}
    </span>
  );
}
export function Empty({
  icon,
  title,
  children,
  action,
}: {
  icon?: ReactNode;
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">{icon || <FileText size={25} />}</div>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function ErrorBox({ children, retry }: { children: ReactNode; retry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <AlertCircle size={17} />
      <div>{children}</div>
      {retry && (
        <button className="text-button" onClick={retry}>
          重试 <ArrowUpRight size={14} />
        </button>
      )}
    </div>
  );
}
export function Loading({ label = '正在加载' }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle size={19} className="spin" />
      {label}…
    </div>
  );
}
export function Modal({
  title,
  kicker,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  kicker?: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    d.showModal();
    return () => d.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`dialog ${wide ? 'dialog-wide' : ''}`}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          const r = e.currentTarget.getBoundingClientRect();
          if (
            e.clientX < r.left ||
            e.clientX > r.right ||
            e.clientY < r.top ||
            e.clientY > r.bottom
          )
            onClose();
        }
      }}
    >
      <header className="dialog-header">
        <div>
          {kicker && <span className="eyebrow">{kicker}</span>}
          <h2>{title}</h2>
        </div>
        <button aria-label="关闭对话框" className="icon-button" onClick={onClose}>
          <X size={20} />
        </button>
      </header>
      {children}
    </dialog>
  );
}
// All input remains React text. Never interpret source HTML, scripts, or unsanitized markup.
export function SafeText({ text }: { text: string }) {
  return (
    <div className="safe-text">
      {text.split(/\n\s*\n/).map((part, i) => {
        if (/^#{1,4}\s/.test(part)) {
          const cleaned = part.replace(/^#{1,4}\s/, '');
          return <h3 key={i}>{cleaned}</h3>;
        }
        return <p key={i}>{part}</p>;
      })}
    </div>
  );
}
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}

/** Stored evidence offsets are Unicode code points, as used by the Python API.
 * Convert an explicit anchor to UTF-16 only after validating it. Never silently
 * fall back to a different occurrence when an explicit anchor is invalid.
 * Normalization would alter immutable provenance, so NFC and NFD stay distinct. */
export function HighlightedQuote({
  text,
  quote,
  startOffset,
}: {
  text: string;
  quote: string;
  startOffset?: number;
}) {
  let index = -1;
  if (quote) {
    if (startOffset === undefined) {
      index = text.indexOf(quote);
    } else {
      const codePoints = Array.from(text);
      if (
        Number.isSafeInteger(startOffset) &&
        startOffset >= 0 &&
        startOffset <= codePoints.length
      ) {
        const utf16Index = codePoints.slice(0, startOffset).join('').length;
        if (text.slice(utf16Index, utf16Index + quote.length) === quote) index = utf16Index;
      }
    }
  }
  if (index < 0) return <p>{text}</p>;
  return (
    <p>
      {text.slice(0, index)}
      <mark>{quote}</mark>
      {text.slice(index + quote.length)}
    </p>
  );
}
