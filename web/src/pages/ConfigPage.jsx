import { useCallback, useEffect, useMemo, useState } from 'react';
import { Cloud, Database, KeyRound, RefreshCw, Save, Settings2, ShieldCheck, Terminal, WandSparkles } from 'lucide-react';
import { get, post } from '../api';
import { Button, Card, ErrorState, InlineNotice, SectionHeader, Select } from '../components/ui';

function fieldValue(field) {
  if (field.value === null || field.value === undefined) return field.type === 'bool' ? false : '';
  if (field.type === 'list_str_multiline' && Array.isArray(field.value)) return field.value.join('\n');
  return field.value;
}
function groupIcon(name) { if (/邮箱|OTP/i.test(name)) return Database; if (/Codex/i.test(name)) return Terminal; if (/授权|密钥/i.test(name)) return KeyRound; if (/代理|浏览器/i.test(name)) return Cloud; return Settings2; }

export default function ConfigPage({ notify }) {
  const [fields, setFields] = useState([]); const [draft, setDraft] = useState({}); const [activeGroup, setActiveGroup] = useState(''); const [loading, setLoading] = useState(true); const [saving, setSaving] = useState(false); const [error, setError] = useState(''); const [savedAt, setSavedAt] = useState(null); const [toolBusy, setToolBusy] = useState(''); const [toolResult, setToolResult] = useState(''); const [roxyItems, setRoxyItems] = useState([]); const [roxySelection, setRoxySelection] = useState('');
  const load = useCallback(async () => { try { const response = await get('/api/config'); const list = Array.isArray(response) ? response : (response.fields || response.items || []); setFields(list); setDraft(Object.fromEntries(list.map((field) => [field.key, fieldValue(field)]))); setActiveGroup((current) => current || list[0]?.group || ''); setError(''); } catch (loadError) { setError(loadError.message); } finally { setLoading(false); } }, []);
  useEffect(() => { load(); }, [load]);
  const groups = useMemo(() => [...new Set(fields.map((field) => field.group || '其他'))], [fields]);
  const visible = fields.filter((field) => (field.group || '其他') === activeGroup);
  function update(key, value) { setDraft((current) => ({ ...current, [key]: value })); }
  async function save() { setSaving(true); try { const response = await post('/api/config', { updates: draft }); notify(response.note || '配置已保存并热加载', 'success'); setSavedAt(new Date()); await load(); } catch (saveError) { notify(saveError.message, 'error'); } finally { setSaving(false); } }

  async function loadRoxy() {
    setToolBusy('roxy'); setToolResult('');
    try {
      const response = await get('/api/roxy/workspaces');
      const items = response.items || [];
      setRoxyItems(items);
      const current = items.find((item) => String(item.id) === String(draft.ROXY_WORKSPACE_ID || '') && String(item.projectId || '') === String(draft.ROXY_PROJECT_ID || ''));
      setRoxySelection(current ? `${current.id}::${current.projectId || ''}` : '');
      setToolResult(`已获取 ${items.length} 个团队/项目`);
    } catch (toolError) { notify(toolError.message, 'error'); }
    finally { setToolBusy(''); }
  }

  async function saveRoxy() {
    if (!roxySelection) return notify('请先选择团队/项目', 'warning');
    const [workspaceId, projectId = ''] = roxySelection.split('::');
    setToolBusy('roxy-save');
    try {
      const response = await post('/api/config', { updates: { ROXY_WORKSPACE_ID: workspaceId, ROXY_PROJECT_ID: projectId } });
      notify(response.note || 'Roxy 团队/项目已保存', 'success'); setToolResult('团队/项目选择已保存并热加载'); await load();
    } catch (toolError) { notify(toolError.message, 'error'); }
    finally { setToolBusy(''); }
  }

  async function cloudmail(mode) {
    setToolBusy(mode); setToolResult('');
    try {
      const common = { api_base: String(draft.CLOUDMAIL_API_BASE || '').trim(), admin_email: String(draft.CLOUDMAIL_ADMIN_EMAIL || '').trim(), password: String(draft.CLOUDMAIL_PASSWORD || '').trim() };
      const response = mode === 'cloudmail-token'
        ? await post('/api/cloudmail/gen-token', { ...common, path: String(draft.CLOUDMAIL_TOKEN_PATH || '/api/public/genToken').trim() })
        : await post('/api/cloudmail/domains', { ...common, token: String(draft.CLOUDMAIL_AUTH_TOKEN || '').trim() });
      setToolResult(mode === 'cloudmail-token' ? (response.message || 'Token 已生成') : `${response.message || '域名已获取'}：${(response.domains || []).join(', ')}`);
      notify(response.message || 'CloudMail 操作完成', 'success'); await load();
    } catch (toolError) { notify(toolError.message, 'error'); }
    finally { setToolBusy(''); }
  }

  return <div className="page-stack"><div className="page-intro"><div><div className="eyebrow"><Settings2 size={14} /> RUNTIME SETTINGS</div><h2>运行配置</h2><p>启动后可在这里配置邮箱、代理、浏览器、Codex、短信和第三方 API；保存会写入本地 `.env` 并热加载，无需手工编辑配置源码。</p></div><div className="intro-actions"><Button icon={RefreshCw} onClick={load} loading={loading}>重新读取</Button><Button icon={Save} variant="primary" onClick={save} loading={saving}>保存全部</Button></div></div><InlineNotice>首次启动没有固定授权码时，请执行 <code>./webui.sh logs</code> 查看本次临时授权码；数据库位置和 WebUI 监听地址仍属于启动级设置。</InlineNotice>{savedAt ? <InlineNotice>最近保存：{savedAt.toLocaleTimeString('zh-CN')}</InlineNotice> : null}{error ? <ErrorState message={error} onRetry={load} /> : null}<div className="config-layout"><Card className="config-nav"><div className="config-nav-heading"><ShieldCheck size={16} /><strong>配置分组</strong></div>{groups.map((group) => { const Icon = groupIcon(group); return <button className={`config-nav-item ${activeGroup === group ? 'is-active' : ''}`} key={group} onClick={() => { setActiveGroup(group); setToolResult(''); }}><Icon size={15} /><span>{group}</span><small>{fields.filter((field) => (field.group || '其他') === group).length}</small></button>; })}</Card><Card className="config-editor"><SectionHeader title={activeGroup || '配置'} description="修改后点击右上角保存，密码和授权码不会在列表中明文展示。" />{activeGroup === 'RoxyBrowser' ? <div className="config-tool"><div><strong>Roxy 团队 / 项目</strong><p>从本地 Roxy API 读取可用工作区，并同步工作区和项目 ID。</p></div><div className="config-tool-actions"><Button icon={Cloud} loading={toolBusy === 'roxy'} onClick={loadRoxy}>获取团队</Button>{roxyItems.length ? <Select value={roxySelection} onChange={setRoxySelection} options={[{ value: '', label: '选择团队 / 项目' }, ...roxyItems.map((item) => ({ value: `${item.id}::${item.projectId || ''}`, label: item.label || `${item.name || item.id} · ${item.projectId || '无项目'}` }))]} /> : null}<Button variant="primary" disabled={!roxySelection} loading={toolBusy === 'roxy-save'} onClick={saveRoxy}>保存选择</Button></div></div> : null}{activeGroup === '邮箱 / OTP' && fields.some((field) => field.key.startsWith('CLOUDMAIL_')) ? <div className="config-tool"><div><strong>CloudMail 工具</strong><p>使用上方草稿中的 API 地址、管理员邮箱和密码生成 Token 或读取域名。</p></div><div className="config-tool-actions"><Button icon={KeyRound} loading={toolBusy === 'cloudmail-token'} onClick={() => cloudmail('cloudmail-token')}>生成 Token</Button><Button icon={Database} loading={toolBusy === 'cloudmail-domains'} onClick={() => cloudmail('cloudmail-domains')}>获取域名</Button></div></div> : null}{toolResult ? <div className="config-tool-result">{toolResult}</div> : null}{loading ? <div className="form-loading"><span /><span /><span /></div> : <div className="config-fields">{visible.map((field) => <ConfigField key={field.key} field={field} value={draft[field.key]} onChange={(value) => update(field.key, value)} />)}{!visible.length ? <div className="empty-state"><WandSparkles size={20} /><strong>暂无可编辑字段</strong></div> : null}</div>}</Card></div></div>;
}

