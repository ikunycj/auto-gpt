import { useCallback, useEffect, useMemo, useState } from 'react';
import { Ban, CheckCircle2, ClipboardList, FileText, Play, RefreshCw, RotateCcw, Square, Trash2, UsersRound } from 'lucide-react';
import { get, post } from '../api';
import { useDebounced, usePolling } from '../hooks';
import {
  Button, Card, Checkbox, EmptyState, ErrorState, InlineNotice, LoadingRows, Modal, NumberField,
  Pager, RefreshButton, SectionHeader, Stat, StatusPill, Table, Toolbar,
} from '../components/ui';

function date(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', { hour12: false });
}

export default function RegisterPage({ notify }) {
  const [jobs, setJobs] = useState([]);
  const [waitingOtp, setWaitingOtp] = useState([]);
  const [jobMeta, setJobMeta] = useState({ total: 0, status_counts: {} });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [count, setCount] = useState('1');
  const [workers, setWorkers] = useState('3');
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [log, setLog] = useState(null);
  const [otp, setOtp] = useState({});
  const debouncedPage = useDebounced(page, 0);

  const loadJobs = useCallback(async () => {
    try {
      const [response, otpResponse] = await Promise.all([
        get('/api/jobs', { paged: 1, page: debouncedPage, page_size: pageSize }),
        get('/api/manual-otp/waiting'),
      ]);
      setJobs(response.items || []);
      setJobMeta(response);
      setWaitingOtp(otpResponse.waiting || []);
      setError('');
      setSelected((previous) => new Set([...previous].filter((id) => (response.items || []).some((job) => Number(job.id) === id))));
    } catch (loadError) { setError(loadError.message); }
    finally { setLoading(false); }
  }, [debouncedPage, pageSize]);
  const { refresh: refreshJobs, running } = usePolling(loadJobs, 3000, true);
  useEffect(() => { setPage(1); }, [pageSize]);
  useEffect(() => {
    if (!log?.jobId) return undefined;
    let disposed = false;
    const pollLog = async () => {
      try { const response = await get(`/api/jobs/${encodeURIComponent(log.jobId)}/log`); if (!disposed) setLog((current) => current ? { ...current, text: response.log || '暂无日志' } : current); } catch (_) { /* task may disappear while the modal is open */ }
    };
    const timer = window.setInterval(pollLog, 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [log?.jobId]);

  const counts = jobMeta.status_counts || {};
  const activeCount = Number(counts.active || 0);
  const selectedIds = useMemo(() => [...selected], [selected]);
  const toggleAll = (checked) => setSelected(checked ? new Set(jobs.filter((job) => !['running', 'stopping'].includes(job.status)).map((job) => Number(job.id))) : new Set());
  const toggle = (id, checked) => setSelected((previous) => { const next = new Set(previous); checked ? next.add(Number(id)) : next.delete(Number(id)); return next; });

  async function startJobs() {
    const normalizedCount = Math.max(1, Math.min(200, Number(count) || 1));
    const normalizedWorkers = Math.max(1, Math.min(16, Number(workers) || 1));
    setSubmitting(true);
    try {
      const response = await post('/api/jobs', { count: normalizedCount, workers: normalizedWorkers });
      notify(response.warning ? `已提交 ${response.submitted} 个任务：${response.warning}` : `已提交 ${response.submitted} 个注册任务`, response.warning ? 'warning' : 'success');
      await loadJobs();
    } catch (error) { notify(error.message, 'error'); }
    finally { setSubmitting(false); }
  }

  async function action(path, body, message, confirmMessage = '') {
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    try { await post(path, body); notify(message, 'success'); await loadJobs(); }
    catch (error) { notify(error.message, 'error'); }
  }

  async function submitOtp(job) {
    const code = String(otp[job.id] || '').trim();
    if (!code) return notify('请输入验证码', 'warning');
    try { await post('/api/manual-otp', { job_id: job.id, email: job.email, code }); notify('验证码已提交', 'success'); setOtp((current) => ({ ...current, [job.id]: '' })); await loadJobs(); }
    catch (error) { notify(error.message, 'error'); }
  }

  async function openLog(job) {
    try { const result = await get(`/api/jobs/${encodeURIComponent(job.id)}/log`); setLog({ jobId: job.id, title: `任务 #${job.id} · ${job.email || ''}`, text: result.log || '暂无日志' }); }
    catch (error) { notify(error.message, 'error'); }
  }

  return <div className="page-stack">
    <div className="page-intro"><div><div className="eyebrow"><ClipboardList size={14} /> CONTROL ROOM</div><h2>注册任务</h2><p>把注册、验证码和失败重试集中在同一条可追踪的工作流里。</p></div><div className="intro-actions"><Button icon={RefreshCw} onClick={() => refreshJobs()} loading={running}>刷新任务</Button></div></div>
    <div className="stats-grid"><Stat label="任务总数" value={jobMeta.total || 0} icon={ClipboardList} /><Stat label="进行中" value={activeCount} tone="amber" icon={RefreshCw} /><Stat label="成功" value={counts.success || counts.completed || 0} tone="green" icon={CheckCircle2} /><Stat label="失败" value={counts.failed || 0} tone="red" icon={Ban} /><Stat label="已停止" value={counts.stopped || counts.cancelled || 0} tone="neutral" icon={Square} /></div>
    <Card className="command-card"><SectionHeader title="启动一批注册" description="任务会进入后端队列，页面会自动刷新状态。" /><div className="command-row"><NumberField label="任务数量" value={count} min={1} max={200} onChange={setCount} /><NumberField label="并发线程" value={workers} min={1} max={16} onChange={setWorkers} /><div className="command-help"><UsersRound size={16} /><span>建议并发 1–3，遇到邮箱或浏览器服务限流时降低线程数。</span></div><Button variant="primary" icon={Play} onClick={startJobs} loading={submitting}>开始注册</Button></div></Card>
    {waitingOtp.length ? <InlineNotice tone="warning">有 {waitingOtp.length} 个任务正在等待手动验证码：{waitingOtp.slice(0, 3).map((item) => item.email || item).join('、')}{waitingOtp.length > 3 ? '…' : ''}</InlineNotice> : null}
    {error ? <ErrorState message={error} onRetry={loadJobs} /> : null}
    <Card className="table-card"><SectionHeader title="任务队列" description={`${activeCount} 个任务正在处理，运行中任务不能删除。`} actions={<div className="section-actions"><Button icon={RotateCcw} size="sm" disabled={!selectedIds.length} onClick={() => action('/api/jobs/retry-bulk', { job_ids: selectedIds, workers: Number(workers) || 1 }, `已提交 ${selectedIds.length} 个重试任务`)}>重试选中</Button><Button icon={Trash2} size="sm" variant="danger" disabled={!selectedIds.length} onClick={() => action('/api/jobs/delete-bulk', { job_ids: selectedIds }, '已删除可删除任务', `确定删除选中的 ${selectedIds.length} 个任务吗？对应日志也会被删除。`)}>删除选中</Button><Button icon={Ban} size="sm" onClick={() => action('/api/jobs/cancel-pending', {}, '已取消排队任务', '确定取消所有排队中的任务吗？')}>取消排队</Button></div>} />
      <Toolbar><span className="selection-note">已选 {selectedIds.length} 个</span><span className="toolbar-spacer" /><RefreshButton onClick={refreshJobs} loading={running} /></Toolbar>
      <Table className="jobs-table"><thead><tr><th className="check-col"><Checkbox checked={jobs.length > 0 && jobs.filter((job) => !['running', 'stopping'].includes(job.status)).every((job) => selected.has(Number(job.id)))} onChange={toggleAll} /></th><th>ID</th><th>邮箱</th><th>状态</th><th>开始时间</th><th>结果 / 错误</th><th className="actions-col">操作</th></tr></thead><tbody>{loading ? <LoadingRows columns={7} /> : jobs.length === 0 ? <tr><td colSpan="7"><EmptyState title="队列为空" description="设置数量后启动第一批注册任务。" /></td></tr> : jobs.map((job) => { const locked = ['running', 'stopping'].includes(job.status); return <tr key={job.id}><td><Checkbox checked={selected.has(Number(job.id))} disabled={locked} onChange={(checked) => toggle(job.id, checked)} /></td><td className="mono">#{job.id}</td><td><strong>{job.email || '等待分配邮箱'}</strong>{job.retry_attempt ? <small className="table-sub">第 {job.retry_attempt} 次</small> : null}</td><td><StatusPill value={job.status} /></td><td>{date(job.started_at || job.created_at)}</td><td className="truncate" title={job.error_message || ''}>{job.error_message || job.display_status || '—'}</td><td><div className="row-actions"><Button icon={FileText} size="sm" onClick={() => openLog(job)}>日志</Button>{job.manual_otp_required && <div className="otp-inline"><input aria-label="邮箱验证码" placeholder="验证码" maxLength={8} value={otp[job.id] || ''} onChange={(event) => setOtp((current) => ({ ...current, [job.id]: event.target.value }))} /><Button icon={CheckCircle2} size="sm" onClick={() => submitOtp(job)}>提交</Button></div>}{!locked && job.retryable ? <Button icon={RotateCcw} size="sm" onClick={() => action(`/api/jobs/${job.id}/retry`, { workers: Number(workers) || 1 }, '重试已提交')}>重试</Button> : null}{locked ? <Button icon={Square} size="sm" onClick={() => action(`/api/jobs/${job.id}/stop`, {}, '已发送停止请求')}>停止</Button> : null}{!locked ? <Button icon={Trash2} size="sm" variant="danger" onClick={() => action(`/api/jobs/${job.id}/delete`, {}, '任务已删除', '确定删除这个任务吗？对应日志也会被删除。')}>删除</Button> : null}</div></td></tr>; })}</tbody></Table>
      <Pager page={page} pageSize={pageSize} total={jobMeta.total || 0} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} />
    </Card>
    {jobMeta.warning ? <InlineNotice tone="warning">{jobMeta.warning}</InlineNotice> : null}
    <Modal open={Boolean(log)} title={log?.title || '任务日志'} onClose={() => setLog(null)} wide footer={<Button onClick={() => setLog(null)}>关闭</Button>}><pre className="log-viewer">{log?.text}</pre></Modal>
  </div>;
}
