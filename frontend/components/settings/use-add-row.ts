"use client";
// The state of the "add by email" row, free of any look: the text, its error, the suggestion list and the pending add. Shared by the
// General tab (design-system parts) and the V2 Members dialog (Graphico parts); both render it, neither repeats it.
import { useState } from "react";
import { useEmailSuggestions } from "@/components/members/use-email-suggestions";
import type { Suggestion } from "@/lib/domain/members";

export interface AddOutcome {
  ok: boolean;
  error?: string | null;
}

export function useAddRow(projectId: string, onAdd: (text: string) => Promise<AddOutcome>) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | undefined>(undefined);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [busy, setBusy] = useState(false);
  const { items } = useEmailSuggestions(text, projectId);

  const add = () => {
    if (busy) return;
    const pending = onAdd(text);
    setBusy(true);
    setOpen(false);
    void pending.then((result) => {
      setBusy(false);
      setError(result.ok ? undefined : (result.error ?? undefined)); // a success remounts the row (new key), which empties the field
    });
  };
  const pick = (s: Suggestion) => {
    setText(s.email);
    setError(undefined);
    setOpen(false);
  };
  const type = (value: string) => {
    setText(value);
    setError(undefined);
  };
  return { text, error, items, open, active, busy, add, pick, type, setOpen, setActive };
}