function ConfigField({ field, value, onChange }) {
  const type = String(field.type || 'str').toLowerCase(); const secret = Boolean(field.secret); const label = field.label || field.key; const help = field.help || '';
  if (type === 'bool' || type === 'boolean') return <label className="config-field config-switch"><span className="config-field-copy"><strong>{label}</strong><small>{help}</small><code>{field.key}</code></span><input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} /><span className="switch-track" /></label>;
  if (type === 'list_str_multiline' || type === 'list' || String(value || '').includes('\n')) return <label className="config-field"><span className="config-field-copy"><strong>{label}</strong><small>{help}</small><code>{field.key}</code></span><textarea rows="5" value={value ?? ''} onChange={(event) => onChange(event.target.value)} /></label>;
  if (type === 'int' || type === 'float' || type === 'number') return <label className="config-field"><span className="config-field-copy"><strong>{label}</strong><small>{help}</small><code>{field.key}</code></span><input type="number" step={type === 'float' ? '0.1' : '1'} value={value ?? ''} onChange={(event) => onChange(event.target.value)} /></label>;
  return <label className="config-field"><span className="config-field-copy"><strong>{label}</strong><small>{help}</small><code>{field.key}</code></span><input type={secret ? 'password' : 'text'} value={value ?? ''} onChange={(event) => onChange(event.target.value)} autoComplete="off" /></label>;
}
