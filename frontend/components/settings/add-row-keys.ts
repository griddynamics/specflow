// What a key does in the "add by email" row: the keyboard of the combobox, shared by the General tab and the V2 Members dialog. The rule
// (which key means what) is `addRowKey` in settings-logic.ts; this applies it to the row's handlers.
import type { KeyboardEvent } from "react";
import type { Suggestion } from "@/lib/domain/members";
import { addRowKey } from "./settings-logic";

export interface AddRowKeys {
  items: readonly Suggestion[];
  /** The index of the highlighted suggestion. */
  active: number;
  /** The list is wanted open (typing or focus). */
  open: boolean;
  busy: boolean;
  onOpen: (open: boolean) => void;
  onActive: (index: number) => void;
  onPick: (s: Suggestion) => void;
  onAdd: () => void;
}

/** The suggestion list shows when it is wanted and has items. */
export const showsList = (k: Pick<AddRowKeys, "open" | "items">): boolean => k.open && k.items.length > 0;

/** The highlighted index kept inside the list. */
export const activeOf = (k: Pick<AddRowKeys, "active" | "items">): number => Math.min(k.active, Math.max(k.items.length - 1, 0));

/** Enter picks the active suggestion while the list is open, otherwise it adds. Escape closes only the list. */
export function handleAddRowKey(e: KeyboardEvent<HTMLInputElement>, k: AddRowKeys): void {
  const act = addRowKey(e.key, { active: activeOf(k), count: k.items.length, showList: showsList(k), busy: k.busy });
  if (act === null) return;
  if (act.kind === "close") {
    e.preventDefault(); // a native <dialog> closes on Escape's default action
    e.nativeEvent.stopImmediatePropagation(); // closes the list only, whatever else listens for Esc on the document
    k.onOpen(false);
    return;
  }
  e.preventDefault();
  if (act.kind === "move") {
    if (act.open) k.onOpen(true);
    k.onActive(act.index);
  } else if (act.kind === "pick") {
    const picked = k.items.at(act.index);
    if (picked !== undefined) k.onPick(picked);
  } else k.onAdd();
}
