import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArchiveX, CheckCircle2, Code2, Download, FileJson, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';
import { download, get, post } from '../api';
import { useDebounced, usePolling } from '../hooks';
import { Button, Card, Checkbox, EmptyState, ErrorState, Pager, RefreshButton, SearchField, SectionHeader, Stat, StatusPill, Table, Toolbar } from '../components/ui';

export default function CodexPage({ notify }) {
  const [rows, setRows] = useState([]); const [meta, setMeta] = useState({ total: 0, summary: {} }); const [query, setQuery] = useState(''); const [page, setPage] = useState(1); const [size, setSize] = useState(20); const [selected, setSelected] = useState(new Set()); const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const debouncedQuery = useDebounced(query);
  const load = useCallback(async () => { try { const response = await get('/api/codex', { paged: 1, page, page_size: size, q: debouncedQuery }); setRows(response.accounts || []); setMeta(response); setError(''); setSelected((current) => new Set([...current].filter((name) => (response.accounts || []).some((row) => (row.filename || row.name) === name)))); } catch (loadError) { setError(loadError.message); } finally { setLoading(false); } }, [page, size, debouncedQuery]);
  const { refresh, running } = usePolling(load, 5000, true);
  useEffect(() => setPage(1), [debouncedQuery, size]);
  const names = useMemo(() => [...selected], [selected]);
  const toggleAll = (checked) => setSelected(checked ? new Set(rows.map((row) => row.filename || row.name)) : new Set());
  const toggle = (name, checked) => setSelected((current) => { const next = new Set(current); checked ? next.add(name) : next.delete(name); return next; });
  async function bulk(path, body, message) { if (/\/delete(?:-|\/|$)/.test(path) && !window.confirm('确定删除选中的凭证吗？此操作不可恢复。')) return; try { await post(path, body); notify(message, 'success'); await load(); } catch (error) { notify(error.message, 'error'); } }
  async function resetSelected() {
    if (!names.length) return notify('请先选择凭证', 'warning');
    try { await Promise.all(names.map((filename) => post('/api/codex/reset-export', { filename }))); notify(`已重置 ${names.length} 个凭证`, 'success'); await load(); } catch (error) { notify(error.message, 'error'); }
  }
  async function downloadFiles(path) {
    if (!names.length) return notify('请先选择凭证', 'warning');
    try { await download(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filenames: names }) }); notify('凭证 ZIP 已下载', 'success'); } catch (error) { notify(error.message, 'error'); }
  }

  return <div className="page-stack"><div className="page-intro"><div><div className="eyebrow"><Code2 size={14} /> CREDENTIAL VAULT</div><h2>Codex 凭证</h2><p>查看本地授权文件的导出状态，按需下载或清理。</p></div><Button icon={RefreshCw} onClick={refresh} loading={running}>刷新凭证</Button></div>
    <div className="stats-grid compact-stats"><Stat label="凭证总数" value={meta.summary?.total ?? meta.total ?? 0} icon={FileJson} /><Stat label="已导出" value={meta.summary?.exported ?? 0} tone="green" icon={CheckCircle2} /><Stat label="待处理" value={meta.summary?.pending ?? 0} tone="amber" icon={RotateCcw} /></div>
    <Card className="table-card"><SectionHeader title="授权文件" description="批量下载会由后端打包，浏览器只接收最终文件。" actions={<div className="section-actions"><Button icon={Download} size="sm" disabled={!names.length} onClick={() => downloadFiles('/api/codex/download-bulk')}>下载本地</Button><Button icon={Download} size="sm" disabled={!names.length} onClick={() => downloadFiles('/api/codex/download-bulk-from-cpa')}>从 CPA 下载</Button><Button icon={RotateCcw} size="sm" disabled={!names.length} onClick={resetSelected}>重置状态</Button><Button icon={Trash2} size="sm" variant="danger" disabled={!names.length} onClick={() => bulk('/api/codex/delete-bulk', { filenames: names }, '已删除选中凭证')}>删除</Button></div>} />
      <Toolbar><SearchField value={query} onChange={setQuery} placeholder="搜索文件名或邮箱…" /><span className="selection-note">已选 {names.length}</span><RefreshButton onClick={refresh} loading={running} /></Toolbar>{error ? <ErrorState message={error} onRetry={load} /> : null}
      <Table><thead><tr><th className="check-col"><Checkbox checked={rows.length > 0 && rows.every((row) => selected.has(row.filename || row.name))} onChange={toggleAll} /></th><th>文件</th><th>账号</th><th>状态</th><th>更新时间</th><th className="actions-col">操作</th></tr></thead><tbody>{loading ? <tr><td colSpan="6"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : rows.length === 0 ? <tr><td colSpan="6"><EmptyState title="没有 Codex 凭证" description="完成一次授权后，凭证会出现在这里。" /></td></tr> : rows.map((row) => { const name = row.filename || row.name; return <tr key={name}><td><Checkbox checked={selected.has(name)} onChange={(checked) => toggle(name, checked)} /></td><td className="mono"><strong>{name}</strong></td><td>{row.email || row.account_email || '—'}</td><td><StatusPill value={(row.exported_count || 0) > 0 ? 'used' : 'available'}>{(row.exported_count || 0) > 0 ? `已导出 ${row.exported_count} 次` : '未导出'}</StatusPill></td><td>{row.updated_at || row.created_at || '—'}</td><td><div className="row-actions"><Button icon={Download} size="sm" onClick={() => download(`/api/codex/download/${encodeURIComponent(name)}`)}>下载</Button><Button icon={ArchiveX} size="sm" variant="danger" onClick={() => bulk('/api/codex/delete', { filename: name }, '凭证已删除')}>删除</Button></div></td></tr>; })}</tbody></Table><Pager page={page} pageSize={size} total={meta.total || 0} onPageChange={setPage} onPageSizeChange={(value) => { setSize(value); setPage(1); }} /></Card>
  </div>;
}
