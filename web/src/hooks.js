import { useCallback, useEffect, useRef, useState } from 'react';

export function usePolling(callback, interval, enabled = true) {
  const callbackRef = useRef(callback);
  const [running, setRunning] = useState(false);
  useEffect(() => { callbackRef.current = callback; }, [callback]);
  const run = useCallback(async () => {
    setRunning(true);
    try { return await callbackRef.current(); } finally { setRunning(false); }
  }, []);
  useEffect(() => {
    if (!enabled || !interval) return undefined;
    let cancelled = false;
    const tick = async () => {
      if (!cancelled) await callbackRef.current();
    };
    tick();
    const timer = window.setInterval(tick, interval);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [enabled, interval]);
  return { refresh: run, running };
}

export function useDebounced(value, delay = 250) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function useAsyncAction(notify) {
  const [busy, setBusy] = useState(false);
  const run = useCallback(async (action, successMessage) => {
    setBusy(true);
    try {
      const result = await action();
      if (successMessage) notify?.(successMessage, 'success');
      return result;
    } catch (error) {
      notify?.(error.message || '操作失败', 'error');
      throw error;
    } finally { setBusy(false); }
  }, [notify]);
  return { busy, run };
}
