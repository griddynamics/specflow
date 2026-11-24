import type { ComponentPropsWithoutRef, JSX, ReactNode } from "react";
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

/** A label, one control (TextInput, TextArea, Select or your own), an optional hint and an optional error. */
export function Field({ label, htmlFor, hint, error, children }: Readonly<FieldProps>): JSX.Element {
  return (
    <div className="gx-field">
      <label className="gx-field__label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint === undefined ? null : <p className="gx-field__hint">{hint}</p>}
      {error === undefined ? null : (
        <p className="gx-field__error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function TextInput({ className, ...rest }: Readonly<ComponentPropsWithoutRef<"input">>): JSX.Element {
  return <input {...rest} className={cx("gx-input", className)} />;
}

export function TextArea({ className, ...rest }: Readonly<ComponentPropsWithoutRef<"textarea">>): JSX.Element {
  return <textarea {...rest} className={cx("gx-input", className)} />;
}

export function Select({ className, ...rest }: Readonly<ComponentPropsWithoutRef<"select">>): JSX.Element {
  return <select {...rest} className={cx("gx-input", className)} />;
}
