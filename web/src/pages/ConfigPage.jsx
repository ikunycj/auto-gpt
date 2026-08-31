import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDown, ArrowUp, AtSign, Cloud, CloudCog, Database, Info, KeyRound,
  Fingerprint, Globe2, Link2, Mail, MailPlus, Monitor, PanelsTopLeft, RefreshCw,
  Save, Settings2, ShieldCheck, Smartphone, Terminal, Upload, WandSparkles,
} from 'lucide-react';
import { get, post } from '../api';
import {
  Button, Card, ErrorState, IconButton, InlineNotice, SectionHeader, Select,
} from '../components/ui';

const EMAIL_CHANNELS = [
  {
    id: 'general',
    label: '总览与行为',
    title: '邮箱总览与验证码',
    icon: Settings2,
    description: '设置默认使用的邮箱渠道、优先顺序和验证码等待时间。',
    tooltip: '先完成具体渠道的配置，再回到这里勾选默认渠道并调整顺序。',
    navHint: '默认渠道、顺序与验证码',
    acquisition: '可以使用自己能正常收信的邮箱，也可以使用已导入的邮箱，或由临时邮箱服务自动提供。只需配置你准备使用的渠道。',
    steps: ['进入左侧具体邮箱渠道，按页面提示准备邮箱或服务账号。', '回到本页勾选已就绪渠道，并调整默认优先顺序。', '注册账号时还可以为本次任务重新勾选和排序。'],
  },
  {
    id: 'generic_api',
    label: '接码链接邮箱',
    title: '邮箱 + 接码链接',
    icon: Link2,
    description: '使用服务商提供的邮箱地址和专属接码链接。',
    tooltip: '服务商必须同时提供邮箱地址和该邮箱专用的 HTTP 或 HTTPS 取码链接。',
    navHint: '导入邮箱和专属取码链接',
    acquisition: '向你使用的邮箱接码服务商获取。每个邮箱都必须配有一个能够查看该邮箱验证码的专属链接。',
    steps: ['进入“邮箱”菜单并点击“导入邮箱”。', '邮箱类型选择“接码 API”，每行粘贴一个邮箱和对应的接码链接。', '导入成功且邮箱状态为“可用”后，本渠道才能启用。'],
  },
  {
    id: 'outlook',
    label: 'Outlook',
    title: 'Outlook 邮箱',
    icon: Mail,
    description: '使用你持有并允许自动收信的 Outlook 邮箱。',
    tooltip: '每个邮箱需要同时具备邮箱、密码、客户端 ID 和刷新令牌，缺少任意一项都无法自动取码。',
    navHint: '导入已有 Outlook 邮箱',
    acquisition: '使用你自己创建或合法持有的 Outlook 邮箱，并准备该邮箱的客户端 ID 和刷新令牌。普通邮箱密码本身不足以自动收取验证码。',
    steps: ['进入“邮箱”菜单并点击“导入邮箱”。', '邮箱类型选择“Outlook”，按邮箱、密码、客户端 ID、刷新令牌的顺序逐行导入。', '导入成功且邮箱状态为“可用”后，再选择下方的收信方式；不确定时保持“自动”。'],
  },
  {
    id: 'cloudflare_domain',
    label: 'Cloudflare 域名',
    title: 'Cloudflare 域名邮箱',
    icon: AtSign,
    description: '使用自己的域名生成邮箱，并把来信转发到 QQ 邮箱。',
    tooltip: '适合已经拥有域名，并愿意在 Cloudflare 设置邮件转发的用户。',
    navHint: '自有域名转发到 QQ 邮箱',
    acquisition: '先购买并持有一个域名，把域名接入 Cloudflare，然后在 Email Routing 中将来信转发到你的 QQ 邮箱。',
    steps: ['在 Cloudflare 开启 Email Routing，并确认测试邮件能转发到 QQ 邮箱。', '在 QQ 邮箱中开启 IMAP 服务并生成授权码；这里填写的是授权码，不是 QQ 登录密码。', '填写转发域名、QQ 邮箱和授权码，其余收信服务器设置通常保持默认。'],
  },
  {
    id: 'cloudflare',
    label: 'Cloudflare Worker',
    title: 'Cloudflare Worker 临时邮箱',
    icon: CloudCog,
    description: '连接一套已经部署好的 Cloudflare 临时邮箱服务。',
    tooltip: '如果你没有服务地址或不知道鉴权方式，请先不要启用该渠道。',
    navHint: '连接已有 Worker 邮箱服务',
    acquisition: '使用你自己部署的兼容临时邮箱服务，或向服务管理员索取访问地址、鉴权方式和访问密钥。',
    steps: ['向服务管理员确认访问地址和是否需要密钥。', '填写服务地址，并按管理员提供的信息选择鉴权方式、填写密钥。', '路径、超时和随机名称长度属于高级设置，没有特别说明时保持默认。'],
  },
  {
    id: 'gptmail',
    label: 'GPTMail',
    title: 'GPTMail 临时邮箱',
    icon: Database,
    description: '由 GPTMail 在注册时提供临时邮箱并自动收取验证码。',
    tooltip: '需要先在 GPTMail 服务中取得可用的 API Key。',
    navHint: '使用 GPTMail 服务账号',
    acquisition: '前往 GPTMail 服务网站申请或购买 API Key。项目不会自动生成服务密钥，也无法保证第三方服务一直可用。',
    website: { label: '打开 GPTMail', href: 'https://mail.chatgpt.org.uk' },
    steps: ['打开 GPTMail 网站并取得 API Key。', '将 API Key 填入下方并保存。', '保存后渠道显示“已就绪”，注册时即可勾选。'],
  },
  {
    id: 'mailnest',
    label: 'MailNest',
    title: 'MailNest 临时邮箱',
    icon: Database,
    description: '由 MailNest 在注册时购买或领取临时邮箱并自动取码。',
    tooltip: '需要 MailNest API Key、项目代码和足够的账户余额。',
    navHint: '使用 MailNest 余额取邮箱',
    acquisition: '前往 MailNest 注册账号，获取 API Key，并确保账户余额足够购买邮箱。OpenAI 邮箱项目通常使用页面给出的项目代码。',
    website: { label: '打开 MailNest', href: 'https://mailnest.top' },
    steps: ['登录 MailNest，充值后在账户中复制 API Key。', '在购买邮箱页面确认项目代码，并填写到下方。', '保存后渠道显示“已就绪”；余额不足时仍可能领取失败。'],
  },
  {
    id: 'cloudmail',
    label: 'CloudMail',
    title: 'CloudMail 临时邮箱',
    icon: Cloud,
    description: '连接一套已有的 CloudMail 服务并使用其中的邮箱域名。',
    tooltip: '需要服务地址和 Token；没有 Token 时可以用管理员账号和密码生成。',
    navHint: '连接已有 CloudMail 服务',
    acquisition: '使用你自己部署的 CloudMail 服务，或向服务管理员取得服务地址和 Token。若管理员只提供了账号密码，可在本页生成 Token。',
    website: { label: '查看 CloudMail 使用说明', href: 'https://doc.skymail.ink/api/api-doc' },
    steps: ['填写 CloudMail 服务地址。', '有 Token 就直接填写；没有 Token 时填写管理员邮箱和密码，再点击“生成 Token”。', '点击“获取域名”确认服务中存在可用域名，然后保存。'],
  },
];

