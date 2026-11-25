/** The part of `HTMLDialogElement` the controlled dialog uses. */
export interface DialogLike {
  open: boolean;
  showModal(): void;
  close(): void;
}

/** Makes the element match the `open` prop. Guarded on `dialog.open`, so it never throws on a second `showModal` or a second `close`. */
export function syncDialog(dialog: DialogLike, open: boolean): void {
  if (open && !dialog.open) dialog.showModal();
  if (!open && dialog.open) dialog.close();
}

/**
 * The handler of the native `close` event, the one place `onClose` is reported from (Escape and the close button both end here).
 * A close the parent asked for (`isOpen()` already false) is not reported back. `resync` runs after every close, so a parent that
 * keeps `open` true gets its dialog shown again.
 */
export function closeReporter(isOpen: () => boolean, onClose: () => void, resync: () => void): () => void {
  return () => {
    if (isOpen()) onClose();
    resync();
  };
}
