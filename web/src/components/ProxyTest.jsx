import { useEffect, useRef, useState } from 'react';
import { Activity } from 'lucide-react';
import { post } from '../api';
import { Button } from './ui';

// Tests drafts without saving them. Account tests read credentials server-side.
export default function ProxyTest({ proxies = [''], accountId = '', disabled = false, label = '测试代理' }) {
  const [busy, setBusy] = useState(false);
  const [items, setItems] = useState([]);
  const [error, setError] = useState('');
  const requestId = useRef(0);
  const routeKey = JSON.stringify([accountId, proxies]);
  useEffect(() => {
    requestId.current += 1;
    setItems([]); setError(''); setBusy(false);
    return () => { requestId.current += 1; };
  }, [routeKey]);

  async function test() {
    if (!accountId && proxies.length > 20) {
      setError('每次最多测试 20 条代理，请分批测试。');
      return;
    }
    const current = ++requestId.current;
    setBusy(true); setError(''); setItems([]);
    try {
      const response = accountId
        ? await post(`/api/gpt-accounts/${encodeURIComponent(accountId)}/proxy/test`)
        : await post('/api/proxies/test', { proxies: proxies.length ? proxies : [''] });
      if (current === requestId.current) setItems(response.items || []);
    } catch (failure) {
      if (current === requestId.current) setError(failure.message || '测试失败');
    } finally {
      if (current === requestId.current) setBusy(false);
    }
  }

  return <div className="proxy-test">
    <Button type="button" icon={Activity} onClick={test} disabled={disabled} loading={busy}>{label}</Button>
    {busy ? <p className="field-help">正在检测出口 IP 和授权站点连通性，不会打开浏览器或登录账号…</p> : null}
    <div aria-live="polite">
      {error ? <p className="field-help" role="alert">{error}</p> : null}
      {items.map((item, index) => <div className="config-tool-result" key={index}>
        <strong>{index + 1}. {item.proxy_display} · {item.ok ? '连通测试通过' : '测试未通过'}</strong>
        <p>出口 IP：{item.exit_ip || '未获取'}</p>
        {item.network ? <p>出口检测：{item.network.message}（{item.network.latency_ms} ms）</p> : null}
        {item.target ? <p>授权站点：{item.target.message}（{item.target.latency_ms} ms）</p> : null}
        <small>{item.message}</small>
      </div>)}
    </div>
  </div>;
}
