"use client";
// FE-DEMO-02.AC1: the ONE helper for controls that are not available yet.
//   const notAvailable = useNotAvailable();
//   <Button onClick={notAvailable}>…</Button>            // always shows the toast "Not available yet" (arguments ignored)
//   notAvailable.from(err)                                // toasts only when `err` is a DataError "not_available" (a port method
//                                                         // behind such a control, TECH-SPECS §3.2); returns whether it did
import { useMemo } from "react";
import { useToast } from "@/components/ui/Toast";
import { isDataError } from "@/lib/domain/errors";

export const NOT_AVAILABLE_TITLE = "Not available yet";

export function isNotAvailable(err: unknown): boolean {
  return isDataError(err) && err.code === "not_available";
}

export type NotAvailable = (() => void) & { from(err: unknown): boolean };

export function useNotAvailable(): NotAvailable {
  const toast = useToast();
  return useMemo(() => {
    const show = () => {
      toast({ title: NOT_AVAILABLE_TITLE, tone: "info" });
    };
    const from = (err: unknown): boolean => {
      if (!isNotAvailable(err)) return false;
      show();
      return true;
    };
    return Object.assign(
      () => {
        show();
      },
      { from },
    );
  }, [toast]);
}
