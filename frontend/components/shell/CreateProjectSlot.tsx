"use client";
// FE-SHELL-02.AC4 (TECH-SPECS §1.1, §5.2): the create-project SLOT. P07 only opens and closes it; P08 renders the modal here.
// P08 (owner now): `CreateProjectSlot` renders `<CreateProjectModal>` from components/create-project/**. AppShell mounts it once,
// inside the keyed shell, so "Reset demo" closes the modal and drops its form.
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { CreateProjectModal } from "@/components/create-project/CreateProjectModal";

export interface CreateProjectSlotControl {
  isOpen: boolean;
  open: () => void;
  close: () => void;
}

const SlotContext = createContext<CreateProjectSlotControl | null>(null);

/** Holds the open flag. AppShell mounts it once; "New" calls `open()`, the modal's close/Create/Esc call `close()`. */
export function CreateProjectSlotProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [isOpen, setOpen] = useState(false);
  const value = useMemo<CreateProjectSlotControl>(
    () => ({
      isOpen,
      open: () => {
        setOpen(true);
      },
      close: () => {
        setOpen(false);
      },
    }),
    [isOpen],
  );
  return <SlotContext.Provider value={value}>{children}</SlotContext.Provider>;
}

export function useCreateProjectSlot(): CreateProjectSlotControl {
  const ctx = useContext(SlotContext);
  if (!ctx) throw new Error("CreateProjectSlotProvider is missing (render inside <AppShell>)");
  return ctx;
}

/** P08: renders the Create project modal; the hidden marker keeps `data-state` for the shell tests (p07, p08). */
export function CreateProjectSlot(props: Readonly<{ open: boolean; onClose: () => void }>) {
  // Dismissal (Esc, Tab confinement, focus return) belongs to the modal primitive that CreateProjectModal renders through.
  return (
    <>
      <div hidden data-testid="create-project-slot" data-state={props.open ? "open" : "closed"} />
      <CreateProjectModal open={props.open} onClose={props.onClose} />
    </>
  );
}
