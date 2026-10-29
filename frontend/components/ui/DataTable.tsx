// The one primitive that renders a `<table>`: screens compose it instead of writing table markup, so table semantics (header
// cells, scopes, captions) stay in a single module. It renders the element as given; styling stays with the caller's class.
import type { ComponentPropsWithRef } from "react";

export function DataTable(props: ComponentPropsWithRef<"table">) {
  return <table {...props} />;
}
