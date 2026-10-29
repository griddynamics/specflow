"use client";
// FE-DS-02 (R5): port of `Toast`, `ToastProvider` and `useToast` in bundle.js. Wrap the app once, call `useToast()(...)`
// anywhere below. A toast fades after ~4 s (`duration` overrides). Placement bottom-right comes from `.gdg-toasts`.
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { GlassSurface } from "./GlassSurface";
import { Icon, type IconName } from "./Icon";
import { cx, truthy } from "./utils";

export interface ToastProps {
  title: string;
  message?: string;
  tone?: "neutral" | "success" | "info" | "danger";
  action?: ReactNode;
  onClose?: () => void;
}

export type ToastInput = ToastProps & { duration?: number };
export interface ToastProviderProps {
  children?: ReactNode;
}

const TONE_ICON: Record<NonNullable<ToastProps["tone"]>, IconName> = {
  success: "check-circle",
  danger: "x-circle",
  info: "warning-circle",
  neutral: "check",
};

// Looked up by any string: a tone outside the set shows the plain check.
const ICON_BY_TONE: Partial<Record<string, IconName>> = TONE_ICON;

export const TOAST_DURATION_MS = 4000;

export function Toast({ title, message, tone = "neutral", action, onClose }: Readonly<ToastProps>) {
  const titleId = useId();
  return (
    <GlassSurface
      variant="strong"
      floating
      className={cx("gdg-toast", `gdg-toast--${tone}`)}
      role={tone === "danger" ? "alert" : "status"}
      data-testid="toast"
    >
      <span className="gdg-toast__icon">
        <Icon name={ICON_BY_TONE[tone] ?? "check"} size={20} />
      </span>
      <div className="gdg-toast__text">
        <b id={titleId}>{title}</b>
        {truthy(message) ? <span>{message}</span> : null}
      </div>
      {action ?? null}
      {onClose ? (
        <button type="button" className="gdg-iconbtn" aria-label="Dismiss" aria-describedby={titleId} onClick={onClose}>
          <Icon name="x" size={16} />
        </button>
      ) : null}
    </GlassSurface>
  );
}

type Pending = ToastInput & { id: number };
const ToastContext = createContext<((t: ToastInput) => void) | null>(null);

export function ToastProvider({ children }: Readonly<ToastProviderProps>) {
  const [list, setList] = useState<Pending[]>([]);
  const seq = useRef(0);
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const all = timers.current;
    return () => {
      all.forEach(clearTimeout);
      all.clear();
    };
  }, []);
  const remove = useCallback((id: number) => {
    setList((l) => l.filter((x) => x.id !== id));
  }, []);
  const push = useCallback(
    (t: ToastInput) => {
      const id = ++seq.current;
      setList((l) => [...l, { ...t, id }]);
      const timer = setTimeout(() => {
        timers.current.delete(timer);
        remove(id);
      }, t.duration ?? TOAST_DURATION_MS);
      timers.current.add(timer);
    },
    [remove],
  );
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="gdg-toasts" aria-live="polite" data-testid="toasts">
        {list.map((t) => (
          <Toast
            key={t.id}
            title={t.title}
            message={t.message}
            tone={t.tone}
            action={t.action}
            onClose={() => {
              remove(t.id);
            }}
          />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/**
 * Returns the `toast(...)` function. Without a ToastProvider it reports the misuse (an error in the console, the toast as its
 * cause) and does nothing else, like bundle.js: the caller carries on.
 */
export function useToast(): (t: ToastInput) => void {
  const push = useContext(ToastContext);
  return (
    push ??
    ((t) => {
      reportError(new Error("useToast needs <ToastProvider>", { cause: t }));
    })
  );
}
