import RelayPage from './RelayPage';

export default function GptAccountsPage({ notify, onOpenSettings, onOpenProxy, onOpenEmail, onRegistrationRequestHandled, onSummaryRefresh, registrationRequested, summary }) {
  return <RelayPage notify={notify} summary={summary} mode="accounts" embedded onOpenSettings={onOpenSettings} onOpenProxy={onOpenProxy} onOpenEmail={onOpenEmail} onSummaryRefresh={onSummaryRefresh} registrationRequested={registrationRequested} onRegistrationRequestHandled={onRegistrationRequestHandled} />;
}
