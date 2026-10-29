"use client";
// FE-DS-02 (R5): port of `TextField` in bundle.js, with `multiline` rendering a textarea. The props are a union: with
// `multiline` the textarea attributes, ref and onChange types apply, otherwise the input ones.
import { useId, type InputHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { cx, nonEmpty, truthy } from "./utils";

interface TextFieldBase {
  label: string;
  help?: string;
  /** Error message; replaces help and turns the field danger. */
  error?: string;
  id?: string;
  className?: string;
}

export type TextFieldProps =
  | (TextFieldBase & Omit<InputHTMLAttributes<HTMLInputElement>, keyof TextFieldBase> & { multiline?: false; rows?: undefined })
  | (TextFieldBase &
      Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, keyof TextFieldBase | "rows"> & {
        multiline: true;
        /** Visible lines (default 3). */ rows?: number;
      });

export function TextField(props: TextFieldProps) {
  const { label, help, error, className, id, multiline, rows, ...rest } = props;
  const auto = useId();
  const fid = id ?? auto;
  const hid = `${fid}-h`;
  const shared = {
    id: fid,
    autoComplete: rest.autoComplete ?? "off", // a text control asks the browser for no autofill unless the caller names a real token
    "aria-invalid": truthy(error) ? true : undefined,
    "aria-describedby": truthy(error) || truthy(help) ? hid : undefined,
  };
  return (
    <div className={cx("gdg-field", truthy(error) && "gdg-field--error", className)}>
      <label className="gdg-field__label" htmlFor={fid}>
        {label}
      </label>
      {truthy(multiline) ? (
        <textarea {...(rest as TextareaHTMLAttributes<HTMLTextAreaElement>)} {...shared} rows={rows ?? 3} />
      ) : (
        <input {...(rest as InputHTMLAttributes<HTMLInputElement>)} {...shared} />
      )}
      {truthy(error) || truthy(help) ? (
        <span className="gdg-field__help" id={hid} role={truthy(error) ? "alert" : undefined}>
          {nonEmpty(error) ?? help}
        </span>
      ) : null}
    </div>
  );
}
