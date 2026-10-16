import { useCallback, useEffect, useState } from 'react';
import { Activity, Edit3, Network, Plus, Power, RefreshCw, Trash2 } from 'lucide-react';
import { del, get, patch, post } from '../api';
import { useDebounced } from '../hooks';
import {
  Button, Card, Checkbox, EmptyState, ErrorState, Input, Modal, Pager, SearchField, SectionHeader, Select,
  Table, Textarea, useConfirm,
} from '../components/ui';

function displayProxy(value) {
  try {
    const url = new URL(value);
    const port = url.port ? `:${url.port}` : '';
    return `${url.protocol}//${url.hostname}${port}`;
  } catch (_) {
    return value;
  }
}

function proxyScheme(value) {
  try { return new URL(value).protocol.replace(':', '').toUpperCase(); } catch (_) { return '未知'; }
}

function normalizeLines(value) {
  return String(value || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

export default function ProxyPage({ notify }) {
  const confirm = useConfirm();
  const [proxies, setProxies] = useState([]);
  const [selected, setSelected] = useState(new Set());
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [editOpen, setEditOpen] = useState(false);
  const [editRecord, setEditRecord] = useState(null);
  const [editValue, setEditValue] = useState('');
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchValue, setBatchValue] = useState('');
  const [testResults, setTestResults] = useState({});
  const [testing, setTesting] = useState(false);
  const debouncedQuery = useDebounced(query);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await get('/api/proxies', {
        page,
        page_size: pageSize,
        q: debouncedQuery,
        status: statusFilter,
      });
      setProxies(response.items || []);
      setTotal(Number(response.total || 0));
      setSelected(new Set());
      setError('');
    } catch (loadError) {
      setError(loadError.message || '代理IP读取失败');
    } finally {
      setLoading(false);
    }
  }, [debouncedQuery, page, pageSize, statusFilter]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { setPage(1); }, [debouncedQuery, statusFilter]);

  const allSelected = proxies.length > 0 && proxies.every((item) => selected.has(item.id));
  const selectedRows = proxies.filter((item) => selected.has(item.id));

  function toggle(id, checked) {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id); else next.delete(id);
      return next;
    });
  }

  function toggleAll(checked) {
    setSelected(checked ? new Set(proxies.map((item) => item.id)) : new Set());
  }

  function openAdd() {
    setEditRecord(null);
    setEditValue('');
    setEditOpen(true);
  }

  function openEdit(record) {
    setEditRecord(record);
    setEditValue(record.proxy || '');
    setEditOpen(true);
  }

  async function submitEdit() {
    const value = String(editValue || '').trim();
    if (!value) return notify?.('代理地址不能为空', 'warning');
    setBusy(true);
    try {
      if (editRecord) {
        await patch(`/api/proxies/${encodeURIComponent(editRecord.id)}`, { proxy: value });
        notify?.('代理IP已更新', 'success');
      } else {
        await post('/api/proxies', { proxy: value });
        notify?.('代理IP已新增', 'success');
      }
      setEditOpen(false);
      await load();
    } catch (actionError) {
      notify?.(actionError.message || '代理IP保存失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(record) {
    setBusy(true);
    try {
      await patch(`/api/proxies/${encodeURIComponent(record.id)}`, { enabled: !record.enabled });
      notify?.(record.enabled ? '代理IP已禁用' : '代理IP已启用', 'success');
      await load();
    } catch (actionError) {
      notify?.(actionError.message || '代理IP状态更新失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function deleteOne(record) {
    if (!await confirm(`确定删除代理IP ${displayProxy(record.proxy)} 吗？`)) return;
    setBusy(true);
    try {
      await del(`/api/proxies/${encodeURIComponent(record.id)}`);
      notify?.('代理IP已删除', 'success');
      await load();
    } catch (actionError) {
      notify?.(actionError.message || '代理IP删除失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function deleteSelected() {
    if (!selectedRows.length) return notify?.('请先选择代理IP', 'warning');
    if (!await confirm(`确定删除选中的 ${selectedRows.length} 个代理IP吗？`)) return;
    setBusy(true);
    try {
      await del('/api/proxies', { ids: selectedRows.map((item) => item.id) });
      notify?.(`已删除 ${selectedRows.length} 个代理IP`, 'success');
      await load();
    } catch (actionError) {
      notify?.(actionError.message || '批量删除失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  function openBatchEdit() {
    if (!selectedRows.length) return notify?.('请先选择代理IP', 'warning');
    setBatchValue(selectedRows.map((item) => item.proxy).join('\n'));
    setBatchOpen(true);
  }

  async function submitBatchEdit() {
    const values = normalizeLines(batchValue);
    if (values.length !== selectedRows.length) {
      return notify?.(`请提供 ${selectedRows.length} 行代理地址，与选中数量一致`, 'warning');
    }
    setBusy(true);
    try {
      await patch('/api/proxies/bulk', {
        items: selectedRows.map((item, index) => ({ id: item.id, proxy: values[index] })),
      });
      notify?.(`已更新 ${selectedRows.length} 个代理IP`, 'success');
      setBatchOpen(false);
      await load();
    } catch (actionError) {
      notify?.(actionError.message || '批量编辑失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function runTest(rows) {
    if (!rows.length) return notify?.('请先选择代理IP', 'warning');
    if (rows.length > 20) return notify?.('每次最多测试 20 条代理，请分批测试。', 'warning');
    setTesting(true);
    setTestResults((current) => ({
      ...current,
      ...Object.fromEntries(rows.map((row) => [row.id, { status: 'testing' }])),
    }));
    try {
      const response = await post('/api/proxies/test', { proxies: rows.map((row) => row.proxy) });
      const items = response.items || [];
      setTestResults((current) => ({
        ...current,
        ...Object.fromEntries(rows.map((row, index) => [row.id, items[index] || { status: 'failed', ok: false, message: '未返回测试结果' }])),
      }));
    } catch (testError) {
      setTestResults((current) => ({
        ...current,
        ...Object.fromEntries(rows.map((row) => [row.id, { status: 'failed', ok: false, message: testError.message || '测试失败' }])),
      }));
      notify?.(testError.message || '代理测试失败', 'error');
    } finally {
      setTesting(false);
    }
  }

  return <div className="page-stack proxy-page">
    <div className="page-intro">
      <div><div className="eyebrow"><Network size={14} /> 网络与出口</div><h2>代理IP</h2></div>
      <div className="intro-actions">
        <Button icon={RefreshCw} onClick={load} loading={loading} disabled={busy || testing}>重新读取</Button>
        <Button icon={Activity} onClick={() => runTest(selectedRows)} loading={testing} disabled={loading || busy || testing || !selectedRows.length}>测试选中</Button>
        <Button icon={Trash2} variant="danger" onClick={deleteSelected} disabled={loading || busy || testing || !selectedRows.length}>批量删除</Button>
        <Button icon={Plus} onClick={openAdd} disabled={loading || busy || testing}>新增代理</Button>
        <Button icon={Edit3} onClick={openBatchEdit} disabled={loading || busy || testing || !selectedRows.length}>批量编辑</Button>
      </div>
    </div>
    {error ? <ErrorState message={error} onRetry={load} /> : null}
    <Card className="proxy-page-card">
      <SectionHeader title="代理IP列表" />
      <div className="proxy-toolbar"><SearchField value={query} onChange={setQuery} placeholder="搜索代理地址…" /><Select value={statusFilter} onChange={setStatusFilter} options={[{ value: '', label: '全部状态' }, { value: 'enabled', label: '已启用' }, { value: 'disabled', label: '已禁用' }]} /><span className="selection-note">已选 {selectedRows.length}</span></div>
      <Table className="proxy-table"><thead><tr><th><Checkbox checked={allSelected} onChange={toggleAll} ariaLabel="选择当前页代理IP" /></th><th>代理IP</th><th>协议</th><th>状态</th><th>测试结果</th><th>操作</th></tr></thead><tbody>
        {loading ? <tr><td colSpan="6"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : proxies.length ? proxies.map((record) => {
          const result = testResults[record.id];
          return <tr key={record.id}>
            <td><Checkbox checked={selected.has(record.id)} onChange={(checked) => toggle(record.id, checked)} ariaLabel={`选择代理IP ${displayProxy(record.proxy)}`} /></td>
            <td className="mono" title={record.proxy}>{displayProxy(record.proxy)}</td>
            <td>{proxyScheme(record.proxy)}</td>
            <td><span className={`status-pill ${record.enabled ? 'status-available' : 'status-disabled'}`}>{record.enabled ? '已启用' : '已禁用'}</span></td>
            <td className="proxy-inline-result">{!result ? <span className="table-sub">未测试</span> : result.status === 'testing' ? <span className="table-sub">测试中…</span> : <><span className={`status-pill ${result.ok ? 'status-alive' : 'status-dead'}`}>{result.ok ? '通过' : '失败'}</span><small>{result.exit_ip ? `出口 IP：${result.exit_ip}` : result.message || '无结果'}{result.network?.latency_ms != null ? ` · ${result.network.latency_ms} ms` : ''}</small></>}</td>
            <td><div className="row-actions"><Button size="sm" icon={Activity} loading={testing} onClick={() => runTest([record])}>测试</Button><Button size="sm" icon={Power} onClick={() => toggleEnabled(record)} disabled={busy || testing}>{record.enabled ? '禁用' : '启用'}</Button><Button size="sm" icon={Edit3} onClick={() => openEdit(record)} disabled={busy || testing}>编辑</Button><Button size="sm" icon={Trash2} variant="danger" onClick={() => deleteOne(record)} disabled={busy || testing}>删除</Button></div></td>
          </tr>;
        }) : <tr><td colSpan="6"><EmptyState title="暂无代理IP" description="点击右上角“新增代理”开始添加。" action={<Button icon={Plus} size="sm" onClick={openAdd}>新增代理</Button>} /></td></tr>}
      </tbody></Table>
      <Pager page={page} pageSize={pageSize} total={total} onPageChange={setPage} onPageSizeChange={(value) => { setPageSize(value); setPage(1); }} sizes={[20, 50, 100]} />
    </Card>
    <Modal open={editOpen} title={editRecord ? '编辑代理IP' : '新增代理IP'} onClose={() => !busy && setEditOpen(false)} footer={<><Button disabled={busy} onClick={() => setEditOpen(false)}>取消</Button><Button variant="primary" loading={busy} onClick={submitEdit}>{editRecord ? '更新' : '新增'}</Button></>}><label className="field-label"><span>代理地址</span><Input value={editValue} onChange={(event) => setEditValue(event.target.value)} placeholder="http://host:port 或 socks5h://user:pass@host:port" autoComplete="off" /></label></Modal>
    <Modal open={batchOpen} title={`批量编辑代理IP（已选 ${selectedRows.length} 个）`} onClose={() => !busy && setBatchOpen(false)} footer={<><Button disabled={busy} onClick={() => setBatchOpen(false)}>取消</Button><Button variant="primary" loading={busy} onClick={submitBatchEdit}>更新</Button></>}><label className="field-label"><span>新的代理地址（每行一个）</span><Textarea rows={8} value={batchValue} onChange={(event) => setBatchValue(event.target.value)} placeholder="http://host:port\nsocks5h://user:pass@host:port" /></label></Modal>
  </div>;
}
