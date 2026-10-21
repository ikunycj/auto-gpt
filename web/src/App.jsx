import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity, Archive, Boxes, CircleDollarSign, Code2, Database, Gauge, Inbox,
  LayoutDashboard, LogOut, Menu, PackageOpen, Settings2, ShieldCheck, Smartphone,
  Users, X,
} from 'lucide-react';
import { get, post } from './api';
import { Toast, IconButton } from './components/ui';
import { usePolling } from './hooks';
import RegisterPage from './pages/RegisterPage';
import AccountsPage from './pages/AccountsPage';
import CodexPage from './pages/CodexPage';
import RelayPage from './pages/RelayPage';
import MailboxPage from './pages/MailboxPage';
import ConfigPage from './pages/ConfigPage';

const NAV = [
  { id: 'register', label: '注册任务', caption: '创建与监控', icon: LayoutDashboard },
  { id: 'accounts', label: '已注册账号', caption: '查活与维护', icon: Users },
  { id: 'codex', label: 'Codex 凭证', caption: '导出与清理', icon: Code2 },
  { id: 'relay', label: 'GPT 接码台', caption: 'OAuth 与手机池', icon: Smartphone },
  { id: 'outlook', label: '邮箱池', caption: '素材与状态', icon: Inbox },
  { id: 'config', label: '运行配置', caption: '安全参数', icon: Settings2 },
];

const PAGE_TITLES = Object.fromEntries(NAV.map((item) => [item.id, item.label]));

function useToast() {
  const [toast, setToast] = useState(null);
  const notify = useCallback((message, type = 'info') => setToast({ message, type, id: Date.now() }), []);
  return [toast, notify, () => setToast(null)];
}

function Sidebar({ active, onChange, open, onClose }) {
  return <aside className={`sidebar ${open ? 'is-open' : ''}`}>
    <div className="brand-lockup"><div className="brand-mark"><Gauge size={18} /></div><div><strong>ChatGPT 注册机</strong><span>registration console</span></div><IconButton label="关闭导航" icon={X} className="sidebar-close" onClick={onClose} /></div>
    <div className="sidebar-rule" />
    <nav className="nav-list" aria-label="工作区导航">
      <div className="nav-label">工作区</div>
      {NAV.map(({ id, label, caption, icon: Icon }) => <button key={id} className={`nav-item ${active === id ? 'is-active' : ''}`} onClick={() => { onChange(id); onClose?.(); }}><span className="nav-icon"><Icon size={17} /></span><span><strong>{label}</strong><small>{caption}</small></span>{active === id ? <span className="nav-current" /> : null}</button>)}
    </nav>
    <div className="sidebar-footer"><div className="local-status"><span className="status-dot" /> <span>本地服务在线</span></div><span className="build-tag">React workspace</span></div>
  </aside>;
}

function Topbar({ active, onMenu, summary, onRefresh, refreshing }) {
  return <header className="topbar"><div className="topbar-leading"><IconButton label="打开导航" icon={Menu} className="menu-button" onClick={onMenu} /><div><div className="breadcrumb">工作区 <span>/</span> <strong>{PAGE_TITLES[active]}</strong></div><h1>{PAGE_TITLES[active]}</h1></div></div><div className="topbar-actions"><div className="topbar-health"><span className="status-dot" /> <span>API 在线</span><small>{summary?.accounts ?? 0} 个账号</small></div><IconButton label="刷新概览" icon={Activity} className={refreshing ? 'is-loading' : ''} onClick={onRefresh} disabled={refreshing} /><form method="post" action="/logout"><button className="logout-button" type="submit"><LogOut size={15} />退出</button></form></div></header>;
}

function LoadingView() {
  return <div className="app-loading"><div className="loading-orbit"><span /></div><strong>正在连接本地服务</strong><span>读取工作区状态…</span></div>;
}

export default function App() {
  const [active, setActive] = useState(() => {
    const stored = localStorage.getItem('gpt_console_active_tab');
    return NAV.some((item) => item.id === stored) ? stored : 'register';
  });
  const [navOpen, setNavOpen] = useState(false);
  const [summary, setSummary] = useState(null);
  const [ready, setReady] = useState(false);
  const [toast, notify, clearToast] = useToast();
  const loadSummary = useCallback(async () => {
    const value = await get('/api/summary');
    setSummary(value);
    setReady(true);
    return value;
  }, []);
  const { refresh: refreshSummary, running: refreshing } = usePolling(loadSummary, 5000, true);

  useEffect(() => { localStorage.setItem('gpt_console_active_tab', active); }, [active]);
  useEffect(() => { if (!ready) loadSummary().catch((error) => notify(error.message, 'error')); }, [ready, loadSummary, notify]);

  const page = useMemo(() => {
    const common = { notify, summary, onSummaryRefresh: refreshSummary };
    if (active === 'accounts') return <AccountsPage {...common} />;
    if (active === 'codex') return <CodexPage {...common} />;
    if (active === 'relay') return <RelayPage {...common} />;
    if (active === 'outlook') return <MailboxPage {...common} />;
    if (active === 'config') return <ConfigPage {...common} />;
    return <RegisterPage {...common} />;
  }, [active, notify, summary, refreshSummary]);

  if (!ready) return <LoadingView />;
  return <div className="app-shell"><Sidebar active={active} onChange={setActive} open={navOpen} onClose={() => setNavOpen(false)} /><div className="app-main"><Topbar active={active} onMenu={() => setNavOpen(true)} summary={summary} onRefresh={() => refreshSummary().catch((error) => notify(error.message, 'error'))} refreshing={refreshing} /><main className="page-content">{page}</main></div><div className={`sidebar-scrim ${navOpen ? 'is-visible' : ''}`} onClick={() => setNavOpen(false)} /><Toast toast={toast} onClose={clearToast} /></div>;
}
