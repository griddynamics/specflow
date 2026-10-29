declare const Modal: (props: { onClose: () => void; children?: unknown }) => null;
declare const node: HTMLElement;
declare const onKey: () => void;
declare const onClick: () => void;

// ruleid: ds-no-adhoc-modal-overlay
const backdrop = { position: 'fixed', inset: 0, zIndex: 40 };
// ruleid: ds-no-adhoc-modal-overlay
const scrim = { position: 'fixed', background: 'var(--overlay)' };
// ok: ds-no-adhoc-modal-overlay
const sticky = { position: 'sticky', zIndex: 1 };

// ruleid: ds-no-dialog-role-outside-modal
const dialog = <div role="dialog">Text</div>;
// ruleid: ds-no-dialog-role-outside-modal
const modal = <div aria-modal="true" />;
// ok: ds-no-dialog-role-outside-modal
const region = <div role="region">Text</div>;

// ruleid: ds-dismissible-surface-without-shared-dismissal
function NotesPanel({ onClose }: { onClose: () => void }) {
  return <button onClick={onClose}>Close</button>;
}

// ok: ds-dismissible-surface-without-shared-dismissal
function ConfirmBox({ onClose }: { onClose: () => void }) {
  return <Modal onClose={onClose}>Sure?</Modal>;
}

// ruleid: ds-dismissal-behaviour-written-per-surface
document.addEventListener('keydown', onKey);
// ruleid: ds-dismissal-behaviour-written-per-surface
const opener = document.activeElement;
// ok: ds-dismissal-behaviour-written-per-surface
document.addEventListener('click', onClick);

// ruleid: ds-tab-roles-outside-primitive
const tabs = <div role="tablist">Tabs</div>;
// ruleid: ds-tab-roles-outside-primitive
const tabProps = { role: 'tab', id: 'first' };
// ruleid: ds-tab-roles-outside-primitive
node.setAttribute('role', 'tabpanel');
// ok: ds-tab-roles-outside-primitive
const chips = <div role="group">Filters</div>;

export { backdrop, scrim, sticky, dialog, modal, region, NotesPanel, ConfirmBox, opener, tabs, tabProps, chips };
