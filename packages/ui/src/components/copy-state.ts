/** How long a copy button says "Copied" before it goes back to Copy. */
export const COPIED_MS = 2000;

export interface Copier {
  /** Writes the text; on success `setCopied(true)` now and `setCopied(false)` after `COPIED_MS`. A refused write leaves it false. */
  copy: (text: string) => Promise<void>;
  /** Drops the pending reset (the row left the screen). */
  stop: () => void;
}

export function createCopier(write: (text: string) => Promise<void>, setCopied: (copied: boolean) => void): Copier {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return {
    async copy(text) {
      clearTimeout(timer);
      try {
        await write(text);
      } catch {
        setCopied(false);
        return;
      }
      setCopied(true);
      timer = setTimeout(() => {
        setCopied(false);
      }, COPIED_MS);
    },
    stop() {
      clearTimeout(timer);
    },
  };
}
