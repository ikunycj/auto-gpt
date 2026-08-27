import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Archive, Ban, Check, Database, Inbox, Link2, MailPlus, Plus, Trash2,
  Upload, XCircle,
} from 'lucide-react';
import { copyText, get, post } from '../api';
import { useDebounced, usePolling } from '../hooks';
import {
  Button, Card, Checkbox, EmptyState, ErrorState, Modal, Pager,
  RefreshButton, SearchField, SectionHeader, Select, StatusPill, Table,
  Toolbar,
} from '../components/ui';

const SOURCES = [
  { value: 'all', label: '全部来源' },
  { value: 'outlook', label: 'Outlook' },
  { value: 'generic_api', label: '接码 API' },
  { value: 'cloudflare_domain', label: '域名邮箱' },
  { value: 'cloudflare', label: 'Cloudflare Worker' },
  { value: 'gptmail', label: 'GPTMail' },
  { value: 'mailnest', label: 'MailNest' },
  { value: 'cloudmail', label: 'CloudMail' },
];

const FIXED_PROVIDER_SOURCES = ['cloudflare_domain', 'cloudflare', 'gptmail', 'mailnest', 'cloudmail'];

const STATUSES = [
  { value: '', label: '全部状态' },
  { value: 'available', label: '可用' },
  { value: 'used', label: '已使用' },
  { value: 'failed', label: '失败' },
  { value: 'disabled', label: '已停用' },
];

const SOURCE_LABELS = {
  outlook: 'Outlook',
  generic_api: '接码 API',
  cloudflare_domain: '域名邮箱',
  cloudflare: 'Cloudflare Worker',
  gptmail: 'GPTMail',
  mailnest: 'MailNest',
  cloudmail: 'CloudMail',
};

const PROVIDER_SETTINGS_GROUP = {
  cloudflare_domain: 'email:domain',
  cloudflare: 'email:temporary',
  gptmail: 'email:temporary',
  mailnest: 'email:temporary',
  cloudmail: 'email:temporary',
};

function sourceOf(row, fallback = 'outlook') {
  return String(row?.source || fallback || 'outlook');
}

function keyFor(row, fallback) {
  return `${sourceOf(row, fallback)}|${String(row?.email || '').toLowerCase()}`;
}

