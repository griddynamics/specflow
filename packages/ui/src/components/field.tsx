import { cloneElement, isValidElement } from "react";
import type { ComponentPropsWithoutRef, JSX, ReactElement, ReactNode } from "react";
import { cx } from "../cx";

export interface FieldProps {
  label: string;
  /** The id of the control inside; the label points at it. */
  htmlFor: string;
  hint?: string;
  /** A fixed message shown under the control; announced as an alert. */
  error?: string;
  children: ReactNode;
}

interface ControlAria {
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

/** The control gets `aria-describedby` (hint and error, after any the caller set) and `aria-invalid` while there is an error. */
function withAria(child: ReactElement<ControlAria>, hintId: string | null, errorId: string | null): ReactElement<ControlAria> {
  const ids = [child.props["aria-describedby"], hintId, errorId].filter((id): id is string => id !== null && id !== undefined);
  if (ids.length === 0) return child;
  return cloneElement(child, {
    "aria-describedby": ids.join(" "),
    ...(errorId === null ? {} : { "aria-invalid": true as const }),
  });
}

/** A label, one control (TextInput, TextArea, Select or your own element), an optional hint and an optional error. The control is linked to both. */
export function Field({ label, htmlFor, hint, error, children }: Readonly<FieldProps>): JSX.Element {
  const hintId = hint === undefined ? null : `${htmlFor}-hint`;
  const errorId = error === undefined ? null : `${htmlFor}-error`;
  return (
    <div className="gx-field">
      <label className="gx-field__label" htmlFor={htmlFor}>
        {label}
      </label>
      {isValidElement<ControlAria>(children) ? withAria(children, hintId, errorId) : children}
      {hintId === null ? null : (
        <p id={hintId} className="gx-field__hint">
          {hint}
        </p>
      )}
      {errorId === null ? null : (
        <p id={errorId} className="gx-field__error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function TextInput({ className, ...rest }: Readonly<ComponentPropsWithoutRef<"input">>): JSX.Element {
  return <input autoComplete="off" {...rest} className={cx("gx-input", className)} />;
}

export function TextArea({ className, ...rest }: Readonly<ComponentPropsWithoutRef<"textarea">>): JSX.Element {
  return <textarea {...rest} className={cx("gx-input", className)} />;
}

export function Select({ className, ...rest }: Readonly<ComponentPropsWithoutRef<"select">>): JSX.Element {
  return <select {...rest} className={cx("gx-input", className)} />;
}