// Settings expose grouped email workspaces while the runtime keeps the
// original provider IDs for EMAIL_SOURCE and imported mailbox compatibility.
const EMAIL_SETTING_GROUPS = [
  {
    id: 'general',
    label: '总览与行为',
    title: '邮箱总览与验证码',
    icon: Settings2,
    description: '设置默认使用的邮箱渠道、优先顺序和验证码等待时间。',
    tooltip: '先完成具体渠道的配置，再回到这里勾选默认渠道并调整顺序。',
    navHint: '默认渠道、顺序与验证码',
    channelIds: ['general'],
  },
  {
    id: 'domain',
    label: '域名邮箱',
    title: '域名邮箱',
    icon: AtSign,
    description: '配置自有域名邮箱，把注册验证码转发到你能正常收信的邮箱。',
    tooltip: '适合已经拥有域名，并愿意在 Cloudflare 设置邮件转发的用户。',
    navHint: 'Cloudflare 域名转发',
    channelIds: ['cloudflare_domain'],
  },
  {
    id: 'temporary',
    label: '临时邮箱',
    title: '临时邮箱',
    icon: Cloud,
    description: '分别配置 Cloudflare Worker、GPTMail、MailNest 和 CloudMail 临时邮箱。',
    tooltip: '四个临时邮箱服务相互独立，完成哪一个就启用哪一个。',
    navHint: '四个独立临时邮箱服务',
    channelIds: ['cloudflare', 'gptmail', 'mailnest', 'cloudmail'],
  },
];

const EMAIL_CHANNEL_TO_GROUP = Object.fromEntries(
  EMAIL_SETTING_GROUPS.flatMap((group) => group.channelIds.map((channelId) => [channelId, group.id])),
);

// Imported Outlook/API mailboxes are managed on the 邮箱 page, so their
// optional transport fields stay under the general email settings instead of
// creating hidden provider menus.
const HIDDEN_EMAIL_CHANNEL_DISPLAY = { outlook: 'general', generic_api: 'general' };

const SMS_CHANNELS = [
  {
    id: 'general', label: '总览与开关', title: '接码平台总览', icon: Smartphone,
    description: '选择当前接码平台，并决定是否把它加入手机号池。',
    tooltip: '开启“加入手机号池”后，当前平台会显示在手机号池顶部，授权时作为动态取号来源。',
    navHint: '当前平台、开关和公共参数',
    setup: '先选择一个平台并完成对应模块的配置；开启开关后，GPT账号授权会优先使用该平台动态取号。',
  },
  {
    id: 'grizzly', label: 'GrizzlySMS', title: 'GrizzlySMS', icon: Terminal,
    description: '配置 GrizzlySMS 的 API 地址和密钥。',
    tooltip: '需要在 GrizzlySMS 后台获取 API Key，并确认服务代码和国家代码可购买 OpenAI 号码。',
    navHint: 'API 地址与密钥',
    setup: '需要：GrizzlySMS API 地址和 API Key；总览页的服务代码、国家代码也必须与平台商品匹配。',
  },
  {
    id: 'l', label: 'L 接码服务', title: 'L 接码服务', icon: Link2,
    description: '配置项目内置的 L JSON 管理接口。',
    tooltip: '填写 L 服务地址和后台授权码；服务需要提供 take-phone 与 fetch-code 管理接口。',
    navHint: '本地 L API 与授权码',
    setup: '需要：L 服务地址和后台授权码；号码前缀只在 L 返回本地号码时填写。',
  },
  {
    id: 'h', label: 'H 接码服务', title: 'H 接码服务', icon: Database,
    description: '配置项目内置的 H JSON 管理接口。',
    tooltip: '填写 H 服务地址和后台授权码，并选择复用号码或每次取新号。',
    navHint: '本地 H API 与取号策略',
    setup: '需要：H 服务地址和后台授权码；取号方式决定优先复用历史可用号码还是每次申请新号。',
  },
];

const BROWSER_MODULES = [
  {
    id: 'general',
    label: '总览与驱动',
    title: '浏览器总览与驱动',
    icon: Settings2,
    description: '选择注册流程默认使用的浏览器驱动。各驱动的凭证、会话和指纹参数在对应模块中单独配置。',
    tooltip: '先在这里选择注册驱动；选择后只需要配置对应驱动模块，其他浏览器的设置不会参与当前流程。',
    setup: '需要：选择一个注册驱动。授权驱动在“Codex”模块中单独设置。',
  },
  {
    id: 'roxy',
    label: 'RoxyBrowser',
    title: 'RoxyBrowser',
    icon: Cloud,
    description: '配置 Roxy API、环境生命周期、团队项目、代理池和浏览器行为。',
    tooltip: '填写 Roxy API 地址和 Token；如启用一号一环境，再配置工作区/项目和环境创建策略。',
    setup: '需要：Roxy API 地址；API Token 仅在 Roxy 开启鉴权时填写。',
  },
  {
    id: 'cloak',
    label: 'CloakBrowser',
    title: 'CloakBrowser',
    icon: ShieldCheck,
    description: '配置 Cloak 无头模式、人工行为、出口地区、指纹和用户目录。',
    tooltip: '填写 Cloak License（如有）；通常先配置是否无头、是否使用代理，再按需要固定地区或指纹。',
    setup: '需要：已安装 CloakBrowser；License 可选，免费 binary 可留空。',
  },
  {
    id: 'browser_use',
    label: 'Browser Use',
    title: 'Browser Use Cloud',
    icon: PanelsTopLeft,
    description: '配置 Browser Use Cloud 的 API、CDP、代理国家、Profile 和会话超时。',
    tooltip: '填写 Browser Use API Key 和 CDP 地址；启用内置代理时填写两位国家码，例如 jp 或 us。',
    setup: '需要：Browser Use API Key；官方 CDP 模式通常保持默认地址。',
  },
  {
    id: 'skyvern',
    label: 'Skyvern',
    title: 'Skyvern Cloud Browser',
    icon: CloudCog,
    description: '配置 Skyvern Session、Profile、代理地区、浏览器类型和会话保留策略。',
    tooltip: '填写 Skyvern API Key 和 API 地址；代理地区使用 jp/us 等简写，系统会转换为 Skyvern 参数。',
    setup: '需要：Skyvern API Key 和可用 API 地址；Profile、代理地区均可选。',
  },
  {
    id: 'system_chrome',
    label: '系统 Chrome',
    title: '系统 Chrome / CDP',
    icon: Monitor,
    description: '配置本机 Chrome 可执行文件、CDP 启动、页面超时和调试时的保留策略。',
    tooltip: '留空会自动查找 Google Chrome；只有自动查找失败或使用便携版时才填写完整路径。',
    setup: '需要：本机安装 Google Chrome；可选填写 Chrome 可执行文件路径。',
  },
  {
    id: 'locale',
    label: '地区与指纹',
    title: '地区与指纹策略',
    icon: Fingerprint,
    description: '统一配置浏览器语言、时区、出口 IP 自动画像和云代理拒绝策略。',
    tooltip: '地区画像应与代理出口一致；开启按出口 IP 自动画像后，失败会回退到手动地区画像。',
    setup: '需要：按代理出口选择 jp/cn/us/sg 等地区；不确定时可开启按出口 IP 自动画像。',
  },
];

