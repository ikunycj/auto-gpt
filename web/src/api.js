const jsonHeaders = { 'Content-Type': 'application/json' };

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

function goToLogin() {
  if (window.location.pathname !== '/login') {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(`/login?next=${encodeURIComponent(next || '/')}`);
  }
}

async function parseResponse(response) {
  const type = response.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload?.ok === false) {
      throw new ApiError(payload?.error || payload?.message || `请求失败（${response.status}）`, response.status, payload);
    }
    return payload;
  }
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new ApiError(text || `请求失败（${response.status}）`, response.status);
  }
  return response;
}

export async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(options.body && !(options.body instanceof FormData) ? jsonHeaders : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    goToLogin();
  }
  return parseResponse(response);
}

export function get(path, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  });
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return api(`${path}${suffix}`);
}

export function post(path, body = {}) {
  return api(path, { method: 'POST', body: JSON.stringify(body) });
}

export function put(path, body = {}) {
  return api(path, { method: 'PUT', body: JSON.stringify(body) });
}

export function patch(path, body = {}) {
  return api(path, { method: 'PATCH', body: JSON.stringify(body) });
}

export function del(path, body) {
  return api(path, {
    method: 'DELETE',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

export async function download(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
  });
  if (response.status === 401) {
    goToLogin();
    throw new ApiError('未授权', 401);
  }
  if (!response.ok) {
    const type = response.headers.get('content-type') || '';
    let message = `下载失败（${response.status}）`;
    if (type.includes('application/json')) {
      const payload = await response.json().catch(() => ({}));
      message = payload?.error || message;
    }
    throw new ApiError(message, response.status);
  }
  const blob = await response.blob();
  const disposition = response.headers.get('content-disposition') || '';
  const encoded = disposition.match(/filename\*=(?:UTF-8'')?([^;]+)/i)?.[1];
  const plain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : (plain || 'download');
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}

export async function copyText(text) {
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement('textarea');
  input.value = text;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  document.execCommand('copy');
  input.remove();
}
