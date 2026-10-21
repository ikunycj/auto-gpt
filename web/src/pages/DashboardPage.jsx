import {
  ArrowRight, Boxes, CheckCircle2, Code2, Inbox, RefreshCw, Smartphone,
  Users, XCircle,
} from 'lucide-react';
import { Link } from 'react-router';
import { Button } from '../components/ui';

const shortcuts = [
  { to: '/register', label: '新建注册任务', description: '配置并启动账号注册', icon: Boxes, tone: 'green' },
  { to: '/accounts', label: '管理账号', description: '查看账号状态与套餐', icon: Users, tone: 'blue' },
  { to: '/relay', label: 'GPT 接码台', description: 'OAuth 授权与 sub2api 同步', icon: Smartphone, tone: 'orange' },
  { to: '/codex', label: 'Codex 凭证', description: '管理本地授权凭证', icon: Code2, tone: 'violet' },
  { to: '/mailboxes', label: '邮箱素材', description: '检查邮箱池与可用状态', icon: Inbox, tone: 'teal' },
];

function count(value) {
  if (value === null || value === undefined) return '—';
  return Number(value || 0).toLocaleString('zh-CN');
}

function Metric({ icon: Icon, label, value, detail, tone, to }) {
  const content = (
    <>
      <span className={`overview-metric-icon tone-${tone}`}><Icon size={17} strokeWidth={1.9} /></span>
      <span className="overview-metric-copy">
        <span>{label}</span>
        <strong>{count(value)}</strong>
        <small>{detail}</small>
      </span>
      {to ? <ArrowRight className="overview-metric-arrow" size={15} /> : null}
    </>
  );
  return to
    ? <Link className="overview-metric" to={to}>{content}</Link>
    : <div className="overview-metric">{content}</div>;
}

export default function DashboardPage({ summary, error, onRefresh, refreshing }) {
  const emailTotal = Number(summary?.outlook_total || 0);
  const available = Number(summary?.outlook_available || 0);
  const used = Number(summary?.outlook_used || 0);
  const failed = Number(summary?.outlook_failed || 0);
  const remaining = Math.max(0, emailTotal - available - used - failed);
  const distribution = [
    { label: '可用', value: available, tone: 'available' },
    { label: '已使用', value: used, tone: 'used' },
    { label: '失败', value: failed, tone: 'failed' },
    { label: '其他', value: remaining, tone: 'other' },
  ];
  const dateLabel = new Intl.DateTimeFormat('zh-CN', {
    weekday: 'long', month: 'long', day: 'numeric',
  }).format(new Date());

  return (
    <div className="dashboard-page">
      <div className="dashboard-heading">
        <div>
          <div className="dashboard-date">{dateLabel}</div>
          <h1>工作台总览</h1>
          <p>查看本地账号、邮箱素材和授权资源。</p>
        </div>
        <Button icon={RefreshCw} onClick={onRefresh} loading={refreshing}>刷新数据</Button>
      </div>

      {error ? (
        <div className="dashboard-error" role="alert">
          <XCircle size={17} />
          <span>{error}</span>
          <Button size="sm" icon={RefreshCw} onClick={onRefresh} loading={refreshing}>重试</Button>
        </div>
      ) : null}

      <section className="overview-metrics" aria-label="工作区统计">
        <Metric
          icon={Users}
          label="已注册账号"
          value={summary?.accounts}
          detail="本地账号总量"
          tone="blue"
          to="/accounts"
        />
        <Metric
          icon={Inbox}
          label="邮箱素材"
          value={summary ? emailTotal : null}
          detail="当前邮箱池"
          tone="violet"
          to="/mailboxes"
        />
        <Metric
          icon={CheckCircle2}
          label="可用邮箱"
          value={summary ? available : null}
          detail="等待注册使用"
          tone="green"
          to="/mailboxes"
        />
        <Metric
          icon={XCircle}
          label="异常邮箱"
          value={summary ? failed : null}
          detail="需要检查或处理"
          tone="orange"
          to="/mailboxes"
        />
      </section>

      <div className="overview-content">
        <section className="overview-panel overview-pool" aria-labelledby="pool-title">
          <div className="overview-panel-header">
            <div>
              <h2 id="pool-title">邮箱池状态</h2>
              <p>本地导入的邮箱素材分布</p>
            </div>
            <Link to="/mailboxes" className="text-link">查看邮箱池 <ArrowRight size={14} /></Link>
          </div>

          <div className="pool-total">
            <strong>{count(summary ? emailTotal : null)}</strong>
            <span>条邮箱素材</span>
          </div>
          <div
            className="pool-bar"
            role="img"
            aria-label={`邮箱状态：可用 ${available}，已使用 ${used}，失败 ${failed}，其他 ${remaining}`}
          >
            {distribution.map((item) => (
              <span
                key={item.tone}
                className={`pool-bar-segment pool-${item.tone}`}
                style={{ width: `${emailTotal ? (item.value / emailTotal) * 100 : 0}%` }}
              />
            ))}
          </div>
          <div className="pool-legend">
            {distribution.map((item) => (
              <div className="pool-legend-item" key={item.tone}>
                <span className={`pool-legend-dot pool-${item.tone}`} />
                <span>{item.label}</span>
                <strong>{count(summary ? item.value : null)}</strong>
              </div>
            ))}
          </div>
        </section>

        <section className="overview-panel overview-shortcuts" aria-labelledby="shortcuts-title">
          <div className="overview-panel-header">
            <div>
              <h2 id="shortcuts-title">常用入口</h2>
              <p>直接继续工作</p>
            </div>
          </div>
          <div className="shortcut-list">
            {shortcuts.map(({ to, label, description, icon: Icon, tone }) => (
              <Link className="shortcut-row" to={to} key={to}>
                <span className={`shortcut-icon tone-${tone}`}><Icon size={17} /></span>
                <span className="shortcut-copy"><strong>{label}</strong><small>{description}</small></span>
                <ArrowRight size={15} className="shortcut-arrow" />
              </Link>
            ))}
          </div>
        </section>
      </div>

      <section className="overview-status" aria-label="本地服务">
        <span className="overview-status-mark"><CheckCircle2 size={17} /></span>
        <span><strong>本地工作区</strong><small>数据保存在当前设备，仅本机服务可访问。</small></span>
        <span className="overview-status-value">127.0.0.1</span>
      </section>
    </div>
  );
}
