import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity, ArrowDown, ArrowUp, Check, Clipboard, Database, Download, MailPlus, Minus, Plus,
  RefreshCw, Settings2, ShieldCheck, Smartphone, Trash2, Upload,
} from 'lucide-react';
import { copyText, del, download, get, patch, post, put } from '../api';
import { useDebounced, usePolling } from '../hooks';
import {
  Button, Card, Checkbox, EmptyState, ErrorState, IconButton, Modal, NumberField, Pager,
  RefreshButton, SearchField, SectionHeader, Select, StatusPill, Table, Toolbar,
} from '../components/ui';

const PAGE_SIZES = [20, 50, 100];
const ACTIVE_JOB_STATUSES = new Set(['pending', 'running', 'stopping', 'waiting_email', 'waiting_sms', 'waiting_totp', 'waiting_browser']);
const EMAIL_PREVIEW_MAX_LENGTH = 20;

const STATUS_TEXT = {
  registration: { registered: '已注册', unregistered: '未注册', registering: '注册中', failed: '注册失败' },
  codex: { authorized: '已授权', unauthorized: '未授权', authorizing: '授权中', failed: '授权失败' },
  phone: { verified: '已接码', unverified: '未接码', verifying: '接码中', failed: '接码失败' },
};
const GENERIC_STATUS_TEXT = { alive: '存活', dead: '失效', unknown: '未验活', available: '可用', error: '错误', platform: '自动取号', ready: '已启用', unavailable: '未就绪' };
const EMAIL_SOURCE_TEXT = {
  outlook: 'Outlook', generic_api: '通用接码 API', cloudflare_domain: 'Cloudflare 域名',
  cloudflare: 'Cloudflare Worker', gptmail: 'GPTMail', mailnest: 'MailNest', cloudmail: 'CloudMail',
};

function idOf(row) { return String(row?.relay_account_id || row?.id || row?.email || ''); }
function pretty(value) { return value == null || value === '' ? '—' : String(value); }
function maskedPreview(value) {
  const text = value == null ? '' : String(value);
  return text ? `${text.slice(0, 4)}...` : '—';
}
function emailPreview(value) {
  const text = value == null ? '' : String(value);
  const separator = text.lastIndexOf('@');
  if (separator <= 0 || separator === text.length - 1) return maskedPreview(text);
  const prefix = `${text.slice(0, Math.min(4, separator))}...@`;
  const domain = text.slice(separator + 1);
  const domainLimit = EMAIL_PREVIEW_MAX_LENGTH - prefix.length;
  if (domain.length <= domainLimit) return `${prefix}${domain}`;
  const tailLength = Math.min(7, Math.floor((domainLimit - 3) * 2 / 3));
  const headLength = domainLimit - tailLength - 3;
  return `${prefix}${domain.slice(0, headLength)}...${domain.slice(-tailLength)}`;
}
function urlHostPreview(value) {
  const text = value == null ? '' : String(value);
  if (!text) return '—';
  try {
    const hostname = new URL(text).hostname;
    return hostname ? `...${hostname}...` : maskedPreview(text);
  } catch (_) {
    return maskedPreview(text);
  }
}
function CopyValue({ value, label, notify, masked = false, preview, empty = '—' }) {
  const text = value == null ? '' : String(value);
  if (!text) return <span className="copy-cell-empty">{empty}</span>;
  const copy = async () => {
    try {
      await copyText(text);
      notify(`${label}已复制`, 'success');
    } catch (error) {
      notify(error.message || `${label}复制失败`, 'error');
    }
  };
  return <button type="button" className="copy-cell-button" title={`点击复制${label}`} aria-label={`复制${label}`} onClick={copy}>{preview ?? (masked ? maskedPreview(text) : text)}</button>;
}
function status(kind, value) {
  const normalized = String(value || 'unknown').toLowerCase();
  return <StatusPill value={normalized}>{STATUS_TEXT[kind]?.[normalized] || GENERIC_STATUS_TEXT[normalized] || pretty(value)}</StatusPill>;
}
function phonePoolState(row) {
  if (row.status) return String(row.status).toLowerCase();
  if (row.invalid) return 'invalid';
  if (row.reserved) return 'reserved';
  if (row.assigned) return 'bound';
  if (!row.candidate || Number(row.available_uses ?? row.remaining_uses ?? 0) <= 0) return 'used';
  return 'available';
}
function activeJob(row) {
  const job = row?.codex_job;
  return Boolean(job && ACTIVE_JOB_STATUSES.has(String(job.status || '').toLowerCase()));
}
function activeMaintenance(row) {
  const job = row?.maintenance_job;
  return Boolean(job && ACTIVE_JOB_STATUSES.has(String(job.status || '').toLowerCase()));
}

