import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity } from 'lucide-react';
import { post } from '../api';
import { Button } from './ui';

// Tests drafts without saving them. Account tests read credentials server-side.
export default function ProxyTest({ proxies = [''], accountId = '', disabled = false, label = '测试代理', autoRun = 0, hideButton = false }) {
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

  const test = useCallback(async () => {
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
  }, [accountId, proxies]);

  useEffect(() => {
    if (autoRun) test();
  }, [autoRun, test]);

  return <div className="proxy-test">
    {!hideButton ? <Button type="button" icon={Activity} onClick={test} disabled={disabled || busy} loading={busy}>{label}</Button> : null}
    {busy ? <p className="field-help">正在检测出口 IP 和网络连通性…</p> : null}
    <div aria-live="polite">
      {error ? <p className="field-help" role="alert">{error}</p> : null}
      {items.map((item, index) => <div className="config-tool-result" key={index}>
        <strong>{index + 1}. {item.proxy_display} · {item.ok ? '代理连通测试通过' : '连通测试未通过'}</strong>
        <p>出口 IP：{item.exit_ip || '未获取'}</p>
        {item.network ? <p>出口检测：{item.network.message}（{item.network.latency_ms} ms）</p> : null}
        <small>{item.message}</small>
      </div>)}
    </div>
  </div>;
}
