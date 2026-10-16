import { Children, createContext, forwardRef, isValidElement, useCallback, useContext, useEffect, useId, useRef, useState } from 'react';
import { Button as ThemeButton, TextField, TextArea as ThemeTextArea } from '@radix-ui/themes';
import * as SelectPrimitive from '@radix-ui/react-select';
import * as Dialog from '@radix-ui/react-dialog';
import * as AlertDialog from '@radix-ui/react-alert-dialog';
import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import * as SwitchPrimitive from '@radix-ui/react-switch';
import * as ToastPrimitive from '@radix-ui/react-toast';
import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import * as Collapsible from '@radix-ui/react-collapsible';
import * as ScrollArea from '@radix-ui/react-scroll-area';
import * as ToggleGroup from '@radix-ui/react-toggle-group';
import {
  AlertTriangle, Check, ChevronDown, ChevronUp, ChevronLeft, ChevronRight, CircleHelp, Copy, Download,
  LoaderCircle, MoreHorizontal, RefreshCw, Search, X,
} from 'lucide-react';

export const statusLabels = {
  available: '可用', used: '已使用', failed: '失败', disabled: '已禁用',
  pending: '排队中', running: '运行中', stopping: '停止中', success: '成功',
  completed: '完成', cancelled: '已取消', stopped: '已停止', partial_success: '部分成功',
  authorized: '已授权', not_authorized: '未授权', retrying: '补跑中', deactivated: '已禁用',
  queued: '排队中', error: '错误', unknown: '未知', alive: '存活', dead: '失效',
  bound: '已绑定', reserved: '已预留', invalid: '已失效', expired: '已过期',
};

export function formatStatus(value) {
  const key = String(value || 'unknown').toLowerCase();
  return statusLabels[key] || value || '未知';
}

export function StatusPill({ value, children }) {
  const key = String(value || 'unknown').toLowerCase().replaceAll(' ', '_');
  return <span className={`status-pill status-${key}`}>{children || formatStatus(value)}</span>;
}

export const ActionButton = forwardRef(function ActionButton({ children, className = '', type = 'button', ...props }, ref) {
  return <ThemeButton ref={ref} type={type} className={`ui-action ${className}`} {...props}>{children}</ThemeButton>;
});

export function Hint({ content, children }) {
  if (!content) return children;
  return <TooltipPrimitive.Root><TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger><TooltipPrimitive.Portal><TooltipPrimitive.Content className="ui-tooltip" sideOffset={6}>{content}<TooltipPrimitive.Arrow className="ui-tooltip-arrow" /></TooltipPrimitive.Content></TooltipPrimitive.Portal></TooltipPrimitive.Root>;
}

export function Button({ children, icon: Icon, variant = 'ghost', size = 'md', loading = false, className = '', disabled = false, title, ...props }) {
  return <Hint content={title}><ActionButton {...props} className={`button button-${variant} button-${size}${className ? ` ${className}` : ''}`} disabled={loading || disabled} aria-busy={loading || undefined}>
    {loading ? <LoaderCircle className="spin" size={15} /> : Icon ? <Icon size={15} strokeWidth={2} /> : null}{children}
  </ActionButton></Hint>;
}

export function IconButton({ label, icon: Icon = MoreHorizontal, size = 16, className = '', ...props }) {
  return <Hint content={label}><ActionButton className={`icon-button ${className}`} aria-label={label} {...props}><Icon size={size} strokeWidth={2} /></ActionButton></Hint>;
}

export const Input = forwardRef(function Input({ className = '', ...props }, ref) {
  return <TextField.Root ref={ref} className={`ui-input ${className}`} {...props} />;
});

export const Textarea = forwardRef(function Textarea({ className = '', ...props }, ref) {
  return <ThemeTextArea ref={ref} className={`ui-textarea ${className}`} {...props} />;
});

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
  return <label className="search-field"><Search size={16} /><Input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} type="search" /></label>;
}

const EMPTY_OPTION = '__ui_empty_option__';
const encodeOption = (value) => String(value ?? '') === '' ? EMPTY_OPTION : String(value);

