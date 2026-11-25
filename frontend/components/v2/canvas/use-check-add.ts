"use client";
// The flow of the two link sources (Confluence link, Jira epic): a check of what is typed, then Add source. The answer of a check belongs to
// the values it was made with (`signature`): a different link is a new check, and a refusal or a result never shows under another value.
import { useState } from "react";
import type { CheckLabels } from "../connector-types";
import { CANVAS_COPY } from "./canvas-copy";

type Busy = "check" | "add" | null;

export interface CheckAdd<R> {
  /** The check of the values now typed, if there is one. */
  result: R | null;
  /** The refusal of the last call, if it was made with the values now typed. */
  problem: string | null;
  busy: Busy;
  /** The primary button's label: Check, Checking…, Add source, Adding…. */
  label: string;
  /** Check, or add once the check passed for these values. */
  run: () => void;
}

export function useCheckAdd<R>(opts: {
  signature: string;
  blank: boolean;
  labels: CheckLabels;
  check: () => Promise<R>;
  /** Gets the check that passed, so an add can use what it found (the issues picked). */
  add: (checked: R) => Promise<void>;
  onAdded: () => void;
  failureText: (e: unknown) => string;
}): CheckAdd<R> {
  const { signature, blank, labels } = opts;
  const [passed, setPassed] = useState<{ signature: string; result: R } | null>(null);
  const [refused, setRefused] = useState<{ signature: string; text: string } | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const result = passed?.signature === signature ? passed.result : null;
  const fail = (e: unknown) => {
    setBusy(null);
    setRefused({ signature, text: opts.failureText(e) });
  };
  const run = () => {
    if (busy !== null || blank) return;
    setRefused(null);
    if (result !== null) {
      setBusy("add");
      opts.add(result).then(opts.onAdded, fail);
      return;
    }
    setBusy("check");
    opts.check().then((found) => {
      setBusy(null);
      setPassed({ signature, result: found });
    }, fail);
  };
  let label = result === null ? labels.test : labels.add;
  if (busy === "check") label = labels.testing;
  if (busy === "add") label = CANVAS_COPY.confluenceAdding;
  return { result, problem: refused?.signature === signature ? refused.text : null, busy, label, run };
}