export default function RelayPage({ notify, summary, mode = 'all', embedded = false, onOpenSettings, onSummaryRefresh, onRegistrationRequestHandled, registrationRequested = false }) {
  const showPhones = mode === 'all' || mode === 'phones';
  const showAccounts = mode === 'all' || mode === 'accounts';
  const [accounts, setAccounts] = useState([]);
  const [phones, setPhones] = useState([]);
  const [accountTotal, setAccountTotal] = useState(0);
  const [phoneTotal, setPhoneTotal] = useState(0);
  const [accountQuery, setAccountQuery] = useState('');
  const [phoneQuery, setPhoneQuery] = useState('');
  const [registrationFilter, setRegistrationFilter] = useState('');
  const [codexFilter, setCodexFilter] = useState('');
  const [phoneFilter, setPhoneFilter] = useState('');
  const [gptFilter, setGptFilter] = useState('');
  const [providerFilter, setProviderFilter] = useState('');
  const [phoneStatus, setPhoneStatus] = useState('');
  const [accountPage, setAccountPage] = useState(1);
  const [phonePage, setPhonePage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [accountSelected, setAccountSelected] = useState(new Set());
  const [phoneSelected, setPhoneSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [manualOtpWaiting, setManualOtpWaiting] = useState([]);
  const [importMode, setImportMode] = useState(null);
  const [batchRegisterOpen, setBatchRegisterOpen] = useState(false);
  const [registrationCount, setRegistrationCount] = useState(1);
  const [registrationEmailSources, setRegistrationEmailSources] = useState([]);
  const [batchRegistering, setBatchRegistering] = useState(false);
  const [importText, setImportText] = useState('');
  const [logRow, setLogRow] = useState(null);
  const [logText, setLogText] = useState('');
  const [editAccount, setEditAccount] = useState(null);
  const [editDraft, setEditDraft] = useState({});
  const [workers, setWorkers] = useState(() => Number(localStorage.getItem('gpt_console_relay_workers') || 1));
  const [sub2Open, setSub2Open] = useState(false);
  const [sub2Services, setSub2Services] = useState([]);
  const [sub2ServiceId, setSub2ServiceId] = useState('');
  const [sub2Draft, setSub2Draft] = useState({ name: '', homepage: '', api_base: '', admin_key: '' });

  const accountQ = useDebounced(accountQuery);
  const phoneQ = useDebounced(phoneQuery);

  const load = useCallback(async () => {
    try {
      const [accountResponse, phoneResponse, manualOtpResponse] = await Promise.all([
        showAccounts ? get('/api/gpt-accounts', {
          q: accountQ,
          registration_status: registrationFilter,
          codex_status: codexFilter,
          phone_status: phoneFilter,
          gpt_status: gptFilter,
          provider: providerFilter,
          paged: 1,
          page: accountPage,
          page_size: pageSize,
        }) : Promise.resolve(null),
        showPhones ? get('/api/codex-relay/phones', {
          q: phoneQ, status: phoneStatus, paged: 1, page: phonePage, page_size: pageSize,
        }) : Promise.resolve(null),
        showAccounts ? get('/api/manual-otp/waiting') : Promise.resolve(null),
      ]);
      if (accountResponse) {
        const items = accountResponse.items || [];
        setAccounts(items);
        setAccountTotal(Number(accountResponse.total || 0));
        setAccountSelected((current) => new Set([...current].filter((id) => items.some((row) => idOf(row) === id))));
      }
      if (phoneResponse) {
        const items = phoneResponse.items || [];
        setPhones(items);
        setPhoneTotal(Number(phoneResponse.total || 0));
        setPhoneSelected((current) => new Set([...current].filter((id) => items.some((row) => String(row.id || '') === id))));
      }
      if (manualOtpResponse) setManualOtpWaiting(manualOtpResponse.waiting || []);
      setError('');
    } catch (loadError) {
      setError(loadError.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [showAccounts, showPhones, accountQ, registrationFilter, codexFilter, phoneFilter, gptFilter, providerFilter, accountPage, pageSize, phoneQ, phoneStatus, phonePage]);

  const { refresh, running } = usePolling(load, 2000, true);
  useEffect(() => { localStorage.setItem('gpt_console_relay_workers', String(workers)); }, [workers]);
  useEffect(() => {
    if (!registrationRequested) return;
    setBatchRegisterOpen(true);
    onRegistrationRequestHandled?.();
  }, [onRegistrationRequestHandled, registrationRequested]);
  useEffect(() => setAccountPage(1), [accountQ, registrationFilter, codexFilter, phoneFilter, gptFilter, providerFilter]);
  useEffect(() => setPhonePage(1), [phoneQ, phoneStatus]);

  useEffect(() => {
    if (!logRow) return undefined;
    let disposed = false;
    const poll = async () => {
      try {
        const response = await get(`/api/gpt-accounts/${encodeURIComponent(logRow.id)}/log`);
        if (!disposed) setLogText(response.log || '暂无日志');
      } catch (_) {
        // The task may be removed while the account row remains visible.
      }
    };
    poll();
    const timer = window.setInterval(poll, 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [logRow]);

  const accountIds = useMemo(() => [...accountSelected], [accountSelected]);
  const phoneIds = useMemo(() => [...phoneSelected], [phoneSelected]);
  const selectableAccounts = useMemo(() => accounts.filter((row) => row.email), [accounts]);
  const registrationEmail = summary?.registration_email || {};
  const registrationSources = Array.isArray(registrationEmail.sources) ? registrationEmail.sources : [];
  const registrationChannels = Array.isArray(registrationEmail.channels) && registrationEmail.channels.length
    ? registrationEmail.channels
    : registrationSources.map((source) => ({ id: source, label: EMAIL_SOURCE_TEXT[source] || source, ready: Boolean(registrationEmail.automatic), message: '保存设置后由后端检查渠道状态' }));
  const allRegistrationChannels = Array.isArray(registrationEmail.all_channels) && registrationEmail.all_channels.length
    ? registrationEmail.all_channels
    : registrationChannels;
  const readyRegistrationSources = new Set(allRegistrationChannels.filter((channel) => channel.ready).map((channel) => channel.id));
  const selectedRegistrationChannels = registrationEmailSources
    .filter((source) => readyRegistrationSources.has(source))
    .map((source) => allRegistrationChannels.find((channel) => channel.id === source))
    .filter(Boolean);
  const usingManualRegistration = !selectedRegistrationChannels.length && !registrationEmail.automatic && Boolean(registrationEmail.manual_configured);
  const registrationReady = selectedRegistrationChannels.length > 0 || usingManualRegistration;
  const registrationSourceText = selectedRegistrationChannels.map((channel) => channel.label || EMAIL_SOURCE_TEXT[channel.id] || channel.id).join(' → ') || (usingManualRegistration ? '手动邮箱' : '未选择');
  const registrationMaxCount = usingManualRegistration ? 1 : 200;

  useEffect(() => {
    if (!batchRegisterOpen) return;
    const ready = new Set(allRegistrationChannels.filter((channel) => channel.ready).map((channel) => channel.id));
    const defaults = registrationEmail.automatic
      ? registrationSources.filter((source) => ready.has(source))
      : [];
    setRegistrationEmailSources(defaults);
    onSummaryRefresh?.();
    // Defaults are intentionally reset only when the dialog opens. Summary
    // polling must not overwrite a user's in-progress selection or ordering.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchRegisterOpen]);
  const toggle = (setter, id, checked) => setter((current) => {
    const next = new Set(current);
    if (checked) next.add(id); else next.delete(id);
    return next;
  });

  async function runAction(path, body, message, method = 'post') {
    if (method === 'delete' && !window.confirm('确定删除选中的数据吗？此操作不可恢复。')) return;
    try {
      const fn = method === 'delete' ? del : method === 'patch' ? patch : post;
      await fn(path, body);
      notify(message, 'success');
      await load();
    } catch (actionError) {
      notify(actionError.message || '操作失败', 'error');
    }
  }

  async function submitImport() {
    if (!importText.trim()) return notify('请粘贴导入内容', 'warning');
    try {
      const response = importMode === 'phones'
        ? await post('/api/codex-relay/phones/import', { text: importText })
        : await post('/api/codex-relay/import', { text: importText, format: 'auto' });
      notify(response.message || '导入完成', 'success');
      setImportMode(null);
      setImportText('');
      await load();
    } catch (actionError) {
      notify(actionError.message || '导入失败', 'error');
    }
  }

  async function registerSelected(ids = accountIds) {
    const registerable = ids.filter((id) => accounts.some((row) => idOf(row) === id && ['unregistered', 'failed'].includes(row.registration_status)));
    if (!registerable.length) return notify('请先选择未注册账号', 'warning');
    await runAction('/api/gpt-accounts/register', { account_ids: registerable, workers }, '注册任务已启动');
  }

  async function registerNewAccounts() {
    if (!registrationReady) return notify('请至少选择一个可用邮箱渠道', 'warning');
    const count = Math.max(1, Math.min(registrationMaxCount, Number(registrationCount) || 1));
    setBatchRegistering(true);
    try {
      const payload = { count, workers };
      if (selectedRegistrationChannels.length) payload.email_sources = selectedRegistrationChannels.map((channel) => channel.id);
      const response = await post('/api/jobs', payload);
      const submitted = Number(response.submitted || 0);
      setBatchRegisterOpen(false);
      notify(response.warning || `已从 ${registrationSourceText} 启动 ${submitted} 个注册任务`, response.warning ? 'warning' : 'success');
      await load();
      await onSummaryRefresh?.();
    } catch (actionError) {
      notify(actionError.message || '启动注册任务失败', 'error');
    } finally {
      setBatchRegistering(false);
    }
  }

  function toggleRegistrationSource(source) {
    if (!readyRegistrationSources.has(source)) return;
    setRegistrationEmailSources((current) => current.includes(source)
      ? current.filter((item) => item !== source)
      : [...current, source]);
  }

  function moveRegistrationSource(source, direction) {
    setRegistrationEmailSources((current) => {
      const index = current.indexOf(source);
      const nextIndex = index + direction;
      if (index < 0 || nextIndex < 0 || nextIndex >= current.length) return current;
      const next = [...current];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  }

  async function authorizeSelected(ids = accountIds) {
    const selectedIds = ids.filter((id) => accounts.some((row) => idOf(row) === id && row.email));
    if (!selectedIds.length) return notify('请先选择 GPT 账号', 'warning');
    await runAction('/api/gpt-accounts/authorize', { account_ids: selectedIds, workers, phone_ids: [] }, 'Codex 授权任务已启动，手机号池资源已预留');
  }

  async function softDeleteAccounts(ids = accountIds) {
    const selectedIds = [...new Set(ids.map(String).filter(Boolean))];
    if (!selectedIds.length) return notify('请先选择 GPT 账号', 'warning');
    const label = selectedIds.length === 1 ? '这个 GPT 账号' : `选中的 ${selectedIds.length} 个 GPT 账号`;
    if (!window.confirm(`确定软删除${label}吗？账号将从列表隐藏，底层数据和日志仍会保留。`)) return;
    try {
      const response = await del('/api/gpt-accounts', { account_ids: selectedIds });
      const deletedCount = Number(response.deleted_count ?? response.deleted ?? 0);
      if (!deletedCount) return notify('没有 GPT 账号被删除', 'warning');
      setAccountSelected((current) => {
        const next = new Set(current);
        selectedIds.forEach((id) => next.delete(id));
        return next;
      });
      notify(response.message || `已软删除 ${deletedCount} 个 GPT 账号`, 'success');
      if (deletedCount >= accounts.length && accountPage > 1) setAccountPage(accountPage - 1);
      else await load();
    } catch (actionError) {
      notify(actionError.message || '删除 GPT 账号失败', 'error');
    }
  }

  async function maintenance(actionName) {
    const relayIds = accountIds.map((id) => accounts.find((row) => idOf(row) === id)?.relay_account_id).filter(Boolean);
    if (!relayIds.length) return notify('请先选择 Relay 账号', 'warning');
    await runAction('/api/codex-relay/accounts/actions', { account_ids: relayIds, action: actionName, workers }, '账号任务已入队');
  }

  async function exportCredentials(format, copy = false) {
    const relayIds = accountIds.map((id) => accounts.find((row) => idOf(row) === id)?.relay_account_id).filter(Boolean);
    if (!relayIds.length) return notify('所选账号没有可导出的 Codex 凭证', 'warning');
    try {
      if (copy) {
        const response = await post('/api/codex-relay/accounts/export/copy', { account_ids: relayIds, format });
        await copyText(response.content || '');
        notify(`已复制 ${response.count || 0} 条凭证`, 'success');
      } else {
        await download('/api/codex-relay/accounts/export/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ account_ids: relayIds, format }) });
      }
    } catch (actionError) {
      notify(actionError.message || '导出失败', 'error');
    }
  }

  async function openLog(row) {
    setLogRow(row);
    setLogText('加载日志…');
  }

  async function openEditor(row) {
    setEditAccount(row);
    setEditDraft({
      email: row.email || '',
      chatgpt_password: row.chatgpt_password || row.password || '',
      email_code_url: row.email_code_url || '',
      totp_secret: row.totp_secret || '',
      phone: row.phone_number || '',
      sms_code_url: row.sms_code_url || '',
      note: row.note || '',
    });
  }

  async function saveEditor() {
    if (!editAccount?.relay_account_id) return;
    try {
      await put(`/api/codex-relay/accounts/${encodeURIComponent(editAccount.relay_account_id)}`, editDraft);
      notify('账号已保存', 'success');
      setEditAccount(null);
      await load();
    } catch (actionError) {
      notify(actionError.message || '保存失败', 'error');
    }
  }

  async function submitVerification(job, stage) {
    const code = window.prompt(`${stage === 'sms' ? '短信' : stage === 'totp' ? '2FA' : '邮箱'}验证码`);
    if (!code?.trim()) return;
    await runAction(`/api/codex-relay/jobs/${encodeURIComponent(job.id)}/verification`, { stage, code: code.trim() }, '验证码已提交');
  }

  async function submitManualRegistrationOtp(waiting) {
    const code = window.prompt(`请输入 ${waiting.email || '注册邮箱'} 收到的验证码`);
    if (!code?.trim()) return;
    try {
      await post('/api/manual-otp', { email: waiting.email, job_id: waiting.job_id, code: code.trim() });
      setManualOtpWaiting((current) => current.filter((item) => String(item.email || '').toLowerCase() !== String(waiting.email || '').toLowerCase()));
      notify('邮箱验证码已提交', 'success');
      await load();
    } catch (actionError) {
      notify(actionError.message || '验证码提交失败', 'error');
    }
  }

  async function stopRow(row) {
    if (row.registration_job_id && row.registration_status === 'registering') {
      await runAction(`/api/jobs/${encodeURIComponent(row.registration_job_id)}/stop`, {}, '已发送注册停止请求');
    } else if (activeMaintenance(row)) {
      await runAction(`/api/codex-relay/jobs/${encodeURIComponent(row.maintenance_job.id)}/stop`, {}, '已发送账号维护停止请求');
    } else if (row.codex_job_kind === 'codex_retry') {
      await runAction(`/api/jobs/${encodeURIComponent(row.codex_job_id)}/stop`, {}, '已发送授权停止请求');
    } else if (row.codex_job_id) {
      await runAction(`/api/codex-relay/jobs/${encodeURIComponent(row.codex_job_id)}/stop`, {}, '已发送授权停止请求');
    }
  }

  async function browserFocus(job) {
    try {
      const response = await post(`/api/codex-relay/jobs/${encodeURIComponent(job.id)}/browser-focus`, {});
      if (response.url && /^https?:\/\//i.test(response.url)) window.open(response.url, '_blank', 'noopener,noreferrer');
      notify('已打开人工处理页面', 'success');
    } catch (actionError) {
      notify(actionError.message || '无法打开页面', 'error');
    }
  }

  async function loadSub2() {
    try {
      const response = await get('/api/codex-relay/sub2-services');
      const list = response.items || [];
      setSub2Services(list);
      const selected = sub2ServiceId || list[0]?.id || '';
      setSub2ServiceId(selected);
      const service = list.find((item) => item.id === selected);
      if (service) setSub2Draft({ name: service.name || '', homepage: service.homepage || '', api_base: service.api_base || '', admin_key: service.admin_key || '' });
    } catch (actionError) { notify(actionError.message || '加载 sub2 服务失败', 'error'); }
  }

  async function saveSub2() {
    try {
      const response = await post('/api/codex-relay/sub2-services', { ...sub2Draft, ...(sub2ServiceId ? { id: sub2ServiceId } : {}) });
      notify('sub2api 服务已保存', 'success');
      setSub2ServiceId(response.service?.id || sub2ServiceId);
      await loadSub2();
    } catch (actionError) { notify(actionError.message || '保存 sub2 服务失败', 'error'); }
  }

  async function deleteSub2() {
    if (!sub2ServiceId || !window.confirm('确定删除这个 sub2api 服务配置吗？')) return;
    try {
      await del(`/api/codex-relay/sub2-services/${encodeURIComponent(sub2ServiceId)}`);
      notify('sub2api 服务已删除', 'success');
      setSub2ServiceId('');
      setSub2Draft({ name: '', homepage: '', api_base: '', admin_key: '' });
      await loadSub2();
    } catch (actionError) { notify(actionError.message || '删除失败', 'error'); }
  }

  const phonePanel = showPhones ? <Card className="table-card relay-panel">
    <SectionHeader title="手机号接码池" description={`${phoneTotal} 个号码或动态来源，维护状态、剩余次数和绑定关系。`} actions={<div className="section-actions">
      <Button icon={Upload} size="sm" onClick={() => setImportMode('phones')}>导入手机号</Button>
      <Button icon={Minus} size="sm" disabled={!phoneIds.length} onClick={() => runAction('/api/codex-relay/phones/available-uses', { phone_ids: phoneIds, delta: -1 }, '已减少可用次数', 'patch')}>-1</Button>
      <Button icon={Plus} size="sm" disabled={!phoneIds.length} onClick={() => runAction('/api/codex-relay/phones/available-uses', { phone_ids: phoneIds, delta: 1 }, '已增加可用次数', 'patch')}>+1</Button>
      <Button icon={Trash2} variant="danger" size="sm" disabled={!phoneIds.length} onClick={() => runAction('/api/codex-relay/phones', { phone_ids: phoneIds }, '已删除未绑定号码', 'delete')}>删除</Button>
    </div>} />
    <Toolbar><SearchField value={phoneQuery} onChange={setPhoneQuery} placeholder="搜索尾号或绑定邮箱…" /><Select value={phoneStatus} onChange={setPhoneStatus} options={[{ value: '', label: '全部状态' }, { value: 'available', label: '可用' }, { value: 'bound', label: '已绑定' }, { value: 'reserved', label: '已预留' }, { value: 'used', label: '已用尽' }, { value: 'invalid', label: '已失效' }]} /><RefreshButton onClick={refresh} loading={running} /></Toolbar>
    <Table><thead><tr><th className="check-col"><Checkbox checked={phones.filter((row) => !row.special).length > 0 && phones.filter((row) => !row.special).every((row) => phoneSelected.has(String(row.id || '')))} onChange={(checked) => setPhoneSelected(checked ? new Set(phones.filter((row) => !row.special).map((row) => String(row.id || ''))) : new Set())} /></th><th>手机号 / 来源</th><th>状态</th><th>可用次数</th><th>绑定账号</th></tr></thead><tbody>
      {loading ? <tr><td colSpan="5"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : phones.length === 0 ? <tr><td colSpan="5"><EmptyState title="没有手机号" description="导入接码手机号后，Codex 授权遇到短信验证时会自动取号。" /></td></tr> : phones.map((row) => { const id = String(row.id || ''); const state = phonePoolState(row); const special = Boolean(row.special); return <tr key={id} className={special ? 'phone-pool-special-row' : ''}><td>{special ? <span className="phone-special-mark">平台</span> : <Checkbox checked={phoneSelected.has(id)} onChange={(checked) => toggle(setPhoneSelected, id, checked)} />}</td><td className={special ? 'phone-special-source' : 'mono'}><strong>{special ? pretty(row.label || row.provider_label || '平台自动取号') : pretty(row.phone)}</strong>{special ? <small className="table-sub">{row.provider_label || row.provider || '接码平台'} · 动态号码</small> : null}</td><td><StatusPill value={state}>{state === 'used' ? '已用尽' : undefined}</StatusPill></td><td><strong>{special ? '动态' : pretty(row.available_uses ?? row.remaining_uses)}</strong>{row.reserved_count ? <small className="table-sub">占用 {row.reserved_count} 个任务</small> : null}</td><td>{special ? pretty(row.message || '授权时自动取号') : pretty(row.assigned_account_email)}</td></tr>; })}
    </tbody></Table><Pager page={phonePage} pageSize={pageSize} total={phoneTotal} onPageChange={setPhonePage} onPageSizeChange={(value) => { setPageSize(value); setPhonePage(1); }} sizes={PAGE_SIZES} />
  </Card> : null;

  const accountPanel = showAccounts ? <Card className="table-card relay-panel gpt-account-panel">
    <SectionHeader title="GPT账号" actions={<div className="section-actions">
      <Button icon={Upload} size="sm" onClick={() => setImportMode('accounts')}>导入账号</Button>
      <Button icon={MailPlus} variant="primary" size="sm" onClick={() => setBatchRegisterOpen(true)}>注册新账号</Button>
      <Button icon={Activity} size="sm" disabled={!accountIds.length} onClick={() => registerSelected()}>注册选中</Button>
      <Button icon={RefreshCw} size="sm" disabled={!accountIds.length} onClick={() => authorizeSelected()}>授权选中</Button>
      <Button icon={ShieldCheck} size="sm" disabled={!accountIds.length} onClick={() => maintenance('enable_2fa')}>开启 2FA</Button>
      <Button icon={Activity} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_gpt_liveness')}>GPT 验活</Button>
      <Button icon={Activity} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_email_liveness')}>邮箱验活</Button>
      <Button icon={RefreshCw} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_quota')}>查限额</Button>
      <Button icon={Database} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_sub2_status')}>sub2 状态</Button>
      <Button icon={Clipboard} size="sm" disabled={!accountIds.length} onClick={() => exportCredentials('rt', true)}>复制凭证</Button>
      <Button icon={Download} size="sm" disabled={!accountIds.length} onClick={() => exportCredentials('rt')}>下载凭证</Button>
      <Button icon={Settings2} size="sm" onClick={() => { setSub2Open(true); loadSub2(); }}>sub2 服务</Button>
      <Button icon={Trash2} variant="danger" size="sm" disabled={!accountIds.length} onClick={() => softDeleteAccounts()}>删除</Button>
    </div>} />
    <Toolbar className="gpt-account-toolbar">
      <SearchField value={accountQuery} onChange={setAccountQuery} placeholder="搜索账号或备注…" />
      <Select value={providerFilter} onChange={setProviderFilter} options={[{ value: '', label: '邮箱来源' }, ...Object.entries(EMAIL_SOURCE_TEXT).map(([value, label]) => ({ value, label }))]} />
      <Select value={registrationFilter} onChange={setRegistrationFilter} options={[{ value: '', label: 'GPT注册状态' }, { value: 'registered', label: '已注册' }, { value: 'unregistered', label: '未注册' }, { value: 'registering', label: '注册中' }, { value: 'failed', label: '注册失败' }]} />
      <Select value={codexFilter} onChange={setCodexFilter} options={[{ value: '', label: 'Codex授权状态' }, { value: 'authorized', label: '已授权' }, { value: 'unauthorized', label: '未授权' }, { value: 'authorizing', label: '授权中' }, { value: 'failed', label: '授权失败' }]} />
      <Select value={phoneFilter} onChange={setPhoneFilter} options={[{ value: '', label: '手机接码状态' }, { value: 'verified', label: '已接码' }, { value: 'unverified', label: '未接码' }, { value: 'verifying', label: '接码中' }, { value: 'failed', label: '接码失败' }]} />
      <Select value={gptFilter} onChange={setGptFilter} options={[{ value: '', label: 'GPT状态' }, { value: 'alive', label: '存活' }, { value: 'dead', label: '失效' }, { value: 'unknown', label: '未验活' }]} />
      <span className="selection-note">已选 {accountIds.length}</span>
      <label className="worker-control">并发<input type="number" min="1" max="8" value={workers} onChange={(event) => setWorkers(Math.max(1, Math.min(8, Number(event.target.value) || 1)))} /></label>
      <RefreshButton onClick={refresh} loading={running} />
    </Toolbar>
    <Table className="relay-account-table gpt-account-table"><colgroup>
      <col className="col-check" /><col className="col-account" /><col className="col-secret" /><col className="col-secret" />
      <col className="col-api" /><col className="col-registration" /><col className="col-codex" /><col className="col-phone-status" />
      <col className="col-phone" /><col className="col-gpt-status" /><col className="col-plan" /><col className="col-note" />
      <col className="col-time" /><col className="col-actions" />
    </colgroup><thead><tr>
      <th className="check-col"><Checkbox checked={selectableAccounts.length > 0 && selectableAccounts.every((row) => accountSelected.has(idOf(row)))} onChange={(checked) => setAccountSelected(checked ? new Set(selectableAccounts.map(idOf)) : new Set())} /></th>
      <th>账号</th><th>密码</th><th>2FA</th><th>邮箱接码API</th><th>GPT注册状态</th><th>Codex授权状态</th><th>手机接码</th><th>手机</th><th>GPT状态</th><th>套餐</th><th>备注</th><th>时间</th><th className="actions-col">操作</th>
    </tr></thead><tbody>
      {loading ? <tr><td colSpan="14"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : accounts.length === 0 ? <tr><td colSpan="14"><EmptyState title="没有 GPT 账号" description="导入已有账号，或在设置中配置邮箱来源后启动注册任务。" /></td></tr> : accounts.map((row) => {
        const id = idOf(row);
        const codexJob = row.codex_job;
        const canRelay = Boolean(row.relay_account_id);
        const waitingCode = Boolean(codexJob && ['waiting_email', 'waiting_sms', 'waiting_totp'].includes(codexJob.status));
        const stage = codexJob?.status === 'waiting_sms' || codexJob?.stage === 'sms' ? 'sms' : codexJob?.status === 'waiting_totp' || codexJob?.stage === 'totp' ? 'totp' : 'email';
        const manualOtp = manualOtpWaiting.find((item) => String(item.email || '').toLowerCase() === String(row.email || '').toLowerCase());
        return <tr key={id}>
          <td><Checkbox checked={accountSelected.has(id)} onChange={(checked) => toggle(setAccountSelected, id, checked)} /></td>
          <td className="cell-nowrap"><strong><CopyValue value={row.email} label="账号" notify={notify} preview={emailPreview(row.email)} /></strong>{row.active_operation ? <small className="table-sub">{row.active_operation_label || '账号任务运行中'}</small> : null}</td>
          <td className="mono cell-nowrap"><CopyValue value={row.password} label="密码" notify={notify} masked empty={row.login_method === 'email_otp' ? 'OTP 登录' : '—'} /></td>
          <td className="mono cell-nowrap"><CopyValue value={row.totp_secret} label="2FA" notify={notify} masked /></td>
          <td className="url-cell"><CopyValue value={row.email_code_url} label="邮箱接码 API" notify={notify} preview={urlHostPreview(row.email_code_url)} empty={row.email_provider_label || EMAIL_SOURCE_TEXT[row.email_provider] || '—'} /></td>
          <td>{status('registration', row.registration_status)}</td>
          <td>{status('codex', row.codex_status)}</td>
          <td>{status('phone', row.phone_status)}</td>
          <td className="mono cell-nowrap"><CopyValue value={row.phone} label="手机" notify={notify} empty="" /></td>
          <td>{status('', row.gpt_status || 'unknown')}</td>
          <td className="cell-nowrap">{pretty(row.plan)}</td>
          <td className="note-cell" title={row.note || ''}>{pretty(row.note)}</td>
          <td className="time-cell"><small className="time-line">创建时间：{pretty(row.created_at)}</small><small className="time-line">修改时间：{pretty(row.updated_at)}</small></td>
          <td><div className="row-actions">
            {['unregistered', 'failed'].includes(row.registration_status) ? <Button aria-label="注册" title="注册" size="sm" onClick={() => registerSelected([id])}>注册</Button> : null}
            {row.registration_status === 'registered' && row.codex_status !== 'authorized' && !activeJob(row) && !activeMaintenance(row) ? <Button aria-label="授权" title="授权" size="sm" onClick={() => authorizeSelected([id])}>授权</Button> : null}
            {manualOtp ? <Button aria-label="提交邮箱验证码" title="提交邮箱验证码" size="sm" variant="primary" onClick={() => submitManualRegistrationOtp(manualOtp)}>邮箱验证码</Button> : null}
            {waitingCode ? <Button aria-label="提交验证码" title="提交验证码" size="sm" onClick={() => submitVerification(codexJob, stage)}>验证码</Button> : null}
            {codexJob?.status === 'waiting_browser' ? <><Button aria-label="打开处理页" title="打开处理页" size="sm" onClick={() => browserFocus(codexJob)}>处理页</Button><Button aria-label="继续任务" title="继续任务" size="sm" onClick={() => runAction(`/api/codex-relay/jobs/${encodeURIComponent(codexJob.id)}/browser-assist`, {}, '已通知继续')}>继续</Button></> : null}
            {(row.registration_status === 'registering' || activeJob(row) || activeMaintenance(row)) ? <Button aria-label="停止任务" title="停止任务" size="sm" onClick={() => stopRow(row)}>停止</Button> : null}
            {canRelay ? <Button aria-label="编辑账号" title="编辑账号" size="sm" onClick={() => openEditor(row)}>编辑</Button> : null}
            <Button aria-label={`查看 ${row.email || ''} 的日志`} title="日志" size="sm" onClick={() => openLog(row)}>日志</Button>
            <Button aria-label={`删除 ${row.email || ''}`} title={row.active_operation ? '请先停止正在运行的任务' : '软删除账号'} variant="danger" size="sm" disabled={Boolean(row.active_operation)} onClick={() => softDeleteAccounts([id])}>删除</Button>
          </div></td>
        </tr>;
      })}
    </tbody></Table>
    <Pager page={accountPage} pageSize={pageSize} total={accountTotal} onPageChange={setAccountPage} onPageSizeChange={(value) => { setPageSize(value); setAccountPage(1); }} sizes={PAGE_SIZES} />
  </Card> : null;

  return <div className="page-stack">
    {!embedded ? <div className="page-intro"><div><div className="eyebrow"><Smartphone size={14} /> {mode === 'phones' ? 'PHONE POOL' : 'GPT ACCOUNT WORKSPACE'}</div><h2>{mode === 'phones' ? '手机号池' : 'GPT账号'}</h2>{mode === 'phones' ? null : <p>注册、Codex 授权和手机接码统一显示在账号列表中。</p>}</div></div> : null}
    {error ? <ErrorState message={error} onRetry={load} /> : null}
    {showAccounts && manualOtpWaiting.length ? <div className="manual-otp-strip"><div><strong>{manualOtpWaiting.length} 个注册任务正在等待邮箱验证码</strong><small>验证码提交后，注册任务会自动继续。</small></div><div className="manual-otp-actions">{manualOtpWaiting.map((item) => <Button key={`${item.job_id || ''}:${item.email}`} size="sm" variant="primary" onClick={() => submitManualRegistrationOtp(item)}>{emailPreview(item.email)} · 提交验证码</Button>)}</div></div> : null}
    {phonePanel}
    {accountPanel}

    <Modal open={batchRegisterOpen} title="注册新 GPT 账号" wide onClose={() => setBatchRegisterOpen(false)} footer={<><Button onClick={() => setBatchRegisterOpen(false)}>取消</Button><Button icon={Settings2} onClick={() => { setBatchRegisterOpen(false); onOpenSettings?.('email:general'); }}>邮箱渠道设置</Button><Button icon={MailPlus} variant="primary" disabled={!registrationReady} loading={batchRegistering} onClick={registerNewAccounts}>开始注册</Button></>}>
      <div className="registration-channel-picker">
        <div className="registration-picker-heading"><div><strong>选择本次使用的邮箱渠道</strong><p>可以自由勾选；领取失败时按下方顺序尝试下一个渠道。</p></div><span>{selectedRegistrationChannels.length ? `已选 ${selectedRegistrationChannels.length} 个` : usingManualRegistration ? '手动邮箱' : '尚未选择'}</span></div>
        <div className="source-picker-options registration-source-options">{allRegistrationChannels.map((channel) => { const selected = registrationEmailSources.includes(channel.id) && channel.ready; const imported = Boolean(channel.requires_import); return <label key={channel.id} className={`source-option registration-source-option ${selected ? 'is-selected' : ''} ${channel.ready ? '' : 'is-disabled'}`} title={channel.message || (channel.ready ? '可以使用' : '尚未完成配置')}><input type="checkbox" checked={selected} disabled={!channel.ready} onChange={() => toggleRegistrationSource(channel.id)} /><span><strong>{channel.label || EMAIL_SOURCE_TEXT[channel.id] || channel.id}<em>{imported ? '导入邮箱' : '自动获取'}</em></strong><small>{channel.message || (channel.ready ? '配置完整，可以使用' : '请先到设置页完成配置')}</small></span><b>{channel.ready ? '可用' : '需配置'}</b></label>; })}</div>
        <div className="source-order registration-source-order"><div className="source-order-heading"><strong>本次优先顺序</strong><span>{selectedRegistrationChannels.length ? '数字越靠前越优先' : '勾选渠道后可调整'}</span></div>{selectedRegistrationChannels.length ? selectedRegistrationChannels.map((channel, index) => <div className="source-order-row" key={channel.id}><span className="source-order-number">{index + 1}</span><span>{channel.label || EMAIL_SOURCE_TEXT[channel.id] || channel.id}</span><span className="source-order-actions"><IconButton label="上移" icon={ArrowUp} size={14} disabled={index === 0} onClick={() => moveRegistrationSource(channel.id, -1)} /><IconButton label="下移" icon={ArrowDown} size={14} disabled={index === selectedRegistrationChannels.length - 1} onClick={() => moveRegistrationSource(channel.id, 1)} /></span></div>) : <span className="source-order-empty">未就绪的渠道会保持灰色；请先导入邮箱或到设置页补全配置。</span>}</div>
        {usingManualRegistration ? <div className="registration-manual-note"><strong>当前使用设置中的手动邮箱</strong><span>本次只能注册 1 个账号，收到验证码后需在 GPT账号页面提交。</span></div> : null}
      </div>
      <div className="registration-form-grid">
        <NumberField label="注册数量" value={usingManualRegistration ? 1 : registrationCount} min={1} max={registrationMaxCount} onChange={(value) => setRegistrationCount(Math.max(1, Math.min(registrationMaxCount, Number(value) || 1)))} />
        <NumberField label="并发数" value={workers} min={1} max={8} onChange={(value) => setWorkers(Math.max(1, Math.min(8, Number(value) || 1)))} />
      </div>
    </Modal>

    <Modal open={Boolean(importMode)} title={importMode === 'phones' ? '导入手机号' : '导入 ChatGPT 账号'} onClose={() => setImportMode(null)} wide footer={<><Button onClick={() => setImportMode(null)}>取消</Button><Button icon={Upload} variant="primary" onClick={submitImport}>导入</Button></>}>
      <label className="field-label">粘贴素材<textarea rows="12" value={importText} onChange={(event) => setImportText(event.target.value)} placeholder={importMode === 'phones' ? '手机号----取码 URL----可用次数' : '每行一个账号；邮箱、HTTP 接码地址、密码、2FA 顺序不限'} spellCheck="false" /></label>
      {importMode === 'phones' ? <p className="field-help">每行一个手机号，字段之间使用已配置的分隔符。</p> : <p className="field-help">按内容自动识别邮箱、HTTP 接码地址、2FA 和密码，无需固定字段顺序；<button type="button" className="field-help-link" onClick={() => { setImportMode(null); onOpenSettings?.('邮箱 / OTP'); }}>点击设置分隔符</button></p>}
    </Modal>

    <Modal open={Boolean(editAccount)} title={`编辑 ChatGPT 账号 · ${editAccount?.email || ''}`} onClose={() => setEditAccount(null)} wide footer={<><Button onClick={() => setEditAccount(null)}>取消</Button><Button variant="primary" icon={Check} onClick={saveEditor}>保存账号</Button></>}>
      <div className="edit-form"><div className="edit-grid">
        <label className="field-label">邮箱<input value={editDraft.email || ''} onChange={(event) => setEditDraft((current) => ({ ...current, email: event.target.value }))} autoComplete="off" /></label>
        <label className="field-label">ChatGPT 密码<input value={editDraft.chatgpt_password || ''} onChange={(event) => setEditDraft((current) => ({ ...current, chatgpt_password: event.target.value }))} autoComplete="off" /></label>
        <label className="field-label">邮箱取码 URL<input value={editDraft.email_code_url || ''} onChange={(event) => setEditDraft((current) => ({ ...current, email_code_url: event.target.value }))} autoComplete="off" /></label>
        <label className="field-label">TOTP 密钥<input value={editDraft.totp_secret || ''} onChange={(event) => setEditDraft((current) => ({ ...current, totp_secret: event.target.value }))} autoComplete="off" /></label>
        <label className="field-label">候选手机号<input value={editDraft.phone || ''} onChange={(event) => setEditDraft((current) => ({ ...current, phone: event.target.value }))} autoComplete="off" /></label>
        <label className="field-label">短信取码 URL<input value={editDraft.sms_code_url || ''} onChange={(event) => setEditDraft((current) => ({ ...current, sms_code_url: event.target.value }))} autoComplete="off" /></label>
      </div><label className="field-label">备注<textarea rows="3" value={editDraft.note || ''} onChange={(event) => setEditDraft((current) => ({ ...current, note: event.target.value }))} /></label></div>
    </Modal>

    <Modal open={sub2Open} title="sub2api 服务与双向同步" onClose={() => setSub2Open(false)} wide footer={<><Button onClick={() => setSub2Open(false)}>关闭</Button><Button onClick={deleteSub2} variant="danger" disabled={!sub2ServiceId}>删除服务</Button><Button onClick={saveSub2}>保存服务</Button><Button icon={Download} disabled={!sub2ServiceId || !accountIds.length} onClick={() => runAction('/api/codex-relay/accounts/import-sub2', { account_ids: accountIds.map((id) => accounts.find((row) => idOf(row) === id)?.relay_account_id).filter(Boolean), service_id: sub2ServiceId, delete_terminal: false }, '已同步到 sub2api')}>同步到 sub2</Button><Button icon={RefreshCw} disabled={!sub2ServiceId} variant="primary" onClick={() => runAction('/api/codex-relay/accounts/sync-from-sub2', { service_id: sub2ServiceId }, '已从 sub2api 同步')}>从 sub2 同步</Button></>}>
      <div className="sub2-picker"><Select label="已保存服务" value={sub2ServiceId} onChange={(value) => { setSub2ServiceId(value); const service = sub2Services.find((item) => item.id === value); if (service) setSub2Draft({ name: service.name || '', homepage: service.homepage || '', api_base: service.api_base || '', admin_key: service.admin_key || '' }); }} options={[{ value: '', label: '新建服务' }, ...sub2Services.map((service) => ({ value: service.id, label: `${service.name} · ${service.api_base}` }))]} /><Button icon={Plus} onClick={() => { setSub2ServiceId(''); setSub2Draft({ name: '', homepage: '', api_base: '', admin_key: '' }); }}>新建</Button></div>
      <div className="edit-grid">{[['name', '服务名称'], ['homepage', '官网'], ['api_base', 'API 地址'], ['admin_key', '管理员 Key']].map(([key, label]) => <label className="field-label" key={key}>{label}<input type={key === 'admin_key' ? 'password' : 'text'} value={sub2Draft[key] || ''} onChange={(event) => setSub2Draft((current) => ({ ...current, [key]: event.target.value }))} autoComplete="off" /></label>)}</div>
    </Modal>

    <Modal open={Boolean(logRow)} title={`账号日志 · ${logRow?.email || ''}`} onClose={() => setLogRow(null)} wide footer={<Button onClick={() => setLogRow(null)}>关闭</Button>}><pre className="log-viewer">{logText}</pre></Modal>
  </div>;
}
