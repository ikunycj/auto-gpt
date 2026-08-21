import { useCallback, useEffect, useMemo, useState } from 'react';
import { Archive, CheckCircle2, Clipboard, Download, FileKey2, HeartPulse, Link2, MoreHorizontal, NotebookPen, RefreshCw, RotateCcw, ShieldAlert, Square, Trash2, Upload, UserRound, X } from 'lucide-react';
import { copyText, download, get, post } from '../api';
import { useDebounced, usePolling } from '../hooks';
import { Button, Card, Checkbox, EmptyState, ErrorState, IconButton, Modal, Pager, RefreshButton, SearchField, SectionHeader, Select, StatusPill, Table, Toolbar } from '../components/ui';

function formatDate(value) { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false }); }

export default function AccountsPage({ notify }) {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [query, setQuery] = useState('');
  const [archived, setArchived] = useState('0');
  const [plan, setPlan] = useState('');
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [noteTarget, setNoteTarget] = useState(null);
  const [note, setNote] = useState('');
  const [log, setLog] = useState(null);
  const debouncedQuery = useDebounced(query);

  const loadAccounts = useCallback(async () => {
    try {
      const [response, statusSnapshot] = await Promise.all([
        get('/api/accounts', { paged: 1, page, page_size: size, q: debouncedQuery, archived, plan }),
        get('/api/accounts/plan-check-status', { paged: 1, page, page_size: size, q: debouncedQuery, archived, plan }),
      ]);
      const statusById = new Map((statusSnapshot.items || []).map((item) => [String(item.id), item]));
      const merged = (response.items || []).map((item) => ({ ...item, ...(statusById.get(String(item.id)) || {}) }));
      setRows(merged); setTotal(Number(response.total || 0)); setError('');
      setSelected((current) => new Set([...current].filter((id) => (response.items || []).some((row) => Number(row.id) === id))));
    } catch (loadError) { setError(loadError.message); }
    finally { setLoading(false); }
  }, [page, size, debouncedQuery, archived, plan]);
  const { refresh: refreshAccounts, running } = usePolling(loadAccounts, 2000, true);
  useEffect(() => { setPage(1); }, [debouncedQuery, archived, plan, size]);
  useEffect(() => {
    if (!log?.email || !log.kind) return undefined;
    let disposed = false;
    const pollLog = async () => {
      try { const response = await get(log.kind === 'live' ? '/api/accounts/live-check-log' : '/api/codex/retry-log', { email: log.email }); if (!disposed) setLog((current) => current ? { ...current, text: response.log || '暂无日志' } : current); } catch (_) { /* log may be rotated while polling */ }
    };
    const timer = window.setInterval(pollLog, 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [log?.email, log?.kind]);
  const ids = useMemo(() => [...selected], [selected]);

  async function action(path, body, message, confirmMessage = '') { const confirmation = confirmMessage || (/\/delete(?:-|\/|$)/.test(path) ? '确定删除选中的数据吗？此操作不可恢复。' : ''); if (confirmation && !window.confirm(confirmation)) return; try { await post(path, body); notify(message, 'success'); await loadAccounts(); } catch (error) { notify(error.message, 'error'); } }
  const toggleAll = (checked) => setSelected(checked ? new Set(rows.map((row) => Number(row.id))) : new Set());
  const toggle = (id, checked) => setSelected((current) => { const next = new Set(current); checked ? next.add(Number(id)) : next.delete(Number(id)); return next; });

  async function secrets(field) {
    if (!ids.length) return notify('请先选择账号', 'warning');
    try {
      const response = await post('/api/accounts/secret-bulk', { account_ids: ids, field });
      const text = (response.values || []).map((item) => item.value).filter(Boolean).join('\n');
      if (!text) return notify('选中账号没有可用内容', 'warning');
      await copyText(text); notify(`已复制 ${response.values.length} 项`, 'success');
    } catch (error) { notify(error.message, 'error'); }
  }

  async function openLog(row, kind) {
    try { const response = await get(kind === 'live' ? '/api/accounts/live-check-log' : '/api/codex/retry-log', { email: row.email }); setLog({ email: row.email, kind, title: `${kind === 'live' ? '查活' : 'Codex 补跑'} · ${row.email}`, text: response.log || '暂无日志' }); } catch (error) { notify(error.message, 'error'); }
  }

  async function saveNote() {
    if (!noteTarget) return;
    try { await post(noteTarget.bulk ? '/api/accounts/note-bulk' : `/api/accounts/${noteTarget.id}/note`, noteTarget.bulk ? { account_ids: ids, note } : { note }); notify('备注已保存', 'success'); setNoteTarget(null); await loadAccounts(); } catch (error) { notify(error.message, 'error'); }
  }

  async function checkPlan(id) { await action('/api/accounts/check-plan', { account_id: id }, '套餐查询已入队'); }
  async function extractLink(id) { await action('/api/accounts/extract-link', { account_id: id }, '提链任务已入队'); }
  async function generateAgent(id) { await action('/api/accounts/codex-agent', { account_id: id, verify_task: true }, 'Agent Token 生成已入队'); }
  async function uploadAgent(id) { await action(`/api/accounts/${encodeURIComponent(id)}/codex-agent/upload-sub2`, {}, 'Agent Token 已提交上传'); }
  async function downloadAgent(id) { try { await download(`/api/accounts/${encodeURIComponent(id)}/codex-agent/download`); notify('Agent Token 已下载', 'success'); } catch (error) { notify(error.message, 'error'); } }

  async function generateAgents() { if (!ids.length) return notify('请先选择账号', 'warning'); await action('/api/accounts/codex-agent-bulk', { account_ids: ids, verify_task: true }, 'Codex Agent Token 生成任务已入队'); }
  async function uploadAgents() { if (!ids.length) return notify('请先选择账号', 'warning'); await action('/api/accounts/codex-agent/upload-sub2-bulk', { account_ids: ids }, 'Agent Token 上传任务已提交'); }
  async function downloadAgents() {
    if (!ids.length) return notify('请先选择账号', 'warning');
    try { await download('/api/accounts/codex-agent/download-bulk', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ account_ids: ids }) }); notify('Agent Token ZIP 已下载', 'success'); } catch (error) { notify(error.message, 'error'); }
  }
  async function downloadCpa() {
    if (!ids.length) return notify('请先选择账号', 'warning');
    try { const response = await post('/api/accounts/download-cpa-bulk', { account_ids: ids, prepare: true }); if (response.download_url) await download(response.download_url); notify(`已准备 ${response.added_count || 0} 个 CPA 凭证`, 'success'); } catch (error) { notify(error.message, 'error'); }
  }
  async function stopCodex() { if (!ids.length) return notify('请先选择账号', 'warning'); await action('/api/codex/stop-bulk', { account_ids: ids }, '已发送 Codex 停止请求'); }

  return <div className="page-stack">
    <div className="page-intro"><div><div className="eyebrow"><UserRound size={14} /> ACCOUNT LEDGER</div><h2>已注册账号</h2><p>用状态、套餐和最近活动快速筛出需要维护的账号。</p></div><div className="intro-actions"><Button icon={RefreshCw} onClick={refreshAccounts} loading={running}>刷新列表</Button></div></div>
    <div className="stats-grid compact-stats"><div className="stat stat-neutral"><div className="stat-label"><UserRound size={15} /><span>当前结果</span></div><strong>{total}</strong><small>符合筛选条件</small></div><div className="stat stat-green"><div className="stat-label"><ShieldAlert size={15} /><span>有 access token</span></div><strong>{rows.filter((row) => row.has_access_token).length}</strong><small>当前页</small></div><div className="stat stat-blue"><div className="stat-label"><CheckCircle2 size={15} /><span>有 2FA</span></div><strong>{rows.filter((row) => row.totp_enabled).length}</strong><small>当前页</small></div><div className="stat stat-amber"><div className="stat-label"><Archive size={15} /><span>已归档</span></div><strong>{archived === 'only' ? total : '—'}</strong><small>筛选视图</small></div></div>
    <Card className="table-card"><SectionHeader title="账号列表" description="敏感 token 不随列表返回，复制时会单独请求并在浏览器内处理。" actions={<div className="section-actions"><Button icon={HeartPulse} size="sm" disabled={!ids.length} onClick={() => action('/api/accounts/check-live-bulk', { account_ids: ids }, '查活任务已入队')}>查活</Button><Button icon={RefreshCw} size="sm" disabled={!ids.length} onClick={() => action('/api/accounts/check-plan-bulk', { account_ids: ids }, '套餐查询已入队')}>查套餐</Button><Button icon={Link2} size="sm" disabled={!ids.length} onClick={() => action('/api/accounts/extract-link-bulk', { account_ids: ids }, '提链任务已入队')}>提链</Button><Button icon={RotateCcw} size="sm" disabled={!ids.length} onClick={() => action('/api/codex/retry-bulk', { account_ids: ids, workers: 1 }, 'Codex 补跑已入队')}>补跑 Codex</Button><Button icon={SquareIcon} size="sm" disabled={!ids.length} onClick={stopCodex}>停止补跑</Button></div>} />
      <Toolbar className="filter-toolbar"><SearchField value={query} onChange={setQuery} placeholder="搜索邮箱、来源、备注…" /><Select value={archived} onChange={setArchived} options={[{ value: '0', label: '未归档' }, { value: 'only', label: '仅归档' }, { value: 'all', label: '全部' }]} /><Select value={plan} onChange={setPlan} options={[{ value: '', label: '全部套餐' }, { value: 'plus', label: 'Plus' }, { value: 'free', label: 'Free' }]} /><span className="selection-note">已选 {ids.length}</span><RefreshButton onClick={refreshAccounts} loading={running} /></Toolbar>
      <Toolbar className="batch-toolbar"><Button icon={Clipboard} size="sm" disabled={!ids.length} onClick={() => secrets('copy_line')}>复制整行</Button><Button icon={FileKey2} size="sm" disabled={!ids.length} onClick={() => secrets('access_token')}>复制 Token</Button><Button icon={Archive} size="sm" disabled={!ids.length} onClick={() => action('/api/accounts/archive-bulk', { account_ids: ids, archived: archived !== 'only' }, '归档状态已更新', `确定更新选中 ${ids.length} 个账号的归档状态吗？`)}>归档/恢复</Button><Button icon={NotebookPen} size="sm" disabled={!ids.length} onClick={() => { setNoteTarget({ bulk: true }); setNote(''); }}>批量备注</Button><Button icon={FileKey2} size="sm" disabled={!ids.length} onClick={generateAgents}>生成 Agent</Button><Button icon={Download} size="sm" disabled={!ids.length} onClick={downloadAgents}>下载 Agent</Button><Button icon={Upload} size="sm" disabled={!ids.length} onClick={uploadAgents}>上传 sub2</Button><Button icon={Download} size="sm" disabled={!ids.length} onClick={downloadCpa}>下载 CPA</Button><Button icon={Trash2} variant="danger" size="sm" disabled={!ids.length} onClick={() => action('/api/accounts/delete-bulk', { account_ids: ids }, '已删除选中账号', `确定删除选中的 ${ids.length} 个账号吗？此操作不可恢复。`)}>删除</Button></Toolbar>
      {error ? <ErrorState message={error} onRetry={loadAccounts} /> : null}
      <Table className="accounts-table"><thead><tr><th className="check-col"><Checkbox checked={rows.length > 0 && rows.every((row) => selected.has(Number(row.id)))} onChange={toggleAll} /></th><th>账号</th><th>状态</th><th>套餐</th><th>Codex</th><th>最近活动</th><th>备注</th><th className="actions-col">操作</th></tr></thead><tbody>{loading ? <tr><td colSpan="8"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : rows.length === 0 ? <tr><td colSpan="8"><EmptyState title="没有匹配账号" description="尝试清空搜索，或从邮箱池导入新的账号素材。" /></td></tr> : rows.map((row) => <tr key={row.id}><td><Checkbox checked={selected.has(Number(row.id))} onChange={(checked) => toggle(row.id, checked)} /></td><td><strong>{row.email || `账号 #${row.id}`}</strong><small className="table-sub">#{row.id} · {row.email_source || '未知来源'}</small></td><td><div className="status-stack">{row.archived ? <StatusPill value="disabled">已归档</StatusPill> : <StatusPill value={row.live_check_status || (row.has_access_token ? 'available' : 'unknown')} />}{row.totp_enabled ? <span className="micro-badge">2FA</span> : null}</div></td><td><strong>{row.current_plan_type || row.plan_type || '—'}</strong>{row.plan_expires_at || row.expires_at ? <small className="table-sub">至 {formatDate(row.plan_expires_at || row.expires_at)}</small> : null}</td><td><StatusPill value={row.codex_status || 'not_authorized'} />{row.codex_agent_status ? <small className="table-sub">Agent: {row.codex_agent_status}</small> : null}</td><td>{formatDate(row.live_checked_at || row.plan_checked_at || row.created_at)}</td><td className="truncate" title={row.note || ''}>{row.note || '—'}</td><td><div className="row-actions"><Button icon={HeartPulse} size="sm" onClick={() => action('/api/accounts/check-live-bulk', { account_ids: [row.id] }, '查活任务已入队')}>查活</Button><Button icon={RefreshCw} size="sm" onClick={() => checkPlan(row.id)}>套餐</Button><Button icon={Link2} size="sm" onClick={() => extractLink(row.id)}>提链</Button><Button icon={FileKey2} size="sm" onClick={() => generateAgent(row.id)}>Agent</Button><IconButton label="下载 Agent" icon={Download} onClick={() => downloadAgent(row.id)} /><IconButton label="上传 Agent 到 sub2" icon={Upload} onClick={() => uploadAgent(row.id)} /><IconButton label="查活日志" icon={ActivityIcon} onClick={() => openLog(row, 'live')} /><IconButton label="补跑日志" icon={FileKey2} onClick={() => openLog(row, 'retry')} /><IconButton label="编辑备注" icon={NotebookPen} onClick={() => { setNoteTarget(row); setNote(row.note || ''); }} /><IconButton label="归档" icon={Archive} onClick={() => action(`/api/accounts/${row.id}/archive`, { archived: !row.archived }, row.archived ? '账号已恢复' : '账号已归档')} /><IconButton label="删除" icon={Trash2} onClick={() => action(`/api/accounts/${row.id}/delete`, {}, '账号已删除')} /></div></td></tr>)}</tbody></Table>
      <Pager page={page} pageSize={size} total={total} onPageChange={setPage} onPageSizeChange={(value) => { setSize(value); setPage(1); }} />
    </Card>
    <Modal open={Boolean(noteTarget)} title={noteTarget?.bulk ? `给 ${ids.length} 个账号设置备注` : `编辑备注 · ${noteTarget?.email || ''}`} onClose={() => setNoteTarget(null)} footer={<><Button onClick={() => setNoteTarget(null)}>取消</Button><Button variant="primary" onClick={saveNote}>保存备注</Button></>}><label className="field-label">备注<textarea rows="5" value={note} onChange={(event) => setNote(event.target.value)} placeholder="记录来源、问题或后续动作…" maxLength={2000} /></label></Modal>
    <Modal open={Boolean(log)} title={log?.title || '日志'} onClose={() => setLog(null)} wide footer={<Button onClick={() => setLog(null)}>关闭</Button>}><pre className="log-viewer">{log?.text}</pre></Modal>
  </div>;
}

function ActivityIcon(props) { return <HeartPulse {...props} />; }
function SquareIcon(props) { return <Square {...props} />; }