// Fallback for an older WebUI process that has not yet reloaded the API metadata.
const EMAIL_CHANNEL_BY_KEY = {
  USE_EMAIL_SERVICE: 'general', REGISTER_EMAIL: 'general', REGISTER_PASSWORD: 'general',
  REGISTER_NAME: 'general', OTP_MAX_WAIT: 'general', OTP_POLL_INTERVAL: 'general',
  OTP_SETTLE_SECONDS: 'general', EMAIL_SOURCE: 'general', EMAIL_IMPORT_SEPARATORS: 'general',
  OUTLOOK_FETCH_MODE: 'outlook', OUTLOOK_API_BASE: 'outlook',
  EMAIL_DOMAIN: 'cloudflare_domain', QQ_EMAIL: 'cloudflare_domain', QQ_IMAP_SERVER: 'cloudflare_domain',
  QQ_IMAP_PORT: 'cloudflare_domain', QQ_IMAP_PASSWORD: 'cloudflare_domain',
  GPTMAIL_API_KEY: 'gptmail', MAIL_NEST_API_KEY: 'mailnest', MAIL_NEST_PROJECT_CODE: 'mailnest',
  CLOUDMAIL_API_BASE: 'cloudmail', CLOUDMAIL_ADMIN_EMAIL: 'cloudmail', CLOUDMAIL_PASSWORD: 'cloudmail',
  CLOUDMAIL_TOKEN_PATH: 'cloudmail', CLOUDMAIL_AUTH_TOKEN: 'cloudmail', CLOUDMAIL_DOMAINS: 'cloudmail',
  CLOUDMAIL_AUTO_ADD_USER: 'cloudmail', CLOUDMAIL_RANDOM_LOCAL_LENGTH: 'cloudmail',
  CLOUDFLARE_API_BASE: 'cloudflare', CLOUDFLARE_API_KEY: 'cloudflare', CLOUDFLARE_AUTH_MODE: 'cloudflare',
  CLOUDFLARE_CUSTOM_AUTH: 'cloudflare', CLOUDFLARE_PATH_ACCOUNTS: 'cloudflare',
  CLOUDFLARE_PATH_MESSAGES: 'cloudflare', CLOUDFLARE_PATH_DOMAINS: 'cloudflare',
  CLOUDFLARE_PATH_TOKEN: 'cloudflare', CLOUDFLARE_DEFAULT_DOMAINS: 'cloudflare',
  CLOUDFLARE_REQUEST_TIMEOUT: 'cloudflare', CLOUDFLARE_NAME_LENGTH: 'cloudflare',
};

const SMS_CHANNEL_BY_KEY = {
  SMS_PROVIDER: 'general', SMS_POOL_PLATFORM_ENABLED: 'general', SMS_COUNTRY: 'general',
  SMS_SERVICE: 'general', SMS_MAX_PRICE: 'general', SMS_MAX_RETRIES: 'general',
  SMS_CODE_WAIT: 'general', SMS_POLL_INTERVAL: 'general', SMS_REQUEST_TIMEOUT: 'general',
  SMS_API_BASE: 'grizzly', SMS_API_KEY: 'grizzly',
  H_API_BASE: 'h', H_ADMIN_AUTH_CODE: 'h', H_PHONE_PREFIX: 'h', H_PHONE_ACQUIRE_MODE: 'h',
  L_API_BASE: 'l', L_ADMIN_AUTH_CODE: 'l', L_PHONE_PREFIX: 'l',
};

const ADVANCED_EMAIL_FIELD_KEYS = new Set([
  'REGISTER_PASSWORD', 'OTP_SETTLE_SECONDS',
  'OUTLOOK_API_BASE',
  'QQ_IMAP_SERVER', 'QQ_IMAP_PORT',
  'CLOUDFLARE_CUSTOM_AUTH', 'CLOUDFLARE_PATH_ACCOUNTS', 'CLOUDFLARE_PATH_MESSAGES',
  'CLOUDFLARE_PATH_DOMAINS', 'CLOUDFLARE_PATH_TOKEN', 'CLOUDFLARE_DEFAULT_DOMAINS',
  'CLOUDFLARE_REQUEST_TIMEOUT', 'CLOUDFLARE_NAME_LENGTH',
  'CLOUDMAIL_TOKEN_PATH', 'CLOUDMAIL_DOMAINS', 'CLOUDMAIL_AUTO_ADD_USER',
  'CLOUDMAIL_RANDOM_LOCAL_LENGTH',
]);

// Older WebUI processes may not return browser_module metadata until restarted.
const BROWSER_MODULE_BY_KEY = {
  REGISTRATION_DRIVER: 'general',
  BROWSER_LOCALE_PROFILE: 'locale', AUTO_BROWSER_LOCALE_FROM_IP: 'locale',
  IP_GEO_TIMEOUT: 'locale', REJECT_CLOUD_PROXY: 'locale',
};

const BROWSER_LEGACY_KEYS = {
  RoxyBrowser: 'roxy',
  CloakBrowser: 'cloak',
  'Browser Use': 'browser_use',
  Skyvern: 'skyvern',
  '系统 Chrome': 'system_chrome',
  '地区与指纹': 'locale',
};

const GROUP_META = {
  '功能开关': '决定哪些自动流程运行',
  '代理浏览器': '浏览器驱动、指纹和运行参数', '人工节奏': '注册操作之间的随机等待',
  '代理池': '注册和查询使用的网络代理', '提链': '支付链接提取服务',
  Codex: '授权、Token 和同步服务', '接码平台': '手机号接码平台接口', 'Flow Trigger': '注册成功后的回调接口',
};

const SETTINGS_SECTIONS = [
  { label: '运行与安全', groups: ['功能开关', '人工节奏'] },
  { label: '浏览器与网络', groups: ['代理池'] },
  { label: '外部服务', groups: ['Codex', 'Flow Trigger', '提链', '接码平台'] },
];

