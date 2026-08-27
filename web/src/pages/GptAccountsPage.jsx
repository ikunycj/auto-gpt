import { Users } from 'lucide-react';
import RelayPage from './RelayPage';

export default function GptAccountsPage({ notify, onOpenSettings, onRegistrationRequestHandled, onSummaryRefresh, registrationRequested, summary }) {
  return <div className="page-stack">
    <div className="page-intro"><div><div className="eyebrow"><Users size={14} /> GPT ACCOUNT WORKSPACE</div><h2>GPT账号</h2></div></div>
    <RelayPage notify={notify} summary={summary} mode="accounts" embedded onOpenSettings={onOpenSettings} onSummaryRefresh={onSummaryRefresh} registrationRequested={registrationRequested} onRegistrationRequestHandled={onRegistrationRequestHandled} />
  </div>;
}