function formatDate(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

function urlHost(value) {
  try {
    return new URL(String(value || '')).hostname || '接码地址';
  } catch (_) {
    return '接码地址';
  }
}

function materialFor(row) {
  if (row.fixed) return { value: '', preview: row.ready ? '自动领取邮箱' : '待完成配置' };
  const source = sourceOf(row);
  if (source === 'generic_api') {
    return { value: row.code_url || row.copy_line || '', preview: urlHost(row.code_url) };
  }
  if (source === 'outlook') {
    return { value: row.copy_line || '', preview: 'OAuth 凭证' };
  }
  return { value: '', preview: '域名邮箱' };
}

function CopyValue({ value, preview, label, notify }) {
  const text = String(value || '');
  if (!text) return <span className="copy-cell-empty">{preview || '—'}</span>;
  const copy = async () => {
    try {
      await copyText(text);
      notify(`${label}已复制`, 'success');
    } catch (error) {
      notify(error.message || `${label}复制失败`, 'error');
    }
  };
  return <button type="button" className="copy-cell-button" title={`点击复制${label}`} onClick={copy}>{preview || text}</button>;
}

function fixedProviderRows(summary, source, status, query) {
  const channels = Array.isArray(summary?.registration_email?.all_channels)
    ? summary.registration_email.all_channels
    : [];
  const automatic = Boolean(summary?.registration_email?.automatic);
  const normalizedQuery = String(query || '').trim().toLowerCase();
  return channels
    .filter((channel) => FIXED_PROVIDER_SOURCES.includes(channel.id))
    .map((channel) => {
      const enabled = automatic && Boolean(channel.enabled);
      const ready = Boolean(channel.ready);
      return {
        fixed: true,
        provider: true,
        source: channel.id,
        email: channel.label || SOURCE_LABELS[channel.id] || channel.id,
        providerLabel: SOURCE_LABELS[channel.id] || channel.label || channel.id,
        ready,
        configured: Boolean(channel.configured),
        enabled,
        status: enabled && ready ? 'available' : 'disabled',
        message: channel.message || (ready ? '配置完整，注册时自动领取邮箱' : '尚未完成配置'),
      };
    })
    .filter((row) => source === 'all' || row.source === source)
    .filter((row) => !status || row.status === status)
    .filter((row) => {
      if (!normalizedQuery) return true;
      return [row.email, row.providerLabel, row.source, row.message]
        .some((value) => String(value || '').toLowerCase().includes(normalizedQuery));
    });
}

export default function EmailPage({ notify, summary, onOpenSettings, onSummaryRefresh }) {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [source, setSource] = useState('all');
  const [status, setStatus] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [importOpen, setImportOpen] = useState(false);
  const [importSource, setImportSource] = useState('outlook');
  const [importText, setImportText] = useState('');
  const [importing, setImporting] = useState(false);
  const debouncedQuery = useDebounced(query);

  const load = useCallback(async () => {
    try {
      // Generated providers do not have rows in the local SQLite pools. Their
      // fixed status rows are derived from the summary below.
      if (['cloudflare', 'gptmail', 'mailnest', 'cloudmail'].includes(source)) {
        setRows([]);
        setTotal(0);
        setSelected(new Set());
        setError('');
        return;
      }
      const response = await get('/api/outlook', {
        source,
        status,
        q: debouncedQuery,
        paged: 1,
        page,
        page_size: pageSize,
      });
      const items = response.items || [];
      setRows(items);
      setTotal(Number(response.total || 0));
      setSelected((current) => new Set([...current].filter((key) => items.some((row) => keyFor(row, source) === key))));
      setError('');
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }, [debouncedQuery, page, pageSize, source, status]);

  const { refresh, running } = usePolling(load, 5000, true);
  useEffect(() => { setPage(1); }, [debouncedQuery, pageSize, source, status]);

  const fixedRows = useMemo(
    () => fixedProviderRows(summary, source, status, debouncedQuery),
    [debouncedQuery, source, status, summary],
  );
  const displayRows = useMemo(
    () => (page === 1 ? [...fixedRows, ...rows] : rows),
    [fixedRows, page, rows],
  );
  const displayTotal = total + fixedRows.length;

  const selectedKeys = useMemo(() => [...selected], [selected]);
  const selectedItems = useMemo(() => rows
    .filter((row) => selected.has(keyFor(row, source)))
    .map((row) => ({ email: row.email, source: sourceOf(row, source) })), [rows, selected, source]);

  function toggle(key, checked) {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  async function updateStatus(items, nextStatus) {
    if (!items.length) return notify('请先选择邮箱', 'warning');
    try {
      const response = await post('/api/outlook/status-bulk', { items, status: nextStatus, source });
      const updatedCount = Number(response.updated_count ?? response.updated?.length ?? 0);
      notify(updatedCount ? `已更新 ${updatedCount} 个邮箱` : '没有邮箱被更新', updatedCount ? 'success' : 'warning');
      await load();
      await onSummaryRefresh?.();
    } catch (actionError) {
      notify(actionError.message, 'error');
    }
  }

  async function deleteItems(items) {
    if (!items.length) return notify('请先选择邮箱', 'warning');
    if (!window.confirm(`确定彻底删除选中的 ${items.length} 个邮箱素材吗？此操作不可恢复。`)) return;
    try {
      const response = await post('/api/outlook/delete-bulk', { items, source });
      const deletedCount = Number(response.deleted_count ?? response.deleted?.length ?? 0);
      notify(deletedCount ? `已删除 ${deletedCount} 个邮箱` : '没有邮箱被删除', deletedCount ? 'success' : 'warning');
      setSelected(new Set());
      await load();
      await onSummaryRefresh?.();
    } catch (actionError) {
      notify(actionError.message, 'error');
    }
  }

  async function toggleProvider(row) {
    const runtime = summary?.registration_email;
    const current = Array.isArray(runtime?.sources) ? runtime.sources : [];
    if (!row.enabled && !row.ready) {
      return notify(`${row.providerLabel}尚未完成配置，请先去配置`, 'warning');
    }
    const next = row.enabled
      ? current.filter((item) => item !== row.source)
      : [...current.filter((item) => item !== row.source), row.source];
    try {
      const response = await post('/api/config', {
        updates: {
          USE_EMAIL_SERVICE: next.length > 0,
          EMAIL_SOURCE: next.join(','),
        },
      });
      notify(response.note || (row.enabled ? `${row.providerLabel}已停用` : `${row.providerLabel}已启用`), 'success');
      await onSummaryRefresh?.();
    } catch (actionError) {
      notify(actionError.message, 'error');
    }
  }

  async function importEmails() {
    if (!importText.trim()) return notify('请粘贴邮箱素材', 'warning');
    setImporting(true);
    try {
      const response = await post('/api/outlook/import', { text: importText, source: importSource });
      const inserted = Number(response.inserted || 0);
      const skipped = Number(response.skipped || 0);
      notify(`已导入 ${inserted} 个邮箱${skipped ? `，跳过 ${skipped} 条` : ''}`, inserted ? 'success' : 'warning');
      setImportText('');
      setImportOpen(false);
      await load();
      await onSummaryRefresh?.();
    } catch (actionError) {
      notify(actionError.message, 'error');
    } finally {
      setImporting(false);
    }
  }

  const allSelected = rows.length > 0 && rows.every((row) => selected.has(keyFor(row, source)));
  const importPlaceholder = importSource === 'outlook'
    ? '邮箱----密码----clientId----refreshToken'
    : '邮箱----HTTP 接码地址';

  return <div className="page-stack">
    <div className="page-intro">
      <div><div className="eyebrow"><Inbox size={14} /> EMAIL WORKSPACE</div><h2>邮箱</h2></div>
      <div className="intro-actions">
        <Button icon={MailPlus} disabled title="邮箱注册暂未开放">注册邮箱</Button>
        <Button icon={Upload} variant="primary" onClick={() => setImportOpen(true)}>导入邮箱</Button>
      </div>
    </div>

    <Card className="table-card email-management-panel">
      <SectionHeader title="邮箱管理" actions={<div className="section-actions">
        <Button icon={Check} size="sm" disabled={!selectedKeys.length} onClick={() => updateStatus(selectedItems, 'available')}>标记可用</Button>
        <Button icon={Archive} size="sm" disabled={!selectedKeys.length} onClick={() => updateStatus(selectedItems, 'used')}>标记已用</Button>
        <Button icon={XCircle} size="sm" disabled={!selectedKeys.length} onClick={() => updateStatus(selectedItems, 'failed')}>标记失败</Button>
        <Button icon={Ban} size="sm" disabled={!selectedKeys.length} onClick={() => updateStatus(selectedItems, 'disabled')}>停用</Button>
        <Button icon={Trash2} variant="danger" size="sm" disabled={!selectedKeys.length} onClick={() => deleteItems(selectedItems)}>删除</Button>
      </div>} />
      <Toolbar className="filter-toolbar email-management-toolbar">
        <SearchField value={query} onChange={setQuery} placeholder="搜索邮箱、备注或来源…" />
        <Select value={source} onChange={setSource} options={SOURCES} />
        <Select value={status} onChange={setStatus} options={STATUSES} />
        <span className="selection-note">已选 {selectedKeys.length}</span>
        <RefreshButton onClick={refresh} loading={running} />
      </Toolbar>
      {error ? <ErrorState message={error} onRetry={load} /> : null}
      <Table className="email-management-table"><colgroup>
        <col className="email-col-check" /><col className="email-col-address" /><col className="email-col-source" />
        <col className="email-col-material" /><col className="email-col-status" /><col className="email-col-link" />
        <col className="email-col-note" /><col className="email-col-time" /><col className="email-col-actions" />
      </colgroup><thead><tr>
        <th className="check-col"><Checkbox checked={allSelected} onChange={(checked) => setSelected(checked ? new Set(rows.map((row) => keyFor(row, source))) : new Set())} /></th>
        <th>邮箱</th><th>来源</th><th>接码凭证</th><th>状态</th><th>GPT账号</th><th>备注</th><th>时间</th><th>操作</th>
      </tr></thead><tbody>
        {loading ? <tr><td colSpan="9"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : displayRows.length === 0 ? <tr><td colSpan="9"><EmptyState title="暂无邮箱" description="导入 Outlook 或接码 API 邮箱后，可在这里统一维护。" action={<Button icon={Plus} size="sm" onClick={() => setImportOpen(true)}>导入邮箱</Button>} /></td></tr> : displayRows.map((row) => {
          const rowSource = sourceOf(row, source);
          const rowKey = keyFor(row, source);
          const material = materialFor(row);
          const rowStatus = row.status || 'available';
          if (row.fixed) {
            return <tr key={rowKey} className="email-provider-row">
              <td><span className="fixed-row-lock" title="固定渠道，不可删除">固定</span></td>
              <td><strong>{row.providerLabel}</strong><small className="fixed-row-subtitle">固定邮箱来源</small></td>
              <td><span className="source-tag source-tag-provider"><Database size={13} />{row.providerLabel}</span></td>
              <td><span className="copy-cell-empty">{material.preview}</span></td>
              <td><StatusPill value={rowStatus}>{row.enabled ? '已启用' : row.ready ? '未启用' : '待配置'}</StatusPill></td>
              <td><span className="copy-cell-empty">注册时自动领取</span></td>
              <td className="truncate" title={row.message || ''}>{row.message || '—'}</td>
              <td className="cell-nowrap">—</td>
              <td><div className="row-actions">
                <Button size="sm" disabled={!row.enabled && !row.ready} onClick={() => toggleProvider(row)}>{row.enabled ? '停用' : '启用'}</Button>
                <Button size="sm" onClick={() => onOpenSettings?.(PROVIDER_SETTINGS_GROUP[row.source] || 'email:temporary')}>配置</Button>
              </div></td>
            </tr>;
          }
          return <tr key={rowKey}>
            <td><Checkbox checked={selected.has(rowKey)} onChange={(checked) => toggle(rowKey, checked)} /></td>
            <td><strong><CopyValue value={row.email} label="邮箱" notify={notify} /></strong></td>
            <td><span className="source-tag">{rowSource === 'generic_api' ? <Link2 size={13} /> : <Database size={13} />}{SOURCE_LABELS[rowSource] || rowSource}</span></td>
            <td><CopyValue value={material.value} preview={material.preview} label="邮箱素材" notify={notify} /></td>
            <td><StatusPill value={rowStatus} /></td>
            <td>{row.registered_account_id ? <StatusPill value="registered">已关联</StatusPill> : <span className="copy-cell-empty">未关联</span>}</td>
            <td className="truncate" title={row.note || ''}>{row.note || '—'}</td>
            <td className="cell-nowrap">{formatDate(row.imported_at || row.created_at || row.used_at)}</td>
            <td><div className="row-actions">
              {rowStatus !== 'available' ? <Button size="sm" onClick={() => updateStatus([{ email: row.email, source: rowSource }], 'available')}>可用</Button> : null}
              {rowStatus !== 'disabled' ? <Button size="sm" onClick={() => updateStatus([{ email: row.email, source: rowSource }], 'disabled')}>停用</Button> : null}
              <Button variant="danger" size="sm" onClick={() => deleteItems([{ email: row.email, source: rowSource }])}>删除</Button>
            </div></td>
          </tr>;
        })}
      </tbody></Table>
      <Pager page={page} pageSize={pageSize} total={displayTotal} onPageChange={setPage} onPageSizeChange={(value) => { setPageSize(value); setPage(1); }} />
    </Card>

    <Modal open={importOpen} title="导入邮箱" onClose={() => setImportOpen(false)} wide footer={<><Button onClick={() => setImportOpen(false)}>取消</Button><Button icon={Upload} variant="primary" loading={importing} onClick={importEmails}>开始导入</Button></>}>
      <div className="form-grid">
        <Select label="邮箱类型" value={importSource} onChange={setImportSource} options={[{ value: 'outlook', label: 'Outlook' }, { value: 'generic_api', label: '接码 API' }]} />
      </div>
      <label className="field-label">粘贴邮箱素材<textarea rows="12" value={importText} onChange={(event) => setImportText(event.target.value)} placeholder={importPlaceholder} spellCheck="false" /></label>
      <p className="field-help">{importSource === 'outlook' ? '每行一个邮箱，按邮箱、密码、clientId、refreshToken 的顺序填写；' : '每行一个邮箱，自动识别邮箱和 HTTP 接码地址；'}<button type="button" className="field-help-link" onClick={() => { setImportOpen(false); onOpenSettings?.('email:general'); }}>点击设置分隔符</button></p>
    </Modal>
  </div>;
}
