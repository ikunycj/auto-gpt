import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity, Inbox, Menu, Network, Settings2, Smartphone,
  Users, X,
} from 'lucide-react';
import { get, post } from './api';
import { ActionButton, useConfirm, Toast, IconButton } from './components/ui';
import { usePolling } from './hooks';
import EmailPage from './pages/EmailPage';
import GptAccountsPage from './pages/GptAccountsPage';
import RelayPage from './pages/RelayPage';
import ConfigPage from './pages/ConfigPage';
import ProxyPage from './pages/ProxyPage';
import WorkspaceBrowser from './components/WorkspaceBrowser';

const NAV = [
  { id: 'email', label: '邮箱账号', caption: '待注册邮箱', icon: Inbox },
  { id: 'gpt', label: 'Codex授权', caption: '已注册与授权', icon: Users },
  { id: 'phones', label: '手机号', caption: '接码号码', icon: Smartphone },
  { id: 'proxy', label: '代理IP', caption: '代理池与出口测试', icon: Network },
  { id: 'settings', label: '设置', caption: '运行参数', icon: Settings2 },
];

const PAGE_TITLES = Object.fromEntries(NAV.map((item) => [item.id, item.label]));

function normalizeActiveTab(value) {
  return NAV.some((item) => item.id === value) ? value : 'gpt';
}

function ChatGPTMark({ size = 19 }) {
  return <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <path d="M16 3.5a6.05 6.05 0 0 0-5.86 7.55A6.05 6.05 0 0 0 5.2 21.5a6.05 6.05 0 0 0 7.02 6.99A6.05 6.05 0 0 0 22.8 21.5a6.05 6.05 0 0 0 1-11.7A6.05 6.05 0 0 0 16 3.5Z" stroke="currentColor" strokeWidth="2.15" strokeLinejoin="round" />
    <path d="M16 3.5v13m0 0 11.26 6.5M16 16 4.74 9.5M16 16v13m0-13 11.26-6.5M16 16 4.74 22.5" stroke="currentColor" strokeWidth="2.15" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}

function useToast() {
  const [toast, setToast] = useState(null);
  const notify = useCallback((message, type = 'info') => setToast({ message, type, id: Date.now() }), []);
  return [toast, notify, () => setToast(null)];
}

function Sidebar({ active, onChange, open, onClose, browserProps }) {
  return <aside className={`sidebar ${open ? 'is-open' : ''}`}>
    <div className="brand-lockup"><div className="brand-mark"><ChatGPTMark /></div><div><strong>ChatGPT 注册机</strong><span>registration console</span></div><IconButton label="关闭导航" icon={X} className="sidebar-close" onClick={onClose} /></div>
    <div className="sidebar-rule" />
    <nav className="nav-list" aria-label="工作区导航">
      <div className="nav-label">工作区</div>
      {NAV.map(({ id, label, caption, icon: Icon }) => <ActionButton key={id} className={`nav-item ${active === id ? 'is-active' : ''}`} onClick={() => { onChange(id); onClose?.(); }}><span className="nav-icon"><Icon size={17} /></span><span><strong>{label}</strong><small>{caption}</small></span>{active === id ? <span className="nav-current" /> : null}</ActionButton>)}
    </nav>
    <div className="sidebar-footer"><WorkspaceBrowser {...browserProps} /></div>
  </aside>;
}

function Topbar({ active, onMenu, summary, backendOnline, onRefresh, refreshing }) {
  return <header className="topbar"><div className="topbar-leading"><IconButton label="打开导航" icon={Menu} className="menu-button" onClick={onMenu} /><div><div className="breadcrumb">工作区 <span>/</span> <strong>{PAGE_TITLES[active]}</strong></div><h1>{PAGE_TITLES[active]}</h1></div></div><div className="topbar-actions"><div className={`topbar-health ${backendOnline ? 'is-online' : 'is-offline'}`} aria-live="polite"><span className={`status-dot ${backendOnline ? 'is-online' : 'is-offline'}`} /> <span>{backendOnline ? 'API 在线' : 'API 离线'}</span><small>{backendOnline ? `${summary?.accounts ?? 0} 个账号` : '无法连接后端'}</small></div><IconButton label="刷新概览" icon={Activity} className={refreshing ? 'is-loading' : ''} onClick={onRefresh} disabled={refreshing} /></div></header>;
}

function LoadingView() {
  return <div className="app-loading"><div className="loading-orbit"><span /></div><strong>正在连接本地服务</strong><span>读取工作区状态…</span></div>;
}