export function SelectControl({ value, onValueChange, options, disabled = false, className = '', id, 'aria-label': ariaLabel, ...props }) {
  const items = options.map((option) => ({ value: String(option.value ?? option), label: option.label ?? option, disabled: option.disabled }));
  return <SelectPrimitive.Root value={encodeOption(value)} onValueChange={(next) => onValueChange?.(next === EMPTY_OPTION ? '' : next)} disabled={disabled}>
    <SelectPrimitive.Trigger {...props} id={id} className={`ui-select-trigger ${className}`} aria-label={ariaLabel || '选择选项'}><SelectPrimitive.Value /><SelectPrimitive.Icon className="ui-select-icon"><ChevronDown size={14} /></SelectPrimitive.Icon></SelectPrimitive.Trigger>
    <SelectPrimitive.Portal><SelectPrimitive.Content className="ui-select-content" position="popper" sideOffset={5} collisionPadding={10}>
      <SelectPrimitive.ScrollUpButton className="ui-select-scroll"><ChevronUp size={14} /></SelectPrimitive.ScrollUpButton>
      <SelectPrimitive.Viewport className="ui-select-viewport">{items.map((option) => <SelectPrimitive.Item className="ui-select-item" key={option.value} value={encodeOption(option.value)} disabled={option.disabled}><SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText><SelectPrimitive.ItemIndicator><Check size={14} /></SelectPrimitive.ItemIndicator></SelectPrimitive.Item>)}</SelectPrimitive.Viewport>
      <SelectPrimitive.ScrollDownButton className="ui-select-scroll"><ChevronDown size={14} /></SelectPrimitive.ScrollDownButton>
    </SelectPrimitive.Content></SelectPrimitive.Portal>
  </SelectPrimitive.Root>;
}

export function Select({ label, value, onChange, options, className = '', ...props }) {
  const id = useId();
  return <div className={`select-field ${className}`}>{label ? <label htmlFor={id}>{label}</label> : null}<SelectControl {...props} id={id} value={value} onValueChange={onChange} options={options} aria-label={props['aria-label'] || label || String(options[0]?.label || '选择选项')} /></div>;
}

// Keeps event-style call sites while rendering a Radix Select instead of a DOM select.
export function SelectInput({ children, value, onChange, ...props }) {
  const options = [];
  function collect(nodes) {
    Children.forEach(nodes, (child) => {
      if (!isValidElement(child)) return;
      if (child.type === 'option') options.push({ value: child.props.value, label: child.props.children, disabled: child.props.disabled });
      else if (child.props.children) collect(child.props.children);
    });
  }
  collect(children);
  return <SelectControl {...props} value={value} options={options} onValueChange={(next) => onChange?.({ target: { value: next }, currentTarget: { value: next } })} />;
}

export function NumberField({ label, value, onChange, min = 1, max, step = 1 }) {
  return <label className="number-field">{label ? <span>{label}</span> : null}<Input type="number" min={min} max={max} step={step} value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

export function Table({ children, className = '' }) {
  return <ScrollArea.Root className={`table-scroll ${className}`} type="auto"><ScrollArea.Viewport className="ui-table-viewport"><table>{children}</table></ScrollArea.Viewport><ScrollArea.Scrollbar className="ui-scrollbar" orientation="horizontal"><ScrollArea.Thumb className="ui-scrollbar-thumb" /></ScrollArea.Scrollbar></ScrollArea.Root>;
}

export function CheckboxControl({ checked, onChange, disabled = false, className = '', 'aria-label': ariaLabel, ...props }) {
  return <CheckboxPrimitive.Root {...props} className={`ui-checkbox ${className}`} checked={checked} disabled={disabled} onCheckedChange={(next) => onChange?.({ target: { checked: next === true } })} aria-label={ariaLabel || '选择此项'}><CheckboxPrimitive.Indicator><Check size={12} /></CheckboxPrimitive.Indicator></CheckboxPrimitive.Root>;
}

export function Checkbox({ checked, onChange, label, ariaLabel, disabled = false }) {
  return <label className="checkbox"><CheckboxControl checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} aria-label={ariaLabel || label || '选择此项'} />{label ? <span>{label}</span> : null}</label>;
}

export function Switch({ checked, onCheckedChange, label, disabled }) {
  return <SwitchPrimitive.Root className="ui-switch" checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} aria-label={label}><SwitchPrimitive.Thumb className="ui-switch-thumb" /></SwitchPrimitive.Root>;
}

export function AdvancedFields({ children }) {
  return <Collapsible.Root className="config-advanced-fields"><Collapsible.Trigger className="ui-advanced-trigger"><span>高级设置</span><small>一般不需要修改</small><ChevronDown size={14} /></Collapsible.Trigger><Collapsible.Content className="ui-advanced-content"><div className="config-advanced-grid">{children}</div></Collapsible.Content></Collapsible.Root>;
}

