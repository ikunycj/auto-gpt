import { ActionButton } from './ui';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Check, ChevronDown, Globe2, Settings2 } from 'lucide-react';

const DRIVERS = [
  { value: 'chrome_cdp', label: '系统 Chrome' },
  { value: 'roxy', label: 'RoxyBrowser' },
  { value: 'cloak', label: 'CloakBrowser' },
  { value: 'protocol', label: '纯协议' },
];

export default function WorkspaceBrowser({ value, loading, saving, error, settingsDirty, onChange, onOpenSettings }) {
  const selected = DRIVERS.find((driver) => driver.value === value);
  const disabled = loading || saving || settingsDirty || !selected;
  const status = saving ? '正在保存…' : settingsDirty ? '先保存或撤销设置页的修改，再切换驱动。' : error;
  return <div className="workspace-browser">
    <DropdownMenu.Root modal={false}>
      <DropdownMenu.Trigger asChild>
        <ActionButton type="button" className="workspace-browser-control" aria-label="选择浏览器驱动" disabled={disabled}>
          <span className="nav-icon"><Globe2 size={17} /></span>
          <span className="workspace-browser-copy"><strong>浏览器</strong><small>{loading ? '读取注册驱动…' : selected?.label || '尚未读取'}</small></span>
          <ChevronDown size={15} className="workspace-browser-chevron" />
        </ActionButton>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="workspace-browser-menu" side="top" align="start" sideOffset={8} collisionPadding={12} aria-label="浏览器驱动">
          <DropdownMenu.RadioGroup value={value || ''} onValueChange={onChange}>
            {DRIVERS.map((driver) => <DropdownMenu.RadioItem key={driver.value} value={driver.value} className="workspace-browser-option" disabled={disabled}>
              <span>{driver.label}</span>
              <DropdownMenu.ItemIndicator className="workspace-browser-indicator"><Check size={15} /></DropdownMenu.ItemIndicator>
            </DropdownMenu.RadioItem>)}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
    <p className="workspace-browser-status" role="status" aria-live="polite">{status || ''}</p>
    <ActionButton type="button" className="workspace-browser-settings" onClick={onOpenSettings}><Settings2 size={13} />浏览器参数设置</ActionButton>
  </div>;
}
