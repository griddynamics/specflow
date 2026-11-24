"use client";
// The pieces an integration tab of Settings shares (Jira: jira-parts.tsx; GitHub: github-parts.tsx): the caller's own account row
// (connected with Disconnect, expired with Reconnect, or Connect) and a confirmation dialog. Plain props, no data access.
// Design-system components only: Button, GlassSurface, Icon, Modal.
import type { ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { SETTINGS_COPY } from "./settings-copy";
import "./settings.css";

const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

/** A time the API sent; a text that is not a date is shown as it is (the format would throw). */
export const whenText = (iso: string): string => (Number.isNaN(Date.parse(iso)) ? iso : WHEN.format(new Date(iso)));

export interface AccountRowProps {
  /** `connected`, `expired` or `none`: the row's look and its one button. */
  state: "connected" | "expired" | "none";
  label: string;
  title: string;
  hint: string;
  /**
   * The vendor mark: an SVG data URI in the vendor's colour, or `{ path }` (a 24x24 path drawn in the ink colour, for a near-black mark
   * that would vanish in the dark theme); none shows a plug. The expired row shows a warning icon instead.
   */
  markSrc: string | { path: string } | undefined;
  connectText: string;
  reconnectText: string;
  disconnectText: string;
  /** The hook prefix: the row and its buttons are `<prefix>-row`, `<prefix>-connect`, `<prefix>-disconnect`. */
  testId: string;
  busy: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  /** Lines under the card (a grant note, links). */
  children?: ReactNode;
}

function Mark({ expired, src }: Readonly<{ expired: boolean; src: AccountRowProps["markSrc"] }>) {
  if (expired) return <Icon name="warning-circle" size={20} />;
  if (src === undefined) return <Icon name="plug" size={20} />;
  if (typeof src === "string") return <img src={src} alt="" width={18} height={18} />;
  return (
    <svg className="settings-account__ink" viewBox="0 0 24 24" width={18} height={18} aria-hidden>
      <path fill="currentColor" d={src.path} />
    </svg>
  );
}

/** The caller's own account of one integration, as one row of the people card's look. */
export function AccountRow(p: Readonly<AccountRowProps>) {
  return (
    <section className="settings-sec" aria-label={p.label} data-testid={`${p.testId}-row`} data-state={p.state}>
      <GlassSurface className="settings-list settings-account">
        <div className="settings-row">
          <span className="gdg-avatar gdg-avatar--md gdg-avatar--neutral settings-account__mark" aria-hidden>
            <Mark expired={p.state === "expired"} src={p.markSrc} />
          </span>
          <div className="settings-who">
            <b>{p.title}</b>
            {p.hint === "" ? null : <small>{p.hint}</small>}
          </div>
          {p.state === "connected" ? (
            <Button variant="glass" size="sm" disabled={p.busy} onClick={p.onDisconnect} data-testid={`${p.testId}-disconnect`}>
              {p.disconnectText}
            </Button>
          ) : (
            <Button size="sm" disabled={p.busy} onClick={p.onConnect} data-testid={`${p.testId}-connect`}>
              {p.state === "expired" ? p.reconnectText : p.connectText}
            </Button>
          )}
        </div>
      </GlassSurface>
      {p.children}
    </section>
  );
}

export interface ConfirmDialogProps {
  title: string;
  body: string;
  confirmText: string;
  /** The hook prefix: the buttons are `<prefix>-cancel`, `<prefix>-confirm`. */
  testId: string;
  busy: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children?: ReactNode;
}

/** Confirms a destructive step. The first focus lands on the dialog, not on the destructive button. */
export function ConfirmDialog(p: Readonly<ConfirmDialogProps>) {
  return (
    <Modal
      size="sm"
      title={p.title}
      onClose={p.onClose}
      closeDisabled={p.busy}
      initialFocus="dialog"
      footer={
        <>
          <Button variant="glass" onClick={p.onClose} disabled={p.busy} data-testid={`${p.testId}-cancel`}>
            {SETTINGS_COPY.keep}
          </Button>
          <Button onClick={p.onConfirm} disabled={p.busy} data-testid={`${p.testId}-confirm`}>
            {p.confirmText}
          </Button>
        </>
      }
    >
      <p className="settings-dialog__body">{p.body}</p>
      {p.children}
    </Modal>
  );
}
