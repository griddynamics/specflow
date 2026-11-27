"use client";
// FE-DS-02 (R5): `useControlled` from bundle.js: controlled when `value` is given, otherwise its own state.
import { useState } from "react";

/** Controlled when `value` is given, otherwise it keeps its own state seeded with `initial`. Same as bundle.js `useControlled`. */
export function useControlled<T>(value: T | undefined, initial: T, onChange?: (next: T) => void): [T, (next: T) => void] {
  const [inner, setInner] = useState<T>(initial);
  let current = inner;
  if (value !== undefined) current = value;
  return [
    current,
    (next: T) => {
      if (value === undefined) setInner(next);
      onChange?.(next);
    },
  ];
}