function fieldValue(field) {
  if (field.value === null || field.value === undefined) return field.type === 'bool' ? false : '';
  if (field.type === 'list_str_multiline' && Array.isArray(field.value)) return field.value.join('\n');
  return field.value;
}

function channelForField(field) {
  const channel = field.email_channel || EMAIL_CHANNEL_BY_KEY[field.key] || '';
  return HIDDEN_EMAIL_CHANNEL_DISPLAY[channel] || channel;
}

function smsChannelForField(field) {
  return field.sms_channel || SMS_CHANNEL_BY_KEY[field.key] || '';
}

function browserModuleForField(field) {
  if (field.browser_module) return field.browser_module;
  if (BROWSER_MODULE_BY_KEY[field.key]) return BROWSER_MODULE_BY_KEY[field.key];
  const key = String(field.key || '');
  if (key.startsWith('ROXY_')) return 'roxy';
  if (key.startsWith('CLOAK_')) return 'cloak';
  if (key.startsWith('BROWSER_USE_')) return 'browser_use';
  if (key.startsWith('SKYVERN_')) return 'skyvern';
  if (key.startsWith('CHROME_CDP_')) return 'system_chrome';
  return '';
}

function normalizeActiveKey(value) {
  const raw = String(value || '').trim();
  if (!raw || raw === '邮箱 / OTP' || raw === '邮箱' || raw === 'email') return raw ? 'email:general' : '';
  if (raw.startsWith('email:')) {
    const id = raw.slice('email:'.length);
    if (EMAIL_SETTING_GROUPS.some((group) => group.id === id)) return raw;
    const groupId = EMAIL_CHANNEL_TO_GROUP[id];
    return groupId ? `email:${groupId}` : 'email:general';
  }
  if (raw === '代理浏览器' || raw === 'browser') return 'browser:general';
  if (BROWSER_LEGACY_KEYS[raw]) return `browser:${BROWSER_LEGACY_KEYS[raw]}`;
  if (raw.startsWith('browser:')) return BROWSER_MODULES.some((item) => `browser:${item.id}` === raw) ? raw : 'browser:general';
  if (raw === '接码平台' || raw === 'sms') return 'sms:general';
  if (raw.startsWith('sms:')) return SMS_CHANNELS.some((item) => `sms:${item.id}` === raw) ? raw : 'sms:general';
  if (EMAIL_SETTING_GROUPS.some((group) => group.id === raw)) return `email:${raw}`;
  if (EMAIL_CHANNEL_TO_GROUP[raw]) return `email:${EMAIL_CHANNEL_TO_GROUP[raw]}`;
  return raw;
}

function parseSources(value) {
  const valid = new Set(EMAIL_CHANNELS.filter((item) => item.id !== 'general').map((item) => item.id));
  const raw = String(value || '').replaceAll(';', ',').replaceAll('|', ',').split(',');
  return [...new Set(raw.map((item) => item.trim()).filter((item) => valid.has(item)))];
}

function sourceText(value) {
  return parseSources(value).join(',');
}

function groupIcon(name) {
  if (/Codex/i.test(name)) return Terminal;
  if (/授权|密钥/i.test(name)) return KeyRound;
  if (/代理|浏览器/i.test(name)) return Cloud;
  return Settings2;
}

