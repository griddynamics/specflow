"use client";
// The Jira space key of the two Jira forms (plan destination, gap sub-tasks): it cannot be edited while a call with it is in flight.
import { Field, TextInput } from "@graphico/ui";
import { CANVAS_COPY } from "./canvas-copy";

export function JiraSpaceField({
  id,
  value,
  busy,
  error,
  onChange,
}: Readonly<{ id: string; value: string; busy: boolean; error: string | undefined; onChange: (next: string) => void }>) {
  return (
    <Field label={CANVAS_COPY.jiraSpace} htmlFor={id} error={error}>
      <TextInput
        id={id}
        value={value}
        spellCheck={false}
        readOnly={busy}
        onChange={(e) => {
          onChange(e.target.value);
        }}
      />
    </Field>
  );
}
