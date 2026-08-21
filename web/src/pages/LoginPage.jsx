import { KeyRound, LockKeyhole, ShieldCheck } from 'lucide-react';

export default function LoginPage() {
  const params = new URLSearchParams(window.location.search);
  const next = params.get('next')?.startsWith('/') && !params.get('next')?.startsWith('//') ? params.get('next') : '/';
  const hasError = params.get('error') === 'invalid';
  return <main className="login-page"><section className="login-shell"><div className="login-brand"><div className="brand-mark"><ShieldCheck size={20} /></div><div><strong>ChatGPT 注册机</strong><span>本地控制台</span></div></div><div className="login-copy"><div className="eyebrow"><LockKeyhole size={14} /> LOCAL ACCESS</div><h1>进入工作区</h1><p>使用启动日志或 `.env` 中配置的 WebUI 授权码。</p></div><form className="login-form" method="post" action="/login"><input type="hidden" name="next" value={next} /><label className="field-label">授权码<div className="login-input"><KeyRound size={17} /><input type="password" name="auth_code" required autoFocus autoComplete="current-password" placeholder="输入 WebUI 授权码" /></div></label>{hasError ? <div className="login-error" role="alert">授权码错误，请检查后重试。</div> : null}<label className="checkbox login-remember"><input type="checkbox" name="remember" value="1" defaultChecked /><span>在这台设备上保持登录</span></label><button className="button button-primary login-submit" type="submit">登录控制台</button></form><div className="login-footnote"><span className="status-dot" /> 仅通过本地 Flask session 验证，不在浏览器存储授权码。</div></section></main>;
}