export default function ConfigPage({ notify, summary, initialGroup = '', onOpenEmail, onOpenGptRegistration, onSummaryRefresh }) {
  const [fields, setFields] = useState([]);
  const [draft, setDraft] = useState({});
  const [activeKey, setActiveKey] = useState(() => normalizeActiveKey(initialGroup));
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [savedAt, setSavedAt] = useState(null);
  const [toolBusy, setToolBusy] = useState('');
  const [toolResult, setToolResult] = useState('');
  const [roxyItems, setRoxyItems] = useState([]);
  const [roxySelection, setRoxySelection] = useState('');

  const load = useCallback(async () => {
    try {
      const response = await get('/api/config');
      const list = Array.isArray(response) ? response : (response.fields || response.items || []);
      setFields(list);
      setDraft(Object.fromEntries(list.map((field) => [field.key, fieldValue(field)])));
      setActiveKey((current) => {
        const normalized = normalizeActiveKey(current || initialGroup);
        const emailValid = normalized.startsWith('email:') && EMAIL_SETTING_GROUPS.some((group) => `email:${group.id}` === normalized);
        const browserValid = normalized.startsWith('browser:') && BROWSER_MODULES.some((item) => `browser:${item.id}` === normalized);
        const smsValid = normalized.startsWith('sms:') && SMS_CHANNELS.some((item) => `sms:${item.id}` === normalized);
        const groupValid = list.some((field) => !channelForField(field) && !smsChannelForField(field) && !browserModuleForField(field) && (field.group || '其他') === normalized);
        if (emailValid || browserValid || smsValid || groupValid) return normalized;
        return 'email:general';
      });
      setError('');
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }, [initialGroup]);

  useEffect(() => { load(); }, [load]);

  const emailFields = useMemo(() => fields.filter((field) => channelForField(field)), [fields]);
  const smsFields = useMemo(() => fields.filter((field) => smsChannelForField(field)), [fields]);
  const browserFields = useMemo(() => fields.filter((field) => browserModuleForField(field)), [fields]);
  const groups = useMemo(() => [...new Set(fields.filter((field) => !channelForField(field) && !smsChannelForField(field) && !browserModuleForField(field)).map((field) => field.group || '其他'))], [fields]);
  const activeEmailGroupId = activeKey.startsWith('email:') ? activeKey.slice('email:'.length) : '';
  const activeEmailGroup = EMAIL_SETTING_GROUPS.find((group) => group.id === activeEmailGroupId) || null;
  const activeChannelId = activeEmailGroup?.channelIds.length === 1 ? activeEmailGroup.channelIds[0] : '';
  const activeChannel = EMAIL_CHANNELS.find((item) => item.id === activeChannelId) || null;
  const activeEmailChannels = activeEmailGroup?.channelIds
    .map((channelId) => EMAIL_CHANNELS.find((item) => item.id === channelId))
    .filter(Boolean) || [];
  const activeSmsId = activeKey.startsWith('sms:') ? activeKey.slice('sms:'.length) : '';
  const activeSms = SMS_CHANNELS.find((item) => item.id === activeSmsId) || null;
  const activeBrowserId = activeKey.startsWith('browser:') ? activeKey.slice('browser:'.length) : '';
  const activeBrowser = BROWSER_MODULES.find((item) => item.id === activeBrowserId) || null;
  const unsortedVisible = activeChannel
    ? emailFields.filter((field) => channelForField(field) === activeChannel.id)
    : activeSms
      ? smsFields.filter((field) => smsChannelForField(field) === activeSms.id)
    : activeBrowser
      ? browserFields.filter((field) => browserModuleForField(field) === activeBrowser.id)
      : fields.filter((field) => (field.group || '其他') === activeKey && !channelForField(field) && !smsChannelForField(field) && !browserModuleForField(field));
  const visible = activeChannel?.id === 'general'
    ? [...unsortedVisible].sort((left, right) => (left.key === 'EMAIL_SOURCE' ? -1 : right.key === 'EMAIL_SOURCE' ? 1 : 0))
    : unsortedVisible;
  const advancedVisible = activeChannel ? visible.filter((field) => ADVANCED_EMAIL_FIELD_KEYS.has(field.key)) : [];
  const primaryVisible = activeChannel ? visible.filter((field) => !ADVANCED_EMAIL_FIELD_KEYS.has(field.key)) : visible;
  const hasRoxyFields = browserFields.some((field) => browserModuleForField(field) === 'roxy');
  const activeTitle = activeEmailGroup?.title || activeChannel?.title || activeSms?.title || activeBrowser?.title || activeKey || '配置';
  const activeDescription = activeEmailGroup?.description || activeChannel?.description || activeSms?.description || activeBrowser?.description || GROUP_META[activeKey] || '修改后点击右上角保存，配置会写入 .env 并热加载。';

  function update(key, value) { setDraft((current) => ({ ...current, [key]: value })); }

  async function save(overrides = {}) {
    setSaving(true);
    try {
      const updates = { ...draft, ...overrides };
      if (Object.hasOwn(updates, 'EMAIL_SOURCE')) {
        updates.EMAIL_SOURCE = parseSources(updates.EMAIL_SOURCE)
          .filter((source) => {
            const runtimeChannel = runtimeChannelFor(summary?.registration_email, source);
            return channelDraftMissing(source, updates, runtimeChannel).length === 0;
          })
          .join(',');
      }
      setDraft(updates);
      const response = await post('/api/config', { updates });
      notify(response.note || '配置已保存并热加载', 'success');
      setSavedAt(new Date());
      await load();
      await onSummaryRefresh?.();
    } catch (saveError) { notify(saveError.message, 'error'); }
    finally { setSaving(false); }
  }

  async function enableEmailChannel(channelId) {
    const runtimeChannel = runtimeChannelFor(summary?.registration_email, channelId);
    const missing = channelDraftMissing(channelId, draft, runtimeChannel);
    if (missing.length) {
      notify(`请先完成：${missing.join('、')}`, 'warning');
      return;
    }
    const selected = parseSources(draft.EMAIL_SOURCE);
    const sources = selected.includes(channelId) ? selected : [...selected, channelId];
    await save({ USE_EMAIL_SERVICE: true, EMAIL_SOURCE: sources.join(',') });
  }

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

  function renderField(field) {
    return field.key === 'EMAIL_SOURCE'
      ? <EmailSourcePicker key={field.key} value={draft[field.key]} draft={draft} runtime={summary?.registration_email} onChange={(value) => update(field.key, value)} />
      : <ConfigField key={field.key} field={field} value={draft[field.key]} showKey={!activeChannel} onChange={(value) => update(field.key, value)} />;
  }

  return <div className="page-stack settings-page">
    <div className="page-intro"><div><div className="eyebrow"><Settings2 size={14} /> RUNTIME SETTINGS</div><h2>设置</h2></div><div className="intro-actions"><Button icon={RefreshCw} onClick={load} loading={loading}>重新读取</Button><Button icon={Save} variant="primary" onClick={() => save()} loading={saving}>保存全部</Button></div></div>
    <InlineNotice>修改后点击“保存全部”。密码、Token 和授权码只在输入框中保留，不会在配置目录里明文展示。</InlineNotice>
    {savedAt ? <InlineNotice>最近保存：{savedAt.toLocaleTimeString('zh-CN')}</InlineNotice> : null}
    {error ? <ErrorState message={error} onRetry={load} /> : null}
    <div className="config-layout"><ConfigNavigation fields={fields} emailFields={emailFields} smsFields={smsFields} browserFields={browserFields} groups={groups} activeKey={activeKey} onSelect={(key) => { setActiveKey(key); setToolResult(''); }} /><Card className="config-editor"><SectionHeader title={activeTitle} description={activeDescription} />
      {activeEmailGroup?.id === 'temporary' ? <EmailChannelGroup
        channels={activeEmailChannels}
        emailFields={emailFields}
        draft={draft}
        runtime={summary?.registration_email}
        saving={saving}
        loading={loading}
        toolBusy={toolBusy}
        onEnable={enableEmailChannel}
        onOpenEmail={onOpenEmail}
        onOpenRegistration={onOpenGptRegistration}
        onCloudmail={cloudmail}
        onChange={update}
        toolResult={toolResult}
      /> : <>
        {activeChannel ? <ChannelGuide channel={activeChannel} /> : null}
        {activeSms ? <ChannelGuide channel={activeSms} /> : null}
        {activeBrowser ? <ChannelGuide channel={activeBrowser} /> : null}
        {activeChannel?.id === 'general' ? <EmailModeStatus runtime={summary?.registration_email} enabled={Boolean(draft.USE_EMAIL_SERVICE)} onOpenRegistration={onOpenGptRegistration} /> : null}
        {activeChannel && activeChannel.id !== 'general' ? <EmailChannelAction channel={activeChannel} draft={draft} runtime={summary?.registration_email} saving={saving} onEnable={() => enableEmailChannel(activeChannel.id)} onOpenGeneral={() => setActiveKey('email:general')} onOpenEmail={onOpenEmail} onOpenRegistration={onOpenGptRegistration} /> : null}
        {activeChannel?.id === 'cloudmail' ? <CloudmailTool toolBusy={toolBusy} onRun={cloudmail} /> : null}
        {activeBrowser?.id === 'roxy' && hasRoxyFields ? <RoxyTool draft={draft} toolBusy={toolBusy} roxyItems={roxyItems} roxySelection={roxySelection} onLoad={loadRoxy} onSelect={setRoxySelection} onSave={saveRoxy} /> : null}
        {toolResult ? <div className="config-tool-result">{toolResult}</div> : null}
        {loading ? <div className="form-loading"><span /><span /><span /></div> : <div className="config-fields">{primaryVisible.map(renderField)}{advancedVisible.length ? <details className="config-advanced-fields"><summary><span>高级设置</span><small>一般不需要修改</small></summary><div className="config-advanced-grid">{advancedVisible.map(renderField)}</div></details> : null}{!visible.length && activeChannel?.id !== 'generic_api' ? <div className="empty-state"><WandSparkles size={20} /><strong>暂无可编辑字段</strong><span>{activeBrowser ? '该浏览器模块暂时没有可编辑字段。' : '该渠道通过“邮箱”页面或外部服务管理。'}</span></div> : null}</div>}
      </>}
    </Card></div>
  </div>;
}

function EmailChannelGroup({ channels, emailFields, draft, runtime, saving, loading, toolBusy, onEnable, onOpenEmail, onOpenRegistration, onCloudmail, onChange, toolResult }) {
  return <div className="config-email-channel-group">
    {channels.map((channel) => {
      const channelFields = emailFields.filter((field) => channelForField(field) === channel.id);
      const primaryFields = channelFields.filter((field) => !ADVANCED_EMAIL_FIELD_KEYS.has(field.key));
      const advancedFields = channelFields.filter((field) => ADVANCED_EMAIL_FIELD_KEYS.has(field.key));
      return <section className="config-email-channel-card" key={channel.id}>
        <div className="config-email-channel-card-heading"><strong>{channel.label}</strong><small>{channel.navHint || channel.description}</small></div>
        <ChannelGuide channel={channel} />
        <EmailChannelAction channel={channel} draft={draft} runtime={runtime} saving={saving} onEnable={() => onEnable(channel.id)} onOpenEmail={onOpenEmail} onOpenRegistration={onOpenRegistration} />
        {channel.id === 'cloudmail' ? <CloudmailTool toolBusy={toolBusy} onRun={onCloudmail} /> : null}
        {loading ? <div className="form-loading"><span /><span /><span /></div> : <div className="config-fields">{primaryFields.map((field) => <ConfigField key={field.key} field={field} value={draft[field.key]} showKey={false} onChange={(value) => onChange(field.key, value)} />)}{advancedFields.length ? <details className="config-advanced-fields"><summary><span>高级设置</span><small>一般不需要修改</small></summary><div className="config-advanced-grid">{advancedFields.map((field) => <ConfigField key={field.key} field={field} value={draft[field.key]} showKey={false} onChange={(value) => onChange(field.key, value)} />)}</div></details> : null}{!channelFields.length ? <div className="empty-state"><WandSparkles size={20} /><strong>暂无可编辑字段</strong><span>该渠道通过外部服务管理。</span></div> : null}</div>}
      </section>;
    })}
    {toolResult ? <div className="config-tool-result">{toolResult}</div> : null}
  </div>;
}

function ConfigNavigation({ fields, emailFields, smsFields, browserFields, groups, activeKey, onSelect }) {
  const navRef = useRef(null);
  const knownGroups = new Set(SETTINGS_SECTIONS.flatMap((section) => section.groups));
  const extraGroups = groups.filter((group) => !knownGroups.has(group));
  useEffect(() => {
    const revealActiveItem = () => {
      const activeItem = navRef.current?.querySelector('.config-nav-item.is-active:not(.config-nav-parent)');
      activeItem?.scrollIntoView({ block: 'nearest', inline: 'center' });
    };
    revealActiveItem();
    window.addEventListener('resize', revealActiveItem);
    return () => window.removeEventListener('resize', revealActiveItem);
  }, [activeKey, fields]);
  const renderGroup = (group) => { const Icon = groupIcon(group); const count = fields.filter((field) => !channelForField(field) && !smsChannelForField(field) && !browserModuleForField(field) && (field.group || '其他') === group).length; return <button className={`config-nav-item ${activeKey === group ? 'is-active' : ''}`} key={group} title={GROUP_META[group] || group} onClick={() => onSelect(group)}><span className="config-nav-icon"><Icon size={15} /></span><span><strong>{group}</strong><small>{GROUP_META[group] || '运行参数'}</small></span><small>{count}</small></button>; };
  const renderBrowserNavigation = () => <><button className={`config-nav-item config-nav-parent ${activeKey.startsWith('browser:') ? 'is-active' : ''}`} title="代理浏览器总入口：先选择驱动，再进入对应浏览器模块填写参数" onClick={() => onSelect('browser:general')}><span className="config-nav-icon"><Globe2 size={15} /></span><span><strong>代理浏览器</strong><small>驱动、会话和指纹</small></span><small>{browserFields.length}</small></button><div className="config-browser-list">{BROWSER_MODULES.map((module) => { const Icon = module.icon; const count = fields.filter((field) => browserModuleForField(field) === module.id).length; return <button key={module.id} className={`config-nav-item config-browser-nav-item ${activeKey === `browser:${module.id}` ? 'is-active' : ''}`} title={module.tooltip} data-tooltip={module.tooltip} onClick={() => onSelect(`browser:${module.id}`)}><span className="config-nav-icon"><Icon size={14} /></span><span><strong>{module.label}</strong><small>{module.setup}</small></span><small>{count || '—'}</small></button>; })}</div></>;
  return <Card className="config-nav" ref={navRef}><div className="config-nav-heading"><ShieldCheck size={16} /><strong>配置目录</strong></div><div className="config-nav-label">邮箱与验证码</div><button className={`config-nav-item config-nav-parent ${activeKey.startsWith('email:') ? 'is-active' : ''}`} title="先准备邮箱渠道，再设置默认使用顺序" onClick={() => onSelect('email:general')}><span className="config-nav-icon"><Mail size={15} /></span><span><strong>邮箱与 OTP</strong><small>邮箱来源、收码与顺序</small></span><small>{emailFields.length}</small></button><div className="config-channel-list">{EMAIL_SETTING_GROUPS.map((group) => { const Icon = group.icon; const count = group.channelIds.reduce((total, channelId) => total + fields.filter((field) => channelForField(field) === channelId).length, 0); return <button key={group.id} className={`config-nav-item config-channel-nav-item ${activeKey === `email:${group.id}` ? 'is-active' : ''}`} title={group.tooltip} data-tooltip={group.tooltip} onClick={() => onSelect(`email:${group.id}`)}><span className="config-nav-icon"><Icon size={14} /></span><span><strong>{group.label}</strong><small>{group.navHint || group.description}</small></span><small>{count || '—'}</small></button>; })}</div><div className="config-nav-section"><div className="config-nav-divider" /><div className="config-nav-label">手机接码</div><button className={`config-nav-item config-nav-parent ${activeKey.startsWith('sms:') ? 'is-active' : ''}`} title="配置接码平台，并决定是否加入手机号池" onClick={() => onSelect('sms:general')}><span className="config-nav-icon"><Smartphone size={15} /></span><span><strong>接码平台</strong><small>平台、开关和动态取号</small></span><small>{smsFields.length}</small></button><div className="config-channel-list">{SMS_CHANNELS.map((channel) => { const Icon = channel.icon; const count = fields.filter((field) => smsChannelForField(field) === channel.id).length; return <button key={channel.id} className={`config-nav-item config-channel-nav-item ${activeKey === `sms:${channel.id}` ? 'is-active' : ''}`} title={channel.tooltip} data-tooltip={channel.tooltip} onClick={() => onSelect(`sms:${channel.id}`)}><span className="config-nav-icon"><Icon size={14} /></span><span><strong>{channel.label}</strong><small>{channel.navHint || channel.description}</small></span><small>{count || '—'}</small></button>; })}</div></div>{SETTINGS_SECTIONS.map((section) => <div className="config-nav-section" key={section.label}><div className="config-nav-divider" /><div className="config-nav-label">{section.label}</div>{section.label === '浏览器与网络' ? <><div className="config-browser-tree">{renderBrowserNavigation()}</div>{section.groups.filter((group) => groups.includes(group)).map(renderGroup)}</> : section.groups.filter((group) => groups.includes(group)).map(renderGroup)}</div>)}{extraGroups.length ? <div className="config-nav-section"><div className="config-nav-divider" /><div className="config-nav-label">其他设置</div>{extraGroups.map(renderGroup)}</div> : null}</Card>;
}

function ChannelGuide({ channel }) {
  const Icon = channel.icon;
  const hasEmailGuide = Boolean(channel.acquisition);
  return <div className={`config-channel-guide ${hasEmailGuide ? 'is-detailed' : ''}`}><div className="config-channel-guide-icon"><Icon size={18} /></div><div className="config-channel-guide-copy"><div className="config-guide-heading"><strong>{hasEmailGuide ? '邮箱从哪里获得' : '如何配置'}</strong>{channel.website ? <a href={channel.website.href} target="_blank" rel="noreferrer">{channel.website.label}</a> : null}</div><p>{channel.acquisition || channel.setup}</p>{channel.steps?.length ? <ol>{channel.steps.map((step) => <li key={step}>{step}</li>)}</ol> : null}</div><span className="config-help-tip" title={channel.tooltip} aria-label={channel.tooltip}><Info size={15} /></span></div>;
}

function EmailModeStatus({ enabled, runtime, onOpenRegistration }) {
  const savedModeMatches = Boolean(runtime?.automatic) === enabled;
  const ready = savedModeMatches && Boolean(runtime?.ready);
  const readyCount = Array.isArray(runtime?.usable_sources) ? runtime.usable_sources.length : 0;
  const detail = enabled
    ? ready
      ? `${readyCount} 个已启用渠道当前可用；注册时按优先顺序自动兜底。`
      : '至少完成一个渠道的配置并保存后，才能启动自动注册。'
    : ready
      ? '手动邮箱已配置；任务等待验证码时需要在 GPT账号页面提交。'
      : '请填写手动注册邮箱并保存；此模式不会调用邮箱供应商。';
  return <div className={`config-mode-status ${enabled ? 'is-auto' : 'is-manual'}`}><span className="config-mode-dot" /><div><strong>{enabled ? '当前：自动取邮箱并自动收 OTP' : '当前：手动邮箱和手动 OTP'}</strong><small>{detail}</small></div><Button icon={MailPlus} size="sm" variant="primary" disabled={!ready} onClick={onOpenRegistration}>去注册新账号</Button></div>;
}

function channelDraftMissing(channelId, draft, runtimeChannel) {
  const text = (key) => String(draft[key] || '').trim();
  if (channelId === 'outlook' || channelId === 'generic_api') return runtimeChannel?.missing || [];
  if (channelId === 'cloudflare_domain') return [
    !text('EMAIL_DOMAIN') && '转发域名',
    !text('QQ_EMAIL') && 'QQ 邮箱地址',
    !text('QQ_IMAP_PASSWORD') && 'QQ 邮箱 IMAP 授权码',
  ].filter(Boolean);
  if (channelId === 'cloudflare') {
    const authMode = text('CLOUDFLARE_AUTH_MODE').toLowerCase() || 'none';
    const accountPath = text('CLOUDFLARE_PATH_ACCOUNTS').toLowerCase() || '/api/new_address';
    const needsKey = ['x-admin-auth', 'bearer', 'x-api-key', 'query-key'].includes(authMode) || accountPath.replace(/\/$/, '').endsWith('/admin/new_address');
    return [!text('CLOUDFLARE_API_BASE') && 'Cloudflare API 地址', needsKey && !text('CLOUDFLARE_API_KEY') && 'Cloudflare API Key'].filter(Boolean);
  }
  if (channelId === 'gptmail') return [!text('GPTMAIL_API_KEY') && 'GPTMail API Key'].filter(Boolean);
  if (channelId === 'mailnest') return [!text('MAIL_NEST_API_KEY') && 'MailNest API Key', !text('MAIL_NEST_PROJECT_CODE') && 'MailNest 项目代码'].filter(Boolean);
  if (channelId === 'cloudmail') return [!text('CLOUDMAIL_API_BASE') && 'CloudMail API 地址', !text('CLOUDMAIL_AUTH_TOKEN') && 'CloudMail Token'].filter(Boolean);
  return [];
}

function runtimeChannelFor(runtime, channelId) {
  const allChannels = Array.isArray(runtime?.all_channels) && runtime.all_channels.length
    ? runtime.all_channels
    : (runtime?.channels || []);
  return allChannels.find((item) => item.id === channelId);
}

function EmailChannelAction({ channel, draft, runtime, saving, onEnable, onOpenEmail, onOpenRegistration }) {
  const selectedInDraft = parseSources(draft.EMAIL_SOURCE).includes(channel.id);
  const enabledInDraft = Boolean(draft.USE_EMAIL_SERVICE) && selectedInDraft;
  const runtimeChannel = runtimeChannelFor(runtime, channel.id);
  const enabledAtRuntime = Boolean(runtime?.automatic) && (runtime?.sources || []).includes(channel.id);
  const ready = enabledInDraft && enabledAtRuntime && Boolean(runtimeChannel?.ready);
  const missing = channelDraftMissing(channel.id, draft, runtimeChannel);
  const needsImport = channel.id === 'outlook' || channel.id === 'generic_api';

  let title = `${channel.label} 尚未启用`;
  let detail = missing.length
    ? needsImport ? `请先导入${channel.label}邮箱素材。` : `先填写：${missing.join('、')}。`
    : '保存并启用后，此渠道会加入注册邮箱的兜底顺序。';
  if (ready) {
    title = `${channel.label} 已就绪，可以开始注册`;
    detail = runtimeChannel.message;
  } else if (enabledInDraft && missing.length) {
    title = `${channel.label} 尚未就绪`;
    detail = needsImport ? runtimeChannel?.message || `请先导入${channel.label}邮箱素材。` : `还需填写：${missing.join('、')}。`;
  } else if (enabledInDraft) {
    title = `${channel.label} 设置待保存`;
    detail = '当前信息已经填写完整，保存后即可在注册窗口中选择。';
  }

  return <div className={`config-channel-action ${ready ? 'is-ready' : 'is-warning'}`}><div className="config-channel-action-copy"><strong>{title}</strong><p>{detail}</p></div><div className="config-tool-actions">{needsImport ? <Button icon={Upload} onClick={onOpenEmail}>导入邮箱</Button> : null}{!ready && (!enabledInDraft || !missing.length) ? <Button icon={Settings2} loading={saving} disabled={!needsImport && missing.length > 0} onClick={onEnable}>{enabledInDraft ? '保存并检查' : '保存并启用'}</Button> : null}{ready ? <Button icon={MailPlus} variant="primary" onClick={onOpenRegistration}>去注册新账号</Button> : null}</div></div>;
}

function RoxyTool({ toolBusy, roxyItems, roxySelection, onLoad, onSelect, onSave }) {
  return <div className="config-tool"><div><strong>Roxy 团队 / 项目</strong><p>从本地 Roxy API 读取工作区并同步 ID。</p></div><div className="config-tool-actions"><Button icon={Cloud} loading={toolBusy === 'roxy'} onClick={onLoad}>获取团队</Button>{roxyItems.length ? <Select value={roxySelection} onChange={onSelect} options={[{ value: '', label: '选择团队 / 项目' }, ...roxyItems.map((item) => ({ value: `${item.id}::${item.projectId || ''}`, label: item.label || `${item.name || item.id} · ${item.projectId || '无项目'}` }))]} /> : null}<Button variant="primary" disabled={!roxySelection} loading={toolBusy === 'roxy-save'} onClick={onSave}>保存选择</Button></div></div>;
}

function CloudmailTool({ toolBusy, onRun }) {
  return <div className="config-tool"><div><strong>CloudMail 快捷操作</strong><p>可用当前表单中的地址和凭证生成 Token，或读取并缓存域名。</p></div><div className="config-tool-actions"><Button icon={KeyRound} loading={toolBusy === 'cloudmail-token'} onClick={() => onRun('cloudmail-token')}>生成 Token</Button><Button icon={Database} loading={toolBusy === 'cloudmail-domains'} onClick={() => onRun('cloudmail-domains')}>获取域名</Button></div></div>;
}

function EmailSourcePicker({ value, onChange, draft, runtime }) {
  const readiness = Object.fromEntries(EMAIL_CHANNELS.filter((item) => item.id !== 'general').map((channel) => {
    const runtimeChannel = runtimeChannelFor(runtime, channel.id);
    const missing = channelDraftMissing(channel.id, draft, runtimeChannel);
    return [channel.id, { ready: missing.length === 0, missing, runtimeChannel }];
  }));
  const selected = parseSources(value).filter((id) => readiness[id]?.ready);
  function toggle(id) {
    if (!readiness[id]?.ready) return;
    const next = selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id];
    onChange(sourceText(next.join(',')));
  }
  function move(id, direction) { const index = selected.indexOf(id); const nextIndex = index + direction; if (index < 0 || nextIndex < 0 || nextIndex >= selected.length) return; const next = [...selected]; [next[index], next[nextIndex]] = [next[nextIndex], next[index]]; onChange(next.join(',')); }
  return <div className="config-field config-source-field"><span className="config-field-copy"><strong>默认启用邮箱渠道</strong><small>未完成配置的渠道不能勾选。这里保存默认选择；每次注册时仍可临时调整。</small></span><div className="source-picker-options">{EMAIL_CHANNELS.filter((item) => item.id !== 'general').map((channel) => { const state = readiness[channel.id]; const selectedNow = selected.includes(channel.id); const message = state.ready ? (state.runtimeChannel?.message || '配置完整，可以使用') : `不可用：${state.missing.join('、')}`; return <label key={channel.id} className={`source-option ${selectedNow ? 'is-selected' : ''} ${state.ready ? '' : 'is-disabled'}`} title={state.ready ? channel.tooltip : message}><input type="checkbox" checked={selectedNow} disabled={!state.ready} onChange={() => toggle(channel.id)} /><span><strong>{channel.label}{channel.id === 'outlook' || channel.id === 'generic_api' ? <em>导入邮箱</em> : null}</strong><small>{message}</small></span></label>; })}</div><div className="source-order"><div className="source-order-heading"><strong>默认优先顺序</strong><span>{selected.length ? '数字越靠前越优先' : '尚未选择渠道'}</span></div>{selected.length ? selected.map((id, index) => { const channel = EMAIL_CHANNELS.find((item) => item.id === id); return <div className="source-order-row" key={id}><span className="source-order-number">{index + 1}</span><span>{channel?.label || id}</span><span className="source-order-actions"><IconButton label="上移" icon={ArrowUp} size={14} disabled={index === 0} onClick={() => move(id, -1)} /><IconButton label="下移" icon={ArrowDown} size={14} disabled={index === selected.length - 1} onClick={() => move(id, 1)} /></span></div>; }) : <span className="source-order-empty">请先完成至少一个渠道的配置，然后在上方勾选。</span>}</div></div>;
}

