import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity, Gauge, Inbox, Menu, Settings2, Smartphone,
  Users, X,
} from 'lucide-react';
import { get, post } from './api';
import { Toast, IconButton } from './components/ui';
import { usePolling } from './hooks';
import EmailPage from './pages/EmailPage';
import GptAccountsPage from './pages/GptAccountsPage';
import RelayPage from './pages/RelayPage';
import ConfigPage from './pages/ConfigPage';

const NAV = [
  { id: 'email', label: '邮箱', caption: '注册邮箱', icon: Inbox },
  { id: 'gpt', label: 'GPT账号', caption: '注册与授权', icon: Users },
  { id: 'phones', label: '手机号池', caption: '接码号码', icon: Smartphone },
  { id: 'settings', label: '设置', caption: '运行参数', icon: Settings2 },
];

const PAGE_TITLES = Object.fromEntries(NAV.map((item) => [item.id, item.label]));

const LEGACY_ACTIVE_TAB = {
  register: 'email',
  accounts: 'gpt',
  codex: 'gpt',
  relay: 'gpt',
  outlook: 'email',
  config: 'settings',
};

function normalizeActiveTab(value) {
  const candidate = LEGACY_ACTIVE_TAB[value] || value;
  return NAV.some((item) => item.id === candidate) ? candidate : 'gpt';
}

function useToast() {
  const [toast, setToast] = useState(null);
  const notify = useCallback((message, type = 'info') => setToast({ message, type, id: Date.now() }), []);
  return [toast, notify, () => setToast(null)];
}

function Sidebar({ active, onChange, open, onClose, backendOnline }) {
  return <aside className={`sidebar ${open ? 'is-open' : ''}`}>
    <div className="brand-lockup"><div className="brand-mark"><Gauge size={18} /></div><div><strong>ChatGPT 注册机</strong><span>registration console</span></div><IconButton label="关闭导航" icon={X} className="sidebar-close" onClick={onClose} /></div>
    <div className="sidebar-rule" />
    <nav className="nav-list" aria-label="工作区导航">
      <div className="nav-label">工作区</div>
      {NAV.map(({ id, label, caption, icon: Icon }) => <button key={id} className={`nav-item ${active === id ? 'is-active' : ''}`} onClick={() => { onChange(id); onClose?.(); }}><span className="nav-icon"><Icon size={17} /></span><span><strong>{label}</strong><small>{caption}</small></span>{active === id ? <span className="nav-current" /> : null}</button>)}
    </nav>
    <div className="sidebar-footer"><div className={`local-status ${backendOnline ? 'is-online' : 'is-offline'}`} aria-live="polite"><span className={`status-dot ${backendOnline ? 'is-online' : 'is-offline'}`} /> <span>{backendOnline ? '本地服务在线' : '本地服务离线'}</span></div><span className="build-tag">React workspace</span></div>
  </aside>;
}

function Topbar({ active, onMenu, summary, backendOnline, onRefresh, refreshing }) {
  return <header className="topbar"><div className="topbar-leading"><IconButton label="打开导航" icon={Menu} className="menu-button" onClick={onMenu} /><div><div className="breadcrumb">工作区 <span>/</span> <strong>{PAGE_TITLES[active]}</strong></div><h1>{PAGE_TITLES[active]}</h1></div></div><div className="topbar-actions"><div className={`topbar-health ${backendOnline ? 'is-online' : 'is-offline'}`} aria-live="polite"><span className={`status-dot ${backendOnline ? 'is-online' : 'is-offline'}`} /> <span>{backendOnline ? 'API 在线' : 'API 离线'}</span><small>{backendOnline ? `${summary?.accounts ?? 0} 个账号` : '无法连接后端'}</small></div><IconButton label="刷新概览" icon={Activity} className={refreshing ? 'is-loading' : ''} onClick={onRefresh} disabled={refreshing} /></div></header>;
}

function LoadingView() {
  return <div className="app-loading"><div className="loading-orbit"><span /></div><strong>正在连接本地服务</strong><span>读取工作区状态…</span></div>;
}

export default function App() {
  const [active, setActive] = useState(() => {
    const stored = localStorage.getItem('gpt_console_active_tab');
    return normalizeActiveTab(stored);
  });
  const [settingsGroup, setSettingsGroup] = useState('');
  const [registrationRequested, setRegistrationRequested] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [summary, setSummary] = useState(null);
  const [ready, setReady] = useState(false);
  const [backendOnline, setBackendOnline] = useState(false);
  const [toast, notify, clearToast] = useToast();
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

  const changePage = useCallback((pageId) => {
    setSettingsGroup('');
    setActive(pageId);
  }, []);
  const openSettings = useCallback((group = '') => {
    setSettingsGroup(group);
    setActive('settings');
  }, []);
  const openGptRegistration = useCallback(() => {
    setSettingsGroup('');
    setRegistrationRequested(true);
    setActive('gpt');
  }, []);
  const clearRegistrationRequest = useCallback(() => setRegistrationRequested(false), []);

  useEffect(() => { localStorage.setItem('gpt_console_active_tab', active); }, [active]);
  useEffect(() => { if (!ready) loadSummary().catch((error) => notify(error.message, 'error')); }, [ready, loadSummary, notify]);

  const handleRefresh = useCallback(async () => {
    const value = await refreshSummary();
    if (!value) notify('本地服务不可用', 'error');
  }, [notify, refreshSummary]);

  const page = useMemo(() => {
    const common = { notify, summary, onSummaryRefresh: refreshSummary };
    if (active === 'email') return <EmailPage {...common} onOpenSettings={openSettings} />;
    if (active === 'phones') return <RelayPage {...common} mode="phones" />;
    if (active === 'settings') return <ConfigPage {...common} initialGroup={settingsGroup} onOpenEmail={() => changePage('email')} onOpenGptRegistration={openGptRegistration} />;
    return <GptAccountsPage {...common} onOpenSettings={openSettings} registrationRequested={registrationRequested} onRegistrationRequestHandled={clearRegistrationRequest} />;
  }, [active, changePage, clearRegistrationRequest, notify, openGptRegistration, openSettings, refreshSummary, registrationRequested, settingsGroup, summary]);

  if (!ready) return <LoadingView />;
  return <div className="app-shell"><Sidebar active={active} onChange={changePage} open={navOpen} onClose={() => setNavOpen(false)} backendOnline={backendOnline} /><div className="app-main"><Topbar active={active} onMenu={() => setNavOpen(true)} summary={summary} backendOnline={backendOnline} onRefresh={handleRefresh} refreshing={refreshing} /><main className="page-content">{page}</main></div><div className={`sidebar-scrim ${navOpen ? 'is-visible' : ''}`} onClick={() => setNavOpen(false)} /><Toast toast={toast} onClose={clearToast} /></div>;
}