export default function App() {
  const confirm = useConfirm();
  const [active, setActive] = useState(() => {
    const stored = localStorage.getItem('gpt_console_active_tab');
    return normalizeActiveTab(stored);
  });
  const [settingsGroup, setSettingsGroup] = useState('');
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [registrationDriver, setRegistrationDriver] = useState('');
  const [driverLoading, setDriverLoading] = useState(true);
  const [driverSaving, setDriverSaving] = useState(false);
  const [driverError, setDriverError] = useState('');
  const [configRevision, setConfigRevision] = useState(0);
  const [registrationRequested, setRegistrationRequested] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [summary, setSummary] = useState(null);
  const [ready, setReady] = useState(false);
  const [backendOnline, setBackendOnline] = useState(false);
  const [toast, notify, clearToast] = useToast();
  const loadDriver = useCallback(async () => {
    setDriverLoading(true);
    try {
      const response = await get('/api/config');
      const fields = Array.isArray(response) ? response : response.fields || response.items || [];
      const driver = fields.find((field) => field.key === 'REGISTRATION_DRIVER')?.value;
      if (!driver) throw new Error('未读取到注册驱动，请重新读取配置。');
      setRegistrationDriver(driver);
      setDriverError('');
    } catch (error) {
      setDriverError(error.message);
    } finally {
      setDriverLoading(false);
    }
  }, []);
  useEffect(() => { loadDriver(); }, [loadDriver]);
  const changeDriver = useCallback(async (value) => {
    if (driverSaving || settingsDirty || value === registrationDriver) return;
    setDriverSaving(true);
    setDriverError('');
    try {
      const response = await post('/api/config', { updates: { REGISTRATION_DRIVER: value } });
      setRegistrationDriver(value);
      setConfigRevision((revision) => revision + 1);
      notify(response.note || '注册驱动已保存', response.reloaded === false ? 'warning' : 'success');
    } catch (error) {
      setDriverError(error.message);
      notify(error.message, 'error');
    } finally {
      setDriverSaving(false);
    }
  }, [driverSaving, settingsDirty, registrationDriver, notify]);
  const loadSummary = useCallback(async () => {
    try {
      const value = await get('/api/summary');
      setSummary(value);
      setBackendOnline(true);
      setReady(true);
      return value;
    } catch (error) {
      setBackendOnline(false);
      throw error;
    }
  }, []);
  // Absorb interval failures so a stopped backend does not create an
  // unhandled rejection while the UI continues to claim it is online.
  const pollSummary = useCallback(async () => {
    try {
      return await loadSummary();
    } catch (_) {
      return null;
    }
  }, [loadSummary]);
  const { refresh: refreshSummary, running: refreshing } = usePolling(pollSummary, 5000, true);

  const confirmSettingsExit = useCallback(async () => {
    if (!settingsDirty) return true;
    return confirm('设置有未保存修改，确定离开吗？');
  }, [settingsDirty, confirm]);
  const changePage = useCallback(async (pageId) => {
    if (pageId === active) return;
    if (active === 'settings' && pageId !== 'settings' && !await confirmSettingsExit()) return;
    setSettingsGroup('');
    if (active === 'settings') setSettingsDirty(false);
    setActive(pageId);
  }, [active, confirmSettingsExit]);
  const openSettings = useCallback(async (group = '') => {
    const nextGroup = group || '';
    if (active === 'settings' && nextGroup !== settingsGroup && settingsDirty && !await confirmSettingsExit()) return;
    if (active === 'settings' && nextGroup === settingsGroup) return;
    setSettingsGroup(nextGroup);
    setActive('settings');
  }, [active, confirmSettingsExit, settingsDirty, settingsGroup]);
  const openGptRegistration = useCallback(async () => {
    if (active === 'settings' && !await confirmSettingsExit()) return;
    setSettingsGroup('');
    if (active === 'settings') setSettingsDirty(false);
    setRegistrationRequested(true);
    setActive('gpt');
  }, [active, confirmSettingsExit]);
  const clearRegistrationRequest = useCallback(() => setRegistrationRequested(false), []);

  useEffect(() => { localStorage.setItem('gpt_console_active_tab', active); }, [active]);
  useEffect(() => { if (!ready) loadSummary().catch((error) => notify(error.message, 'error')); }, [ready, loadSummary, notify]);

  const handleRefresh = useCallback(async () => {
    const value = await refreshSummary();
    if (!value) notify('本地服务不可用', 'error');
  }, [notify, refreshSummary]);

  const page = useMemo(() => {
    const common = { notify, summary, onSummaryRefresh: refreshSummary };
    if (active === 'email') return <EmailPage {...common} onOpenSettings={openSettings} onOpenGptRegistration={openGptRegistration} />;
    if (active === 'phones') return <RelayPage {...common} mode="phones" />;
    if (active === 'proxy') return <ProxyPage {...common} />;
    if (active === 'settings') return <ConfigPage {...common} initialGroup={settingsGroup} onOpenEmail={() => changePage('email')} onOpenGptRegistration={openGptRegistration} onDirtyChange={setSettingsDirty} onConfigSaved={loadDriver} configRevision={configRevision} />;
    return <GptAccountsPage {...common} onOpenSettings={openSettings} onOpenProxy={() => changePage('proxy')} onOpenEmail={() => changePage('email')} registrationRequested={registrationRequested} onRegistrationRequestHandled={clearRegistrationRequest} />;
  }, [active, changePage, clearRegistrationRequest, notify, openGptRegistration, openSettings, refreshSummary, registrationRequested, settingsGroup, summary, loadDriver, configRevision]);

  if (!ready) return <LoadingView />;
  return <div className="app-shell"><Sidebar active={active} onChange={changePage} open={navOpen} onClose={() => setNavOpen(false)} browserProps={{ value: registrationDriver, loading: driverLoading, saving: driverSaving, error: driverError, settingsDirty, onChange: changeDriver, onOpenSettings: () => { openSettings('browser:roxy'); setNavOpen(false); } }} /><div className="app-main"><Topbar active={active} onMenu={() => setNavOpen(true)} summary={summary} backendOnline={backendOnline} onRefresh={handleRefresh} refreshing={refreshing} /><main className="page-content">{page}</main></div><div className={`sidebar-scrim ${navOpen ? 'is-visible' : ''}`} onClick={() => setNavOpen(false)} /><Toast toast={toast} onClose={clearToast} /></div>;
}
