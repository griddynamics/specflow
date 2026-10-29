"use client";
// Phase 1 Mission Control sources (DESIGN D3, D10, "Safety"): the dialogs of the sources screen. Each is a design-system Modal
// with one primary action (a modal is its own view). Access is simulated in phase 1: no stored token. The Drive modal is DriveDialog.tsx.
import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { TextField } from "@/components/ui/TextField";
import { parseSourceLink } from "@/lib/domain/logic/source-link";
import { truthy } from "@/components/ui/utils";

const GITHUB_COPY = {
  title: "Add a GitHub repository",
  description: "Specflow reads the README and the docs folder. Code stays in the repository.",
  label: "Repository link",
  placeholder: "https://github.com/org/repo",
  help: "A public repository syncs right away. A private one asks for a read-only token on its card.",
  bad: "Paste a link to a GitHub repository, for example https://github.com/org/repo.",
};

/** Paste a GitHub link; the link is parsed, never fetched. A link that is not a repository is a field error here. */
export function AddLinkDialog({ onAdd, onClose }: Readonly<{ onAdd: (url: string) => Promise<void>; onClose: () => void }>) {
  const copy = GITHUB_COPY;
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit() {
    if (busy) return;
    const trimmed = url.trim();
    if (!trimmed) {
      setError("Enter a link.");
      return;
    }
    if (!parseSourceLink({ kind: "github", url: trimmed }).ok) {
      setError(copy.bad);
      return;
    }
    setBusy(true);
    try {
      await onAdd(trimmed);
      onClose();
    } catch {
      setBusy(false);
      setError("The source could not be added. Try again.");
    }
  }
  return (
    <Modal
      title={copy.title}
      description={copy.description}
      onClose={onClose}
      footer={
        <>
          <Button variant="glass" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              void submit();
            }}
            disabled={busy}
            data-testid="mcs-add-submit"
          >
            Add source
          </Button>
        </>
      }
    >
      <div data-testid="mcs-add-dialog" data-kind="github">
        <TextField
          label={copy.label}
          type="url"
          value={url}
          placeholder={copy.placeholder}
          help={copy.help}
          error={error ?? undefined}
          onChange={(e) => {
            setUrl(e.target.value);
            if (truthy(error)) setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
        />
      </div>
    </Modal>
  );
}

/** D3: a pasted read-only token for a private repository. Phase 1 checks nothing real and keeps nothing. */
export function TokenDialog({
  target,
  onAdd,
  onClose,
}: Readonly<{ target: string; onAdd: (token: string) => Promise<void>; onClose: () => void }>) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit() {
    if (busy) return;
    if (!token.trim()) {
      setError("Paste a token.");
      return;
    }
    setBusy(true);
    try {
      await onAdd(token);
      setToken(""); // the value leaves the form as soon as it was used
      onClose();
    } catch {
      setBusy(false);
      setError("The token was not accepted. Check it and try again.");
    }
  }
  return (
    <Modal
      title="Add a GitHub token"
      description={`${target} is private. Paste a fine-grained token with read-only access to its contents.`}
      onClose={onClose}
      footer={
        <>
          <Button variant="glass" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              void submit();
            }}
            disabled={busy}
            data-testid="mcs-token-submit"
          >
            {busy ? "Checking…" : "Add token"}
          </Button>
        </>
      }
    >
      <div data-testid="mcs-token-dialog">
        <TextField
          label="Token"
          type="password"
          value={token}
          spellCheck={false}
          help="Demo: the token is checked and dropped. Nothing is stored."
          error={error ?? undefined}
          onChange={(e) => {
            setToken(e.target.value);
            if (truthy(error)) setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
        />
      </div>
    </Modal>
  );
}

/** A yes/no confirmation (Rebuild from scratch: full cost; Remove source). */
export function ConfirmDialog({
  title,
  description,
  confirmLabel,
  onConfirm,
  onClose,
  children,
  testId,
}: Readonly<{
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: () => void;
  onClose: () => void;
  children?: ReactNode;
  testId: string;
}>) {
  return (
    <Modal
      title={title}
      description={description}
      onClose={onClose}
      size="sm"
      footer={
        <>
          <Button variant="glass" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              onConfirm();
              onClose();
            }}
            data-testid={`${testId}-confirm`}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div data-testid={testId}>{children}</div>
    </Modal>
  );
}
