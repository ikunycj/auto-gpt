import { useCallback, useEffect, useState } from 'react';
import {
  Activity, ArrowLeftToLine, ArrowRightToLine, Boxes, ChevronRight,
  Code2, Gauge, Inbox, LayoutDashboard, LogOut, Menu, Settings2, Smartphone,
  Users, X,
} from 'lucide-react';
import { Navigate, NavLink, Route, Routes, useLocation } from 'react-router';
import { get } from './api';
import { IconButton, Toast } from './components/ui';
import { usePolling } from './hooks';
import AccountsPage from './pages/AccountsPage';
import CodexPage from './pages/CodexPage';
import ConfigPage from './pages/ConfigPage';
import DashboardPage from './pages/DashboardPage';
import MailboxPage from './pages/MailboxPage';
import RegisterPage from './pages/RegisterPage';
import RelayPage from './pages/RelayPage';

const NAV_GROUPS = [
  {
    label: '工作台',
    items: [{ id: 'home', path: '/', label: '总览', icon: LayoutDashboard, end: true }],
  },
  {
    label: '账号管理',
    items: [
      { id: 'register', path: '/register', label: '注册任务', icon: Boxes },
      { id: 'accounts', path: '/accounts', label: '已注册账号', icon: Users },
      { id: 'codex', path: '/codex', label: 'Codex 凭证', icon: Code2 },
    ],
  },
  {
    label: '授权与资源',
    items: [
      { id: 'relay', path: '/relay', label: 'GPT 接码台', icon: Smartphone },
      { id: 'mailboxes', path: '/mailboxes', label: '邮箱池', icon: Inbox },
    ],
  },
  {
    label: '系统',
    items: [{ id: 'settings', path: '/settings', label: '运行配置', icon: Settings2 }],
  },
];

const ALL_NAV = NAV_GROUPS.flatMap((group) => group.items);
const PAGE_BY_PATH = Object.fromEntries(ALL_NAV.map((item) => [item.path, item]));

function useToast() {
  const [toast, setToast] = useState(null);
  const notify = useCallback((message, type = 'info') => {
    setToast({ message, type, id: Date.now() });
  }, []);
  return [toast, notify, () => setToast(null)];
}

