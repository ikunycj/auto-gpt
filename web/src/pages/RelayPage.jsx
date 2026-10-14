import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, ArrowDown, ArrowUp, Check, Clipboard, Database, Download, MailPlus, Minus, Pencil, Plus,
  RefreshCw, Settings2, ShieldCheck, Smartphone, Trash2, Upload,
} from 'lucide-react';
import { copyText, del, download, get, patch, post, put } from '../api';
import { useDebounced, usePolling } from '../hooks';
import ProxyTest from '../components/ProxyTest';
import {
  Button, Card, Checkbox, EmptyState, ErrorState, IconButton, Modal, NumberField, Pager,
  RefreshButton, SearchField, SectionHeader, Select, StatusPill, Table, Toolbar,
} from '../components/ui';

const PAGE_SIZES = [20, 50, 100];
const ACTIVE_JOB_STATUSES = new Set(['pending', 'running', 'stopping', 'waiting_email', 'waiting_sms', 'waiting_totp', 'waiting_browser']);
const EMAIL_PREVIEW_MAX_LENGTH = 20;

const STATUS_TEXT = {
  registration: { registered: '已注册', unregistered: '未注册', registering: '注册中', failed: '注册失败' },
  codex: { authorized: '已授权', unauthorized: '未授权', authorizing: '授权中', failed: '授权失败', deactivated: '已删除/停用' },
  phone: { verified: '已接码', unverified: '未接码', verifying: '接码中', failed: '接码失败' },
};
const GENERIC_STATUS_TEXT = { alive: '存活', dead: '失效', unknown: '未验活', available: '可用', error: '错误', platform: '自动取号', ready: '已启用', unavailable: '未就绪' };
const EMAIL_SOURCE_TEXT = {
  outlook: 'Outlook', generic_api: '通用接码 API', cloudflare_domain: 'Cloudflare 域名',
  cloudflare: 'Cloudflare Worker', gptmail: 'GPTMail', mailnest: 'MailNest', cloudmail: 'CloudMail',
};
const IMPORTED_EMAIL_SOURCES = new Set(['outlook', 'generic_api']);

function idOf(row) { return String(row?.id || ''); }
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
  if (row.expired) return 'expired';
  if (row.reserved) return 'reserved';
  if (row.assigned) return 'bound';
  if (!row.candidate || Number(row.available_uses ?? 0) <= 0) return 'used';
  return 'available';
}
function phoneExpirationText(row) {
  if (!row?.expires_at) return '不过期';
  const date = new Date(row.expires_at);
  return Number.isNaN(date.getTime()) ? '过期时间异常' : date.toLocaleString('zh-CN', { hour12: false });
}
function activeJob(row) {
  const job = row?.codex_job;
  return Boolean(job && ACTIVE_JOB_STATUSES.has(String(job.status || '').toLowerCase()));
}
function activeMaintenance(row) {
  const job = row?.maintenance_job;
  return Boolean(job && ACTIVE_JOB_STATUSES.has(String(job.status || '').toLowerCase()));
}

