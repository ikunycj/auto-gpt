import { useEffect, useRef } from 'react';
import {
  AlertTriangle, Check, ChevronLeft, ChevronRight, CircleHelp, Copy, Download,
  LoaderCircle, MoreHorizontal, RefreshCw, Search, X,
} from 'lucide-react';

export const statusLabels = {
  available: '可用', used: '已使用', failed: '失败', disabled: '已禁用',
  pending: '排队中', running: '运行中', stopping: '停止中', success: '成功',
  completed: '完成', cancelled: '已取消', stopped: '已停止', partial_success: '部分成功',
  authorized: '已授权', not_authorized: '未授权', retrying: '补跑中', deactivated: '已禁用',
  queued: '排队中', error: '错误', unknown: '未知', alive: '存活', dead: '失效',
};

export function formatStatus(value) {
  const key = String(value || 'unknown').toLowerCase();
  return statusLabels[key] || value || '未知';
}

export function StatusPill({ value, children }) {
  const key = String(value || 'unknown').toLowerCase().replaceAll(' ', '_');
  return <span className={`status-pill status-${key}`}>{children || formatStatus(value)}</span>;
}

export function Button({ children, icon: Icon, variant = 'ghost', size = 'md', loading = false, ...props }) {
  return (
    <button className={`button button-${variant} button-${size}`} disabled={loading || props.disabled} {...props}>
      {loading ? <LoaderCircle className="spin" size={15} /> : Icon ? <Icon size={15} strokeWidth={2} /> : null}
      {children}
    </button>
  );
}

export function IconButton({ label, icon: Icon = MoreHorizontal, size = 16, ...props }) {
  return <button className="icon-button" aria-label={label} title={label} {...props}><Icon size={size} strokeWidth={2} /></button>;
}

export function Card({ children, className = '', ...props }) {
  return <section className={`surface ${className}`} {...props}>{children}</section>;
}

export function SectionHeader({ title, description, actions, eyebrow }) {
  return (
    <div className="section-header">
      <div>
        {eyebrow ? <div className="section-eyebrow">{eyebrow}</div> : null}
        <h2>{title}</h2>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="section-actions">{actions}</div> : null}
    </div>
  );
}

export function EmptyState({ title = '暂无数据', description = '调整筛选条件或先导入一些数据。', action }) {
  return <div className="empty-state"><CircleHelp size={22} /><strong>{title}</strong><span>{description}</span>{action}</div>;
}

export function ErrorState({ message, onRetry }) {
  return <div className="error-state"><AlertTriangle size={18} /><span>{message || '加载失败'}</span>{onRetry ? <Button icon={RefreshCw} onClick={onRetry}>重试</Button> : null}</div>;
}

export function LoadingRows({ columns = 5, rows = 4 }) {
  return <>{Array.from({ length: rows }, (_, row) => <tr key={row} className="loading-row">{Array.from({ length: columns }, (_, col) => <td key={col}><span /></td>)}</tr>)}</>;
}

export function Toolbar({ children, className = '' }) {
  return <div className={`toolbar ${className}`}>{children}</div>;
}

export function SearchField({ value, onChange, placeholder = '搜索…' }) {
  return <label className="search-field"><Search size={16} /><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} type="search" /></label>;
}

export function Select({ label, value, onChange, options, className = '' }) {
  return <label className={`select-field ${className}`}>{label ? <span>{label}</span> : null}<select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((option) => <option key={option.value ?? option} value={option.value ?? option}>{option.label ?? option}</option>)}</select></label>;
}

export function NumberField({ label, value, onChange, min = 1, max, step = 1 }) {
  return <label className="number-field">{label ? <span>{label}</span> : null}<input type="number" min={min} max={max} step={step} value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

export function Table({ children, className = '' }) {
  return <div className={`table-scroll ${className}`}><table>{children}</table></div>;
}

export function Checkbox({ checked, onChange, label, disabled = false }) {
  return <label className="checkbox"><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /><span>{label}</span></label>;
}

export function Pager({ page, pageSize, total, onPageChange, onPageSizeChange, sizes = [20, 50, 100] }) {
  const pages = Math.max(1, Math.ceil(Number(total || 0) / pageSize));
  return <div className="pager">
    <span>{total || 0} 条</span>
    <Select value={String(pageSize)} onChange={(value) => onPageSizeChange(Number(value))} options={sizes.map((size) => ({ value: String(size), label: `${size}/页` }))} />
    <IconButton label="上一页" icon={ChevronLeft} disabled={page <= 1} onClick={() => onPageChange(page - 1)} />
    <strong>{page} / {pages}</strong>
    <IconButton label="下一页" icon={ChevronRight} disabled={page >= pages} onClick={() => onPageChange(page + 1)} />
  </div>;
}

export function Modal({ open, title, onClose, children, footer, wide = false }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => event.key === 'Escape' && onClose?.();
    document.addEventListener('keydown', onKey);
    document.body.classList.add('modal-open');
    return () => { document.removeEventListener('keydown', onKey); document.body.classList.remove('modal-open'); };
  }, [open, onClose]);
  if (!open) return null;
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose?.(); }}>
    <div className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-label={title} ref={ref}>
      <div className="modal-header"><h2>{title}</h2><IconButton label="关闭" icon={X} onClick={onClose} /></div>
      <div className="modal-body">{children}</div>
      {footer ? <div className="modal-footer">{footer}</div> : null}
    </div>
  </div>;
}

export function CopyButton({ value, onCopy }) {
  return <IconButton label="复制" icon={Copy} onClick={() => onCopy?.(value)} />;
}

export function DownloadButton({ onClick, label = '下载' }) {
  return <Button icon={Download} size="sm" onClick={onClick}>{label}</Button>;
}

export function RefreshButton({ onClick, loading = false }) {
  return <IconButton label="刷新" icon={RefreshCw} className={loading ? 'is-loading' : ''} onClick={onClick} disabled={loading} />;
}

export function Toast({ toast, onClose }) {
  useEffect(() => {
    if (!toast) return undefined;
    const timer = window.setTimeout(onClose, 3600);
    return () => window.clearTimeout(timer);
  }, [toast, onClose]);
  if (!toast) return null;
  return <div className={`toast toast-${toast.type || 'info'}`} role="status"><span>{toast.message}</span><IconButton label="关闭提示" icon={X} onClick={onClose} /></div>;
}

export function Stat({ label, value, detail, tone = 'neutral', icon: Icon }) {
  return <div className={`stat stat-${tone}`}><div className="stat-label">{Icon ? <Icon size={15} /> : null}<span>{label}</span></div><strong>{value ?? '—'}</strong>{detail ? <small>{detail}</small> : null}</div>;
}

export function InlineNotice({ children, tone = 'info' }) {
  return <div className={`inline-notice notice-${tone}`}>{tone === 'warning' ? <AlertTriangle size={16} /> : <Check size={16} />}{children}</div>;
}