export function ChoiceTabs({ value, onValueChange, label, children, className = '' }) {
  return <ToggleGroup.Root type="single" value={value} onValueChange={(next) => next && onValueChange(next)} className={`config-browser-tabs ${className}`} aria-label={label}>{children}</ToggleGroup.Root>;
}
export const ChoiceTab = ToggleGroup.Item;

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
  const origin = useRef(null);
  return <Dialog.Root open={open} onOpenChange={(next) => !next && onClose?.()}><Dialog.Portal>
    <Dialog.Overlay className="modal-backdrop ui-dialog-overlay" />
    <Dialog.Content className={`modal ui-dialog-content ${wide ? 'modal-wide' : ''}`} aria-describedby={undefined} onOpenAutoFocus={() => { origin.current = document.activeElement; }} onCloseAutoFocus={(event) => { event.preventDefault(); if (origin.current?.isConnected) origin.current.focus(); }}>
      <div className="modal-header"><Dialog.Title asChild><h2>{title}</h2></Dialog.Title><Dialog.Close asChild><ActionButton className="icon-button" aria-label="关闭"><X size={16} /></ActionButton></Dialog.Close></div>
      <div className="modal-body">{children}</div>
      {footer ? <div className="modal-footer">{footer}</div> : null}
    </Dialog.Content>
  </Dialog.Portal></Dialog.Root>;
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
  return <><ToastPrimitive.Root key={toast?.id || 'empty'} open={Boolean(toast)} onOpenChange={(next) => !next && onClose?.()} duration={3600} className={`toast ui-toast toast-${toast?.type || 'info'}`}><ToastPrimitive.Description asChild><span>{toast?.message}</span></ToastPrimitive.Description><ToastPrimitive.Close asChild><ActionButton className="icon-button" aria-label="关闭提示"><X size={16} /></ActionButton></ToastPrimitive.Close></ToastPrimitive.Root><ToastPrimitive.Viewport className="ui-toast-viewport" /></>;
}

export function Stat({ label, value, detail, tone = 'neutral', icon: Icon }) {
  return <div className={`stat stat-${tone}`}><div className="stat-label">{Icon ? <Icon size={15} /> : null}<span>{label}</span></div><strong>{value ?? '—'}</strong>{detail ? <small>{detail}</small> : null}</div>;
}

export function InlineNotice({ children, tone = 'info' }) {
  return <div className={`inline-notice notice-${tone}`}>{tone === 'warning' ? <AlertTriangle size={16} /> : <Check size={16} />}{children}</div>;
}


const InteractionContext = createContext(null);

export function useConfirm() { return useContext(InteractionContext).confirm; }
export function usePrompt() { return useContext(InteractionContext).prompt; }

export function UIProvider({ children }) {
  const [request, setRequest] = useState(null);
  const [text, setText] = useState('');
  const queue = useRef([]);
  const pending = useRef(null);
  const focusOrigin = useRef(null);
  const showNext = useCallback(() => {
    const next = queue.current.shift();
    if (!next) return;
    pending.current = next;
    focusOrigin.current = document.activeElement;
    setText('');
    setRequest(next);
  }, []);
  const ask = useCallback((kind, message) => new Promise((resolve) => {
    queue.current.push({ kind, message, resolve });
    if (!pending.current) showNext();
  }), [showNext]);
  const finish = useCallback((result) => {
    const previous = pending.current;
    if (!previous) return;
    pending.current = null;
    setRequest(null);
    previous.resolve(result);
    if (focusOrigin.current?.isConnected) focusOrigin.current.focus();
    queueMicrotask(showNext);
  }, [showNext]);
  useEffect(() => () => {
    pending.current?.resolve(false);
    queue.current.splice(0).forEach((item) => item.resolve(false));
  }, []);
  const confirm = useCallback((message) => ask('confirm', message), [ask]);
  const prompt = useCallback((message) => ask('prompt', message), [ask]);
  return <TooltipPrimitive.Provider delayDuration={450}><ToastPrimitive.Provider swipeDirection="right"><InteractionContext.Provider value={{ confirm, prompt }}>
    {children}
    <AlertDialog.Root open={request?.kind === 'confirm'} onOpenChange={(next) => !next && finish(false)}><AlertDialog.Portal><AlertDialog.Overlay className="modal-backdrop ui-dialog-overlay" /><AlertDialog.Content className="modal ui-dialog-content ui-confirm">
      <div className="modal-header"><AlertDialog.Title asChild><h2>确认操作</h2></AlertDialog.Title></div>
      <AlertDialog.Description className="ui-confirm-description">{request?.message}</AlertDialog.Description>
      <div className="modal-footer"><AlertDialog.Cancel asChild><Button onClick={() => finish(false)}>取消</Button></AlertDialog.Cancel><AlertDialog.Action asChild><Button variant="primary" onClick={() => finish(true)}>确定</Button></AlertDialog.Action></div>
    </AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>
    <Modal open={request?.kind === 'prompt'} title="输入验证码" onClose={() => finish(null)} footer={<><Button onClick={() => finish(null)}>取消</Button><Button variant="primary" disabled={!text.trim()} onClick={() => finish(text)}>提交</Button></>}>
      <form onSubmit={(event) => { event.preventDefault(); if (text.trim()) finish(text); }}><label className="field-label">{request?.message}<Input value={text} onChange={(event) => setText(event.target.value)} autoComplete="one-time-code" inputMode="numeric" /></label></form>
    </Modal>
  </InteractionContext.Provider></ToastPrimitive.Provider></TooltipPrimitive.Provider>;
}