function Sidebar({ open, collapsed, onClose, onToggle }) {
  const ToggleIcon = collapsed ? ArrowRightToLine : ArrowLeftToLine;
  return (
    <aside className={`sidebar ${open ? 'is-open' : ''}`} aria-label="主导航">
      <div className="brand-lockup">
        <NavLink to="/" className="brand-home" aria-label="返回工作台" onClick={onClose}>
          <span className="brand-mark"><Gauge size={18} strokeWidth={2.2} /></span>
          <span className="brand-copy">
            <strong>GPT 工作台</strong>
            <small>本地运营控制台</small>
          </span>
        </NavLink>
        <IconButton
          label={collapsed ? '展开侧栏' : '收起侧栏'}
          icon={ToggleIcon}
          className="sidebar-toggle"
          onClick={onToggle}
        />
        <IconButton label="关闭导航" icon={X} className="sidebar-close" onClick={onClose} />
      </div>

      <div className="sidebar-rule" />
      <nav className="nav-list" aria-label="工作区导航">
        {NAV_GROUPS.map((group) => (
          <div className="nav-group" key={group.label}>
            <div className="nav-label">{group.label}</div>
            {group.items.map(({ id, path, label, icon: Icon, end }) => (
              <NavLink
                key={id}
                to={path}
                end={end}
                title={collapsed ? label : undefined}
                aria-label={label}
                className={({ isActive }) => `nav-item ${isActive ? 'is-active' : ''}`}
                onClick={onClose}
              >
                <span className="nav-icon"><Icon size={17} strokeWidth={1.9} /></span>
                <span className="nav-item-copy"><strong>{label}</strong></span>
                <span className="nav-current" aria-hidden="true" />
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        <div className="local-status"><span className="status-dot" /><span>本地服务</span></div>
        <span className="build-tag">127.0.0.1 · 私有工作区</span>
      </div>
    </aside>
  );
}

function Topbar({ activePage, menuOpen, summary, summaryError, onMenu, onRefresh, refreshing }) {
  const HealthIcon = summaryError ? X : Activity;
  return (
    <header className="topbar">
      <div className="topbar-leading">
        <IconButton label="打开导航" icon={Menu} className="menu-button" onClick={onMenu} />
        <div className="breadcrumb" aria-label="面包屑">
          <span>本地工作区</span><ChevronRight size={14} aria-hidden="true" />
          <strong>{activePage.label}</strong>
        </div>
      </div>
      <div className="topbar-actions">
        <div className={`topbar-health ${summaryError ? 'is-error' : ''}`} role="status">
          <span className="health-indicator"><HealthIcon size={13} /></span>
          <span>{summaryError ? 'API 未连接' : 'API 已连接'}</span>
          {summary ? <small>{Number(summary.accounts || 0).toLocaleString('zh-CN')} 个账号</small> : null}
        </div>
        <IconButton
          label="刷新工作台数据"
          icon={Activity}
          className={refreshing ? 'is-loading' : ''}
          onClick={onRefresh}
          disabled={refreshing}
        />
        <span className="topbar-divider" aria-hidden="true" />
        <form method="post" action="/logout">
          <button className="logout-button" type="submit"><LogOut size={15} />退出</button>
        </form>
      </div>
      <span className="sr-only" aria-live="polite">{menuOpen ? '导航已打开' : ''}</span>
    </header>
  );
}

function LoadingView() {
  return (
    <main className="app-loading" aria-label="加载工作台">
      <div className="loading-orbit"><span /></div>
      <strong>正在连接本地服务</strong>
      <span>读取工作区状态…</span>
    </main>
  );
}

export default function App() {
  const location = useLocation();
  const activePage = PAGE_BY_PATH[location.pathname] || PAGE_BY_PATH['/'];
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('gpt_console_sidebar_collapsed') === 'true');
  const [summary, setSummary] = useState(null);
  const [summaryError, setSummaryError] = useState('');
  const [ready, setReady] = useState(false);
  const [toast, notify, clearToast] = useToast();

  const loadSummary = useCallback(async () => {
    try {
      const value = await get('/api/summary');
      setSummary(value);
      setSummaryError('');
      return value;
    } catch (error) {
      setSummaryError(error.message || '读取工作台数据失败');
      return null;
    } finally {
      setReady(true);
    }
  }, []);
  const { refresh: refreshSummary, running: refreshing } = usePolling(loadSummary, 15000, true);

  useEffect(() => {
    localStorage.setItem('gpt_console_sidebar_collapsed', String(collapsed));
  }, [collapsed]);

  useEffect(() => {
    setNavOpen(false);
    window.scrollTo(0, 0);
    document.title = `${activePage.label} · GPT 工作台`;
  }, [location.pathname, activePage.label]);

  useEffect(() => {
    if (!navOpen) return undefined;
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') setNavOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [navOpen]);

  if (!ready) return <LoadingView />;

  return (
    <div className={`app-shell ${collapsed ? 'is-collapsed' : ''}`}>
      <Sidebar
        open={navOpen}
        collapsed={collapsed}
        onClose={() => setNavOpen(false)}
        onToggle={() => setCollapsed((value) => !value)}
      />
      <div className={`sidebar-scrim ${navOpen ? 'is-visible' : ''}`} onClick={() => setNavOpen(false)} />
      <div className="app-main">
        <Topbar
          activePage={activePage}
          menuOpen={navOpen}
          summary={summary}
          summaryError={summaryError}
          onMenu={() => setNavOpen(true)}
          onRefresh={() => refreshSummary()}
          refreshing={refreshing}
        />
        <main className="page-content" id="main-content" tabIndex={-1}>
          <Routes>
            <Route
              path="/"
              element={(
                <DashboardPage
                  summary={summary}
                  error={summaryError}
                  onRefresh={() => refreshSummary()}
                  refreshing={refreshing}
                />
              )}
            />
            <Route path="/register" element={<RegisterPage notify={notify} summary={summary} onRefreshSummary={refreshSummary} />} />
            <Route path="/accounts" element={<AccountsPage notify={notify} />} />
            <Route path="/codex" element={<CodexPage notify={notify} />} />
            <Route path="/relay" element={<RelayPage notify={notify} />} />
            <Route path="/mailboxes" element={<MailboxPage notify={notify} />} />
            <Route path="/outlook" element={<Navigate to="/mailboxes" replace />} />
            <Route path="/settings" element={<ConfigPage notify={notify} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
      <Toast toast={toast} onClose={clearToast} />
    </div>
  );
}