function ConfigField({ field, value, onChange, showKey = true }) {
  const type = String(field.type || 'str').toLowerCase(); const secret = Boolean(field.secret); const label = field.label || field.key; const help = field.help || '';
  const copy = <span className="config-field-copy"><strong>{label}</strong><small>{help}</small>{showKey ? <code>{field.key}</code> : null}</span>;
  if (type === 'bool' || type === 'boolean') return <label className="config-field config-switch" title={help}>{copy}<input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} /><span className="switch-track" /></label>;
  if (field.key === 'REGISTRATION_DRIVER') return <label className="config-field" title={help}>{copy}<select value={value || 'protocol'} onChange={(event) => onChange(event.target.value)}><option value="chrome_cdp">系统 Chrome（本机推荐，邮箱 OTP 优先）</option><option value="roxy">RoxyBrowser（邮箱 OTP 优先）</option><option value="cloak">CloakBrowser（邮箱 OTP 优先）</option><option value="browser_use">Browser Use Cloud（邮箱 OTP 优先）</option><option value="skyvern">Skyvern（邮箱 OTP 优先）</option><option value="protocol">纯协议（仅邮箱 OTP）</option></select></label>;
  if (field.key === 'SMS_PROVIDER') return <label className="config-field" title={help}>{copy}<select value={value || 'l'} onChange={(event) => onChange(event.target.value)}><option value="grizzly">GrizzlySMS</option><option value="l">L 接码服务</option><option value="h">H 接码服务</option></select></label>;
  if (type === 'list_str_multiline' || type === 'list' || String(value || '').includes('\n')) return <label className="config-field" title={help}>{copy}<textarea rows="5" value={value ?? ''} onChange={(event) => onChange(event.target.value)} /></label>;
  if (type === 'int' || type === 'float' || type === 'number') return <label className="config-field" title={help}>{copy}<input type="number" step={type === 'float' ? '0.1' : '1'} value={value ?? ''} onChange={(event) => onChange(event.target.value)} /></label>;
  return <label className="config-field" title={help}>{copy}<input type={secret ? 'password' : 'text'} value={value ?? ''} onChange={(event) => onChange(event.target.value)} autoComplete="off" /></label>;
}
