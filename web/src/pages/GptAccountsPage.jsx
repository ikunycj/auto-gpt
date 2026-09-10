import RelayPage from './RelayPage';

export default function GptAccountsPage({ notify, onOpenSettings, onOpenEmail, onRegistrationRequestHandled, onSummaryRefresh, registrationRequested, summary }) {
  return <RelayPage notify={notify} summary={summary} mode="accounts" embedded onOpenSettings={onOpenSettings} onOpenEmail={onOpenEmail} onSummaryRefresh={onSummaryRefresh} registrationRequested={registrationRequested} onRegistrationRequestHandled={onRegistrationRequestHandled} />;
}