export default function RelayPage({ notify, summary, mode = 'all', embedded = false, onOpenSettings, onOpenEmail, onSummaryRefresh, onRegistrationRequestHandled, registrationRequested = false }) {
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
  const [phoneUsesSaving, setPhoneUsesSaving] = useState({});
  const phoneUsesSavingIds = useRef(new Set());
  const [phoneSettings, setPhoneSettings] = useState(null);
  const [phoneSettingsDraft, setPhoneSettingsDraft] = useState({ available_uses: '', expiration: 'keep', days: '7' });
  const [phoneSettingsSaving, setPhoneSettingsSaving] = useState(false);
  const [phoneSettingsError, setPhoneSettingsError] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [manualOtpWaiting, setManualOtpWaiting] = useState([]);
  const [importMode, setImportMode] = useState(null);
  const [batchRegisterOpen, setBatchRegisterOpen] = useState(false);
  const [registrationCount, setRegistrationCount] = useState(1);
  const [registrationEmailSources, setRegistrationEmailSources] = useState([]);
  const [batchRegistering, setBatchRegistering] = useState(false);
  const [importText, setImportText] = useState('');
  const [importProxy, setImportProxy] = useState('');
  const [proxyRow, setProxyRow] = useState(null);
  const [editProxyMode, setEditProxyMode] = useState('keep');
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
        showPhones ? get('/api/phones', {
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
  const importedChannels = allRegistrationChannels.filter((channel) => IMPORTED_EMAIL_SOURCES.has(channel.id));
  const emailPoolChannel = {
    id: 'pool',
    label: '邮箱池',
    kind: 'pool',
    requires_import: true,
    ready: importedChannels.some((channel) => channel.ready),
    pool_sources: importedChannels.filter((channel) => channel.ready).map((channel) => channel.id),
    message: importedChannels.some((channel) => channel.ready)
      ? `已导入 ${importedChannels.reduce((total, channel) => total + Number(channel.available || 0), 0)} 个可用邮箱`
      : '请先导入 Outlook 或接码 API 邮箱',
  };
  const registrationPickerChannels = [
    emailPoolChannel,
    ...allRegistrationChannels.filter((channel) => !IMPORTED_EMAIL_SOURCES.has(channel.id)),
  ];
  const readyRegistrationSources = new Set(registrationPickerChannels.filter((channel) => channel.ready).map((channel) => channel.id));
  const selectedRegistrationChannels = registrationEmailSources
    .filter((source) => readyRegistrationSources.has(source))
    .map((source) => registrationPickerChannels.find((channel) => channel.id === source))
    .filter(Boolean);
  const selectedRegistrationSourceIds = selectedRegistrationChannels.flatMap((channel) => (
    channel.id === 'pool' ? (channel.pool_sources || []) : [channel.id]
  ));
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
    const pickerDefaults = defaults.some((source) => IMPORTED_EMAIL_SOURCES.has(source))
      ? ['pool', ...defaults.filter((source) => !IMPORTED_EMAIL_SOURCES.has(source))]
      : defaults;
    setRegistrationEmailSources(pickerDefaults);
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

  async function savePhoneAvailableUses(row, value) {
    const id = idOf(row);
    const availableUses = Number(value);
    if (!id || row.special || phoneUsesSavingIds.current.has(id) || Number(row.available_uses) === availableUses) return;
    if (!Number.isInteger(availableUses) || availableUses < 0 || availableUses > 3) return;
    phoneUsesSavingIds.current.add(id);
    setPhoneUsesSaving((current) => ({ ...current, [id]: availableUses }));
    try {
      const result = await patch('/api/phones/settings', { phone_ids: [id], available_uses: availableUses });
      setPhones((current) => current.map((phone) => idOf(phone) === id ? { ...phone, available_uses: availableUses } : phone));
      notify(result.updated ? `可用次数已设为 ${availableUses} 次` : '可用次数未发生变化', 'success');
      await load();
    } catch (actionError) {
      notify(actionError.message || '保存可用次数失败', 'error');
    } finally {
      phoneUsesSavingIds.current.delete(id);
      setPhoneUsesSaving((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
    }
  }

  function openPhoneSettings(row = null) {
    if (row?.special) return;
    const ids = row ? [String(row.id)] : [...phoneIds];
    if (!ids.length) return;
    // Capture IDs now: background polling/filter changes must not retarget a save.
    setPhoneSettings({ ids, row, mode: row ? 'expiration' : 'bulk' });
    setPhoneSettingsDraft({ available_uses: '', expiration: 'keep', days: '7' });
    setPhoneSettingsError('');
  }

  async function savePhoneSettings() {
    if (!phoneSettings || phoneSettingsSaving) return;
    const payload = { phone_ids: phoneSettings.ids };
    if (phoneSettings.mode === 'bulk' && phoneSettingsDraft.available_uses !== '') payload.available_uses = Number(phoneSettingsDraft.available_uses);
    if (phoneSettingsDraft.expiration === 'never') payload.expiration_days = 0;
    if (phoneSettingsDraft.expiration === 'days') {
      const days = Number(phoneSettingsDraft.days);
      if (!Number.isSafeInteger(days) || days < 1) {
        setPhoneSettingsError('过期天数必须是大于 0 的整数；不限制时间请选择“不过期”。');
        return;
      }
      payload.expiration_days = days;
    }
    if (Object.keys(payload).length === 1) return;
    setPhoneSettingsSaving(true);
    setPhoneSettingsError('');
    try {
      const result = await patch('/api/phones/settings', payload);
      notify(result.updated ? `已更新 ${result.updated} 个手机号的${phoneSettings.mode === 'expiration' ? '期限' : '设置'}` : '设置未发生变化', 'success');
      setPhoneSettings(null);
      await load();
    } catch (actionError) {
      setPhoneSettingsError(actionError.message || '保存手机号设置失败');
    } finally {
      setPhoneSettingsSaving(false);
    }
  }

  async function submitImport() {
    if (!importText.trim()) return notify('请粘贴导入内容', 'warning');
    try {
      const response = importMode === 'phones'
        ? await post('/api/phones/import', { text: importText })
        : await post('/api/gpt-accounts/import', { text: importText, format: 'auto', ...(importProxy.trim() ? { proxy: importProxy.trim() } : {}) });
      notify(response.message || '导入完成', 'success');
      setImportMode(null);
      setImportText('');
      setImportProxy('');
      await load();
    } catch (actionError) {
      notify(actionError.message || '导入失败', 'error');
    }
  }

  async function registerNewAccounts() {
    if (!registrationReady) return notify('请至少选择一个可用邮箱渠道', 'warning');
    const count = Math.max(1, Math.min(registrationMaxCount, Number(registrationCount) || 1));
    setBatchRegistering(true);
    try {
      const payload = { count, workers };
      if (selectedRegistrationSourceIds.length) payload.email_sources = selectedRegistrationSourceIds;
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
    const selectedIds = ids.filter((id) => accounts.some((row) => idOf(row) === id && row.email && row.registration_status === 'registered' && !row.active_operation));
    const excluded = ids.length - selectedIds.length;
    if (!selectedIds.length) return notify('所选账号均未提交：请先完成 GPT 注册并停止正在运行的任务', 'warning');
    try {
      const response = await post('/api/gpt-accounts/authorize', { account_ids: selectedIds, workers, phone_ids: [] });
      const submitted = Number(response.submitted || 0);
      const skipped = Array.isArray(response.skipped) ? response.skipped : [];
      const excludedText = excluded ? `，未提交 ${excluded} 个（未注册或任务运行中）` : '';
      const skippedText = skipped.length ? `，后端跳过 ${skipped.length} 个${skipped[0]?.reason ? `（${skipped[0].reason}）` : ''}` : '';
      notify(submitted ? `已启动 ${submitted} 个 Codex 授权任务${excludedText}${skippedText}；仅在需要短信验证时分配号码` : `没有启动授权任务${excludedText}${skippedText}`, submitted && !excluded && !skipped.length ? 'success' : 'warning');
      await load();
    } catch (actionError) {
      notify(actionError.message || '启动 Codex 授权失败', 'error');
    }
  }

  async function deleteAccounts(ids = accountIds) {
    const selectedIds = [...new Set(ids.map(String).filter(Boolean))];
    if (!selectedIds.length) return notify('请先选择 GPT 账号', 'warning');
    const label = selectedIds.length === 1 ? '这个 GPT 账号' : `选中的 ${selectedIds.length} 个 GPT 账号`;
    if (!window.confirm(`确定彻底删除${label}吗？此操作不可恢复。`)) return;
    try {
      const response = await del('/api/gpt-accounts', { account_ids: selectedIds });
      const deletedCount = Number(response.deleted_count ?? response.deleted ?? 0);
      if (!deletedCount) return notify('没有 GPT 账号被删除', 'warning');
      setAccountSelected((current) => {
        const next = new Set(current);
        selectedIds.forEach((id) => next.delete(id));
        return next;
      });
      notify(response.message || `已删除 ${deletedCount} 个 GPT 账号`, 'success');
      if (deletedCount >= accounts.length && accountPage > 1) setAccountPage(accountPage - 1);
      else await load();
    } catch (actionError) {
      notify(actionError.message || '删除 GPT 账号失败', 'error');
    }
  }

  async function maintenance(actionName) {
    if (!accountIds.length) return notify('请先选择 GPT 账号', 'warning');
    await runAction('/api/gpt-accounts/actions', { account_ids: accountIds, action: actionName, workers }, '账号任务已入队');
  }

  async function exportCredentials(format, copy = false) {
    if (!accountIds.length) return notify('请先选择 GPT 账号', 'warning');
    try {
      if (copy) {
        const response = await post('/api/gpt-accounts/export/copy', { account_ids: accountIds, format });
        await copyText(response.content || '');
        notify(`已复制 ${response.count || 0} 条凭证`, 'success');
      } else {
        await download('/api/gpt-accounts/export/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ account_ids: accountIds, format }) });
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
    setEditProxyMode('keep');
    setEditAccount(row);
    setEditDraft({
      email: row.email || '',
      chatgpt_password: row.chatgpt_password || row.password || '',
      email_code_url: row.email_code_url || '',
      totp_secret: row.totp_secret || '',
      proxy: '',
      // These inputs add a new phone to the shared pool; they are never a
      // projection of the account's verified or unverified assigned phone.
      phone: '',
      sms_code_url: '',
      note: row.note || '',
    });
  }

  async function saveEditor() {
    if (!editAccount?.id) return notify('账号缺少 ID，无法保存', 'error');
    const payload = { ...editDraft };
    if (editProxyMode === 'keep') delete payload.proxy;
    else if (editProxyMode === 'direct') payload.proxy = '';
    else if (!String(payload.proxy || '').trim()) return notify('请填写新的代理地址', 'warning');
    // A registered email identifies the OpenAI account and must remain stable.
    if (editAccount.registration_status === 'registered') delete payload.email;
    const poolPhone = String(editDraft.phone || '').trim();
    const poolSmsUrl = String(editDraft.sms_code_url || '').trim();
    if (Boolean(poolPhone) !== Boolean(poolSmsUrl)) return notify('加入手机池时，请同时填写手机号和短信取码 URL', 'error');
    if (poolPhone) {
      payload.phone = poolPhone;
      payload.sms_code_url = poolSmsUrl;
    } else {
      // Empty pool inputs do not modify an assigned phone or remove a pool entry.
      delete payload.phone;
      delete payload.sms_code_url;
    }
    try {
      await put(`/api/gpt-accounts/${encodeURIComponent(editAccount.id)}`, payload);
      notify(poolPhone ? '账号已保存，手机号已加入接码池' : '账号已保存', 'success');
      setEditAccount(null);
      await load();
    } catch (actionError) {
      notify(actionError.message || '保存失败', 'error');
    }
  }

  async function submitVerification(job, stage) {
    const code = window.prompt(`${stage === 'sms' ? '短信' : stage === 'totp' ? '2FA' : '邮箱'}验证码`);
    if (!code?.trim()) return;
    await runAction(`/api/codex-jobs/${encodeURIComponent(job.id)}/verification`, { stage, code: code.trim() }, '验证码已提交');
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
      await runAction(`/api/codex-jobs/${encodeURIComponent(row.maintenance_job.id)}/stop`, {}, '已发送账号维护停止请求');
    } else if (row.codex_job_kind === 'codex_retry') {
      await runAction(`/api/jobs/${encodeURIComponent(row.codex_job_id)}/stop`, {}, '已发送授权停止请求');
    } else if (row.codex_job_id) {
      await runAction(`/api/codex-jobs/${encodeURIComponent(row.codex_job_id)}/stop`, {}, '已发送授权停止请求');
    }
  }

  async function browserFocus(job) {
    try {
      const response = await post(`/api/codex-jobs/${encodeURIComponent(job.id)}/browser-focus`, {});
      if (response.url && /^https?:\/\//i.test(response.url)) window.open(response.url, '_blank', 'noopener,noreferrer');
      notify('已打开人工处理页面', 'success');
    } catch (actionError) {
      notify(actionError.message || '无法打开页面', 'error');
    }
  }

  async function loadSub2() {
    try {
      const response = await get('/api/sub2-services');
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
      const response = await post('/api/sub2-services', { ...sub2Draft, ...(sub2ServiceId ? { id: sub2ServiceId } : {}) });
      notify('sub2api 服务已保存', 'success');
      setSub2ServiceId(response.service?.id || sub2ServiceId);
      await loadSub2();
    } catch (actionError) { notify(actionError.message || '保存 sub2 服务失败', 'error'); }
  }

  async function deleteSub2() {
    if (!sub2ServiceId || !window.confirm('确定删除这个 sub2api 服务配置吗？')) return;
    try {
      await del(`/api/sub2-services/${encodeURIComponent(sub2ServiceId)}`);
      notify('sub2api 服务已删除', 'success');
      setSub2ServiceId('');
      setSub2Draft({ name: '', homepage: '', api_base: '', admin_key: '' });
      await loadSub2();
    } catch (actionError) { notify(actionError.message || '删除失败', 'error'); }
  }

  const phonePanel = showPhones ? <Card className="table-card relay-panel">
    <SectionHeader title="手机号接码池" description={`${phoneTotal} 个号码或动态来源，直接修改可用次数，点击期限维护过期时间。`} actions={<>
      <Button icon={Upload} size="sm" onClick={() => setImportMode('phones')}>导入手机号</Button>
      <Button icon={Minus} size="sm" disabled={!phoneIds.length} onClick={() => runAction('/api/phones/available-uses', { phone_ids: phoneIds, delta: -1 }, '已减少可用次数', 'patch')}>-1</Button>
      <Button icon={Plus} size="sm" disabled={!phoneIds.length} onClick={() => runAction('/api/phones/available-uses', { phone_ids: phoneIds, delta: 1 }, '已增加可用次数', 'patch')}>+1</Button>
      <Button icon={Settings2} size="sm" disabled={!phoneIds.length} onClick={() => openPhoneSettings()}>批量设置</Button>
      <Button icon={Trash2} variant="danger" size="sm" disabled={!phoneIds.length} onClick={() => runAction('/api/phones', { phone_ids: phoneIds }, '已删除未绑定号码', 'delete')}>删除</Button>
    </>} />
    <Toolbar>
      <SearchField value={phoneQuery} onChange={setPhoneQuery} placeholder="搜索尾号或绑定邮箱…" />
      <Select value={phoneStatus} onChange={setPhoneStatus} options={[{ value: '', label: '全部状态' }, { value: 'available', label: '可用' }, { value: 'bound', label: '已绑定' }, { value: 'reserved', label: '已预留' }, { value: 'used', label: '已用尽' }, { value: 'expired', label: '已过期' }, { value: 'invalid', label: '已失效' }]} />
      <span className="selection-note">已选 {phoneIds.length}</span>
      <RefreshButton onClick={refresh} loading={running} />
    </Toolbar>
    <Table className="phone-pool-table">
      <thead><tr>
        <th className="check-col"><Checkbox checked={phones.filter((row) => !row.special).length > 0 && phones.filter((row) => !row.special).every((row) => phoneSelected.has(String(row.id || '')))} onChange={(checked) => setPhoneSelected(checked ? new Set(phones.filter((row) => !row.special).map((row) => String(row.id || ''))) : new Set())} /></th>
        <th>手机号 / 来源</th><th>状态</th><th>可用次数</th><th className="phone-expiration-col">期限</th><th>绑定账号</th>
      </tr></thead>
      <tbody>
        {loading ? <tr><td colSpan="6"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : phones.length === 0 ? <tr><td colSpan="6"><EmptyState title="没有手机号" description="导入后可直接修改可用次数，在「期限」列维护过期时间；默认不过期。" /></td></tr> : phones.map((row) => {
          const id = String(row.id || '');
          const state = phonePoolState(row);
          const special = Boolean(row.special);
          const savingUses = phoneUsesSaving[id] !== undefined;
          return <tr key={id} className={special ? 'phone-pool-special-row' : ''}>
            <td>{special ? <span className="phone-special-mark">平台</span> : <Checkbox checked={phoneSelected.has(id)} onChange={(checked) => toggle(setPhoneSelected, id, checked)} />}</td>
            <td className={special ? 'phone-special-source' : 'mono'}><strong>{special ? pretty(row.label || row.provider_label || '平台自动取号') : pretty(row.phone)}</strong>{special ? <small className="table-sub">{row.provider_label || row.provider || '接码平台'} · 动态号码</small> : null}</td>
            <td><StatusPill value={state}>{state === 'used' ? '已用尽' : undefined}</StatusPill></td>
            <td>{special ? <strong>动态</strong> : <>
              <select className="phone-uses-select" aria-label={`手机号 ${row.phone} 可用次数`} aria-busy={savingUses} disabled={savingUses} value={phoneUsesSaving[id] ?? row.available_uses} onChange={(event) => savePhoneAvailableUses(row, event.target.value)}>
                {[0, 1, 2, 3].map((value) => <option key={value} value={value}>{value} 次</option>)}
              </select>
              {savingUses ? <small className="table-sub" role="status">保存中…</small> : null}
            </>}{row.reserved_count ? <small className="table-sub">占用 {row.reserved_count} 个任务</small> : null}</td>
            <td className="phone-expiration-cell">{special ? '平台管理' : <>
              <button type="button" className="phone-expiration-button" aria-label={`修改手机号 ${row.phone} 期限`} title="点击修改期限" onClick={() => openPhoneSettings(row)}>
                <span>{phoneExpirationText(row)}</span><Pencil size={13} aria-hidden="true" />
              </button>
              {row.expires_at ? <small className="table-sub">{row.expired ? '已过期 · 不再分配' : `剩余 ${row.expires_in_days} 天（向上取整）`}</small> : null}
            </>}</td>
            <td>{special ? pretty(row.message || '授权时自动取号') : pretty(row.assigned_account_email)}</td>
          </tr>;
        })}
      </tbody>
    </Table>
    <Pager page={phonePage} pageSize={pageSize} total={phoneTotal} onPageChange={setPhonePage} onPageSizeChange={(value) => { setPageSize(value); setPhonePage(1); }} sizes={PAGE_SIZES} />
  </Card> : null;

  const accountPanel = showAccounts ? <Card className="table-card relay-panel gpt-account-panel">
    <SectionHeader title="GPT账号" actions={<>
      <Button icon={Upload} size="sm" onClick={() => setImportMode('accounts')}>导入账号</Button>
      <Button icon={MailPlus} variant="primary" size="sm" onClick={() => setBatchRegisterOpen(true)}>注册新账号</Button>
      <Button icon={RefreshCw} size="sm" disabled={!accountIds.length} onClick={() => authorizeSelected()}>授权选中</Button>
      <Button icon={ShieldCheck} size="sm" disabled={!accountIds.length} onClick={() => maintenance('enable_2fa')}>开启 2FA</Button>
      <Button icon={Activity} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_gpt_liveness')}>GPT 验活</Button>
      <Button icon={Activity} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_email_liveness')}>邮箱验活</Button>
      <Button icon={RefreshCw} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_quota')}>查限额</Button>
      <Button icon={Database} size="sm" disabled={!accountIds.length} onClick={() => maintenance('check_sub2_status')}>sub2 状态</Button>
      <Button icon={Clipboard} size="sm" disabled={!accountIds.length} onClick={() => exportCredentials('rt', true)}>复制凭证</Button>
      <Button icon={Download} size="sm" disabled={!accountIds.length} onClick={() => exportCredentials('rt')}>下载凭证</Button>
      <Button icon={Settings2} size="sm" onClick={() => { setSub2Open(true); loadSub2(); }}>sub2 服务</Button>
      <Button icon={Trash2} variant="danger" size="sm" disabled={!accountIds.length} onClick={() => deleteAccounts()}>删除</Button>
    </>} />
    <p className="gpt-account-auth-hint">账号已记录代理时优先使用该代理；未记录时使用本机网络，不会抽取全局代理池或沿用 Roxy 旧代理。注册仍可使用<button type="button" className="field-help-link" onClick={() => onOpenSettings?.('代理池')}>设置中的代理池</button>，非本机代理会自动记录。手机号仅在需要短信验证时分配。</p>
    <Toolbar className="gpt-account-toolbar">
      <SearchField value={accountQuery} onChange={setAccountQuery} placeholder="搜索账号或备注…" />
      <Select value={providerFilter} onChange={setProviderFilter} options={[{ value: '', label: '邮箱来源' }, ...Object.entries(EMAIL_SOURCE_TEXT).map(([value, label]) => ({ value, label }))]} />
      <Select value={codexFilter} onChange={setCodexFilter} options={[{ value: '', label: 'Codex授权状态' }, { value: 'authorized', label: '已授权' }, { value: 'unauthorized', label: '未授权' }, { value: 'authorizing', label: '授权中' }, { value: 'failed', label: '授权失败' }, { value: 'deactivated', label: '已删除/停用' }]} />
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
      <col className="col-proxy" /><col className="col-time" /><col className="col-actions" />
    </colgroup><thead><tr>
      <th className="check-col"><Checkbox checked={selectableAccounts.length > 0 && selectableAccounts.every((row) => accountSelected.has(idOf(row)))} onChange={(checked) => setAccountSelected(checked ? new Set(selectableAccounts.map(idOf)) : new Set())} /></th>
      <th>账号</th><th>密码</th><th>2FA</th><th>邮箱接码API</th><th>GPT注册状态</th><th>Codex授权状态</th><th>手机接码</th><th>手机</th><th>GPT状态</th><th>套餐</th><th>备注</th><th>代理 / 网络</th><th>时间</th><th className="actions-col">操作</th>
    </tr></thead><tbody>
      {loading ? <tr><td colSpan="15"><div className="table-loading"><span className="loading-bar" /><span className="loading-bar" /><span className="loading-bar" /></div></td></tr> : accounts.length === 0 ? <tr><td colSpan="15"><EmptyState title="没有 GPT 账号" description="导入已有账号，或在设置中配置邮箱来源后启动注册任务。" /></td></tr> : accounts.map((row) => {
        const id = idOf(row);
        const codexJob = row.codex_job;
        const codexJobDetail = ['failed', 'waiting_email', 'waiting_sms', 'waiting_totp', 'waiting_browser'].includes(codexJob?.status)
          ? String(codexJob.error || codexJob.message || '').trim() : '';
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
          <td>{status('codex', row.codex_status)}{codexJobDetail ? <small className={`table-sub codex-job-detail ${codexJob?.status === 'failed' ? 'is-error' : ''}`} title={codexJobDetail}>{codexJobDetail}</small> : null}</td>
          <td>{status('phone', row.phone_status)}</td>
          <td className="mono cell-nowrap"><CopyValue value={row.phone} label="手机" notify={notify} empty="" /></td>
          <td>{status('', row.gpt_status || 'unknown')}</td>
          <td className="cell-nowrap">{pretty(row.plan)}</td>
          <td className="note-cell" title={row.note || ''}>{pretty(row.note)}</td>
          <td className="proxy-cell" title={row.proxy_display || '本机网络'}><small>{row.proxy_display || (row.has_proxy ? '已绑定代理' : '本机网络')}</small><Button type="button" size="sm" onClick={() => setProxyRow(row)}>测试代理</Button></td>
          <td className="time-cell"><small className="time-line">创建时间：{pretty(row.created_at)}</small><small className="time-line">修改时间：{pretty(row.updated_at)}</small></td>
          <td><div className="row-actions">
            {row.registration_status === 'registered' && !['authorized', 'deactivated'].includes(row.codex_status) && !activeJob(row) && !activeMaintenance(row) ? <Button aria-label="授权" title="授权" size="sm" onClick={() => authorizeSelected([id])}>授权</Button> : null}
            {manualOtp ? <Button aria-label="提交邮箱验证码" title="提交邮箱验证码" size="sm" variant="primary" onClick={() => submitManualRegistrationOtp(manualOtp)}>邮箱验证码</Button> : null}
            {waitingCode ? <Button aria-label="提交验证码" title="提交验证码" size="sm" onClick={() => submitVerification(codexJob, stage)}>验证码</Button> : null}
            {codexJob?.status === 'waiting_browser' ? <><Button aria-label="打开处理页" title="打开处理页" size="sm" onClick={() => browserFocus(codexJob)}>处理页</Button><Button aria-label="继续任务" title="继续任务" size="sm" onClick={() => runAction(`/api/codex-jobs/${encodeURIComponent(codexJob.id)}/browser-assist`, {}, '已通知继续')}>继续</Button></> : null}
            {(row.registration_status === 'registering' || activeJob(row) || activeMaintenance(row)) ? <Button aria-label="停止任务" title="停止任务" size="sm" onClick={() => stopRow(row)}>停止</Button> : null}
            <Button aria-label="编辑账号" title="编辑账号" size="sm" onClick={() => openEditor(row)}>编辑</Button>
            <Button aria-label={`查看 ${row.email || ''} 的日志`} title="日志" size="sm" onClick={() => openLog(row)}>日志</Button>
            <Button aria-label={`删除 ${row.email || ''}`} title={row.active_operation ? '请先停止正在运行的任务' : '删除账号'} variant="danger" size="sm" disabled={Boolean(row.active_operation)} onClick={() => deleteAccounts([id])}>删除</Button>
          </div></td>
        </tr>;
      })}
    </tbody></Table>
    <Pager page={accountPage} pageSize={pageSize} total={accountTotal} onPageChange={setAccountPage} onPageSizeChange={(value) => { setPageSize(value); setAccountPage(1); }} sizes={PAGE_SIZES} />
  </Card> : null;

  return <div className="page-stack">
    {!embedded ? <div className="page-intro"><div><div className="eyebrow"><Smartphone size={14} /> {mode === 'phones' ? '号码资源' : '账号资源'}</div><h2>{mode === 'phones' ? '手机号池' : 'GPT账号'}</h2>{mode === 'phones' ? null : <p>已注册账号的资料、Codex 授权和手机验证统一保存在账号列表中。</p>}</div></div> : null}
    {error ? <ErrorState message={error} onRetry={load} /> : null}
    {showAccounts && manualOtpWaiting.length ? <div className="manual-otp-strip"><div><strong>{manualOtpWaiting.length} 个注册任务正在等待邮箱验证码</strong><small>验证码提交后，注册任务会自动继续。</small></div><div className="manual-otp-actions">{manualOtpWaiting.map((item) => <Button key={`${item.job_id || ''}:${item.email}`} size="sm" variant="primary" onClick={() => submitManualRegistrationOtp(item)}>{emailPreview(item.email)} · 提交验证码</Button>)}</div></div> : null}
    {phonePanel}
    {accountPanel}

    <Modal open={Boolean(phoneSettings)} title={phoneSettings?.mode === 'expiration' ? `手机号期限 · ${phoneSettings.row.phone}` : `批量设置 · ${phoneSettings?.ids.length || 0} 个手机号`} onClose={() => { if (!phoneSettingsSaving) setPhoneSettings(null); }} footer={<>
      <Button disabled={phoneSettingsSaving} onClick={() => setPhoneSettings(null)}>取消</Button>
      <Button variant="primary" icon={Check} loading={phoneSettingsSaving} disabled={(phoneSettings?.mode === 'expiration' || phoneSettingsDraft.available_uses === '') && phoneSettingsDraft.expiration === 'keep'} onClick={savePhoneSettings}>{phoneSettings?.mode === 'expiration' ? '保存期限' : '保存设置'}</Button>
    </>}>
      {phoneSettingsError ? <ErrorState message={phoneSettingsError} /> : null}
      {phoneSettings?.row ? <p className="field-help">当前期限：{phoneSettings.row.expired ? '已过期 · ' : ''}{phoneExpirationText(phoneSettings.row)}</p> : null}
      <fieldset className="phone-settings-fields" disabled={phoneSettingsSaving}>
        {phoneSettings?.mode === 'bulk' ? <Select label="可用次数" value={phoneSettingsDraft.available_uses} onChange={(value) => setPhoneSettingsDraft((current) => ({ ...current, available_uses: value }))} options={[{ value: '', label: '不修改次数' }, ...[0, 1, 2, 3].map((value) => ({ value: String(value), label: `${value} 次` }))]} /> : null}
        <Select label="期限" value={phoneSettingsDraft.expiration} onChange={(value) => setPhoneSettingsDraft((current) => ({ ...current, expiration: value }))} options={[{ value: 'keep', label: '不修改过期时间' }, { value: 'never', label: '不过期' }, { value: 'days', label: '按天设置' }]} />
        {phoneSettingsDraft.expiration === 'days' ? <NumberField label="过期天数" min={1} value={phoneSettingsDraft.days} onChange={(value) => setPhoneSettingsDraft((current) => ({ ...current, days: value }))} /> : null}
      </fieldset>
      <p className="field-help">{phoneSettings?.mode === 'bulk' ? '次数为精确设置，不是增减。' : '这里只修改期限，不影响可用次数。'}默认不过期；按天设置从本次保存时开始计算，每天为 24 小时，到期后停止分配。不修改过期时间不会延长或清除原期限。</p>
      <p className="field-help">任务占用中的号码不能减少次数或修改有效期；增加次数不会解除已过期或已失效状态。</p>
    </Modal>

    <Modal open={batchRegisterOpen} title="注册新 GPT 账号" wide onClose={() => setBatchRegisterOpen(false)} footer={<><Button onClick={() => setBatchRegisterOpen(false)}>取消</Button><Button icon={Settings2} onClick={() => { setBatchRegisterOpen(false); onOpenSettings?.('email:general'); }}>邮箱渠道设置</Button><Button icon={MailPlus} variant="primary" disabled={!registrationReady} loading={batchRegistering} onClick={registerNewAccounts}>开始注册</Button></>}>
      <div className="registration-channel-picker">
        <div className="registration-picker-heading"><div><strong>选择本次使用的邮箱渠道</strong><p>可以自由勾选；领取失败时按下方顺序尝试下一个渠道。</p></div><span>{selectedRegistrationChannels.length ? `已选 ${selectedRegistrationChannels.length} 个` : usingManualRegistration ? '手动邮箱' : '尚未选择'}</span></div>
        <div className="source-picker-options registration-source-options">{registrationPickerChannels.map((channel) => { const selected = registrationEmailSources.includes(channel.id) && channel.ready; const imported = Boolean(channel.requires_import); const settingsGroup = channel.id === 'pool' ? 'email:general' : (channel.id === 'cloudflare_domain' ? 'email:domain' : 'email:temporary'); const configure = channel.id === 'pool' ? onOpenEmail : () => onOpenSettings?.(settingsGroup); return <div className="registration-source-row" key={channel.id}><label className={`source-option registration-source-option ${selected ? 'is-selected' : ''} ${channel.ready ? '' : 'is-disabled'}`} title={channel.message || (channel.ready ? '可以使用' : '尚未完成配置')}><input type="checkbox" checked={selected} disabled={!channel.ready} onChange={() => toggleRegistrationSource(channel.id)} /><span><strong>{channel.label || EMAIL_SOURCE_TEXT[channel.id] || channel.id}<em>{imported ? '导入邮箱' : '自动获取'}</em></strong><small>{channel.message || (channel.ready ? '配置完整，可以使用' : '尚未完成配置')}</small></span><b>{channel.ready ? '可用' : '需配置'}</b></label>{!channel.ready ? <Button size="sm" onClick={() => { setBatchRegisterOpen(false); configure?.(); }}>{channel.id === 'pool' ? '去导入' : '去配置'}</Button> : null}</div>; })}</div>
        <div className="source-order registration-source-order"><div className="source-order-heading"><strong>本次优先顺序</strong><span>{selectedRegistrationChannels.length ? '数字越靠前越优先' : '勾选渠道后可调整'}</span></div>{selectedRegistrationChannels.length ? selectedRegistrationChannels.map((channel, index) => <div className="source-order-row" key={channel.id}><span className="source-order-number">{index + 1}</span><span>{channel.label || EMAIL_SOURCE_TEXT[channel.id] || channel.id}</span><span className="source-order-actions"><IconButton label="上移" icon={ArrowUp} size={14} disabled={index === 0} onClick={() => moveRegistrationSource(channel.id, -1)} /><IconButton label="下移" icon={ArrowDown} size={14} disabled={index === selectedRegistrationChannels.length - 1} onClick={() => moveRegistrationSource(channel.id, 1)} /></span></div>) : <span className="source-order-empty">未就绪的渠道会保持灰色；请先导入邮箱或到设置页补全配置。</span>}</div>
        {usingManualRegistration ? <div className="registration-manual-note"><strong>当前使用设置中的手动邮箱</strong><span>本次只能注册 1 个账号，收到验证码后需在 GPT账号页面提交。</span></div> : null}
      </div>
      <div className="registration-form-grid">
        <NumberField label="注册数量" value={usingManualRegistration ? 1 : registrationCount} min={1} max={registrationMaxCount} onChange={(value) => setRegistrationCount(Math.max(1, Math.min(registrationMaxCount, Number(value) || 1)))} />
        <NumberField label="并发数" value={workers} min={1} max={8} onChange={(value) => setWorkers(Math.max(1, Math.min(8, Number(value) || 1)))} />
      </div>
    </Modal>

    <Modal open={Boolean(importMode)} title={importMode === 'phones' ? '导入手机号' : '导入 ChatGPT 账号'} onClose={() => setImportMode(null)} wide footer={<><Button onClick={() => setImportMode(null)}>取消</Button><Button icon={Upload} variant="primary" onClick={submitImport}>导入</Button></>}>
      {importMode !== 'phones' ? <>
        <label className="field-label">本批账号代理（可选）<input value={importProxy} onChange={(event) => setImportProxy(event.target.value)} placeholder="http://host:port 或 socks5h://user:pass@host:port" autoComplete="off" /></label>
        <p className="field-help">JSON 每行可设置独立的 proxy，优先于本批代理；显式填写 "proxy": "" 表示本机网络。未填写代理的新账号默认本机网络；重复导入且没有填写代理时保留原记录。</p>
        <ProxyTest proxies={[importProxy.trim()]} label="测试导入代理 / 本机网络" />
      </> : null}
      <label className="field-label">粘贴素材<textarea rows="12" value={importText} onChange={(event) => setImportText(event.target.value)} placeholder={importMode === 'phones' ? '手机号----取码 URL----可用次数' : '每行一个账号；邮箱、HTTP 接码地址、密码、2FA 顺序不限'} spellCheck="false" /></label>
      {importMode === 'phones' ? <p className="field-help">每行一个手机号，可用次数可省略（默认 1 次，范围 0–3）。新号码默认不过期；导入后可直接修改“可用次数”，在“期限”列或“批量设置”中维护过期时间。</p> : <p className="field-help">按内容自动识别邮箱、HTTP 接码地址、2FA 和密码，无需固定字段顺序；<button type="button" className="field-help-link" onClick={() => { setImportMode(null); onOpenSettings?.('email:general'); }}>点击设置分隔符</button></p>}
    </Modal>

    <Modal open={Boolean(proxyRow)} title={`测试账号代理 · ${proxyRow?.email || ''}`} onClose={() => setProxyRow(null)} footer={<Button onClick={() => setProxyRow(null)}>关闭</Button>}>
      <p className="field-help">按账号当前保存的代理测试；未记录则测试本机网络。不会启动授权、更改 Roxy 窗口或保存配置。系统 VPN / TUN 仍可能影响本机出口。</p>
      {proxyRow ? <ProxyTest accountId={proxyRow.id} label="开始测试" /> : null}
    </Modal>

    <Modal open={Boolean(editAccount)} title={`编辑 ChatGPT 账号 · ${editAccount?.email || ''}`} onClose={() => setEditAccount(null)} wide footer={<><Button onClick={() => setEditAccount(null)}>取消</Button><Button variant="primary" icon={Check} onClick={saveEditor}>保存账号</Button></>}>
      <div className="edit-form"><div className="edit-grid">
        <label className="field-label">邮箱<input value={editDraft.email || ''} onChange={(event) => setEditDraft((current) => ({ ...current, email: event.target.value }))} readOnly={editAccount?.registration_status === 'registered'} autoComplete="off" />{editAccount?.registration_status === 'registered' ? <small className="field-help">已注册账号的邮箱属于登录身份，不能直接更改。</small> : null}</label>
        <label className="field-label">ChatGPT 密码<input value={editDraft.chatgpt_password || ''} onChange={(event) => setEditDraft((current) => ({ ...current, chatgpt_password: event.target.value }))} autoComplete="off" /></label>
        <label className="field-label">邮箱取码 URL<input value={editDraft.email_code_url || ''} onChange={(event) => setEditDraft((current) => ({ ...current, email_code_url: event.target.value }))} autoComplete="off" /></label>
        <label className="field-label">TOTP 密钥<input value={editDraft.totp_secret || ''} onChange={(event) => setEditDraft((current) => ({ ...current, totp_secret: event.target.value }))} autoComplete="off" /></label>
      </div><label className="field-label">备注<textarea rows="3" value={editDraft.note || ''} onChange={(event) => setEditDraft((current) => ({ ...current, note: event.target.value }))} /></label>
      <section className="edit-phone-pool" aria-label="账号代理">
        <h3>账号代理</h3>
        <p className="field-help">当前：{editAccount?.proxy_display || '本机网络'}。代理密码不会回填；保持不变不会丢失已有认证信息。</p>
        <Select value={editProxyMode} onChange={setEditProxyMode} options={[{ value: 'keep', label: '保持当前线路' }, { value: 'replace', label: '设置 / 替换代理' }, { value: 'direct', label: '清除代理，使用本机网络' }]} />
        {editProxyMode === 'replace' ? <label className="field-label">代理服务器<input value={editDraft.proxy || ''} onChange={(event) => setEditDraft((current) => ({ ...current, proxy: event.target.value }))} placeholder="http://host:port 或 socks5h://user:pass@host:port" autoComplete="off" /></label> : null}
        {editAccount ? <ProxyTest accountId={editProxyMode === 'keep' ? editAccount.id : ''} proxies={[editProxyMode === 'replace' ? (editDraft.proxy || '').trim() : '']} disabled={editProxyMode === 'replace' && !editDraft.proxy?.trim()} label="测试当前选择的线路" /> : null}
      </section>
      <section className="edit-phone-pool" aria-label="手机号接码池">
        {editAccount?.phone_status === 'verified' && editAccount.phone_number ? <p className="edit-verified-phone">已验证手机号：<strong>{editAccount.phone_number}</strong>（仅展示）</p> : null}
        <h3>加入手机号接码池（可选）</h3>
        <p className="field-help">填写一组号码和短信取码 URL，保存时会加入共享手机池，授权需要短信时才会取号。留空不会删除已有号码；已加入的候选号码请在“手机号接码池”查看和维护。</p>
        <div className="edit-grid">
          <label className="field-label">新增手机号<input value={editDraft.phone || ''} onChange={(event) => setEditDraft((current) => ({ ...current, phone: event.target.value }))} placeholder="加入手机池的号码" autoComplete="off" /></label>
          <label className="field-label">新增短信取码 URL<input value={editDraft.sms_code_url || ''} onChange={(event) => setEditDraft((current) => ({ ...current, sms_code_url: event.target.value }))} placeholder="https://…" autoComplete="off" /></label>
        </div>
      </section></div>
    </Modal>

    <Modal open={sub2Open} title="sub2api 服务与双向同步" onClose={() => setSub2Open(false)} wide footer={<><Button onClick={() => setSub2Open(false)}>关闭</Button><Button onClick={deleteSub2} variant="danger" disabled={!sub2ServiceId}>删除服务</Button><Button onClick={saveSub2}>保存服务</Button><Button icon={Download} disabled={!sub2ServiceId || !accountIds.length} onClick={() => runAction('/api/gpt-accounts/sub2/import', { account_ids: accountIds, service_id: sub2ServiceId, delete_terminal: false }, '已同步到 sub2api')}>同步到 sub2</Button><Button icon={RefreshCw} disabled={!sub2ServiceId} variant="primary" onClick={() => runAction('/api/gpt-accounts/sub2/sync', { service_id: sub2ServiceId }, '已从 sub2api 同步')}>从 sub2 同步</Button></>}>
      <div className="sub2-picker"><Select label="已保存服务" value={sub2ServiceId} onChange={(value) => { setSub2ServiceId(value); const service = sub2Services.find((item) => item.id === value); if (service) setSub2Draft({ name: service.name || '', homepage: service.homepage || '', api_base: service.api_base || '', admin_key: service.admin_key || '' }); }} options={[{ value: '', label: '新建服务' }, ...sub2Services.map((service) => ({ value: service.id, label: `${service.name} · ${service.api_base}` }))]} /><Button icon={Plus} onClick={() => { setSub2ServiceId(''); setSub2Draft({ name: '', homepage: '', api_base: '', admin_key: '' }); }}>新建</Button></div>
      <div className="edit-grid">{[['name', '服务名称'], ['homepage', '官网'], ['api_base', 'API 地址'], ['admin_key', '管理员 Key']].map(([key, label]) => <label className="field-label" key={key}>{label}<input type={key === 'admin_key' ? 'password' : 'text'} value={sub2Draft[key] || ''} onChange={(event) => setSub2Draft((current) => ({ ...current, [key]: event.target.value }))} autoComplete="off" /></label>)}</div>
    </Modal>

    <Modal open={Boolean(logRow)} title={`账号日志 · ${logRow?.email || ''}`} onClose={() => setLogRow(null)} wide footer={<Button onClick={() => setLogRow(null)}>关闭</Button>}><pre className="log-viewer">{logText}</pre></Modal>
  </div>;
}
