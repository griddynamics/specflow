// Semgrep reads this file as syntax only: the components it renders are not declared.
declare const name: string;
declare const setName: (next: string) => void;
declare const pick: () => void;
declare const query: string;
declare const order: { name: string };
declare const code: string;
declare function useId(): string;
declare function matchesSearch(haystack: string, needle: string): boolean;

// ruleid: ds-use-text-input-primitive
const input = <input value={name} onChange={setName} />;
// ok: ds-use-text-input-primitive
const file = <input type="file" onChange={pick} />;

// ruleid: ds-input-token-is-a-border
const ground = { background: 'var(--input)' };
// ok: ds-input-token-is-a-border
const border = { border: '1px solid var(--input)' };

// ruleid: ds-restated-field-default
const restated = <TextField autoComplete="off" />;
// ok: ds-restated-field-default
const username = <TextField autoComplete="username" />;

function NameField() {
  const id = useId();
  // ruleid: ds-hand-assembled-form-field
  return <label htmlFor={id}>Name</label>;
}

function CodeField() {
  // ok: ds-hand-assembled-form-field
  return <FormField label="Code" control={<TextField value={code} />} />;
}

// ruleid: ds-no-inline-search-match
const inline = order.name.toLowerCase().includes(query);
// ok: ds-no-inline-search-match
const classifier = code.toLowerCase().startsWith('x');

function matches() {
  const haystack = order.name.toLowerCase();
  // ruleid: ds-no-hoisted-search-match
  return haystack.includes(query);
}

function isTagged() {
  const label = order.name.toLowerCase();
  // ok: ds-no-hoisted-search-match
  return label.includes('new');
}

// ok: ds-no-hoisted-search-match
const shared = matchesSearch(order.name, query);

// ruleid: ds-use-text-input-primitive, ds-no-hand-rolled-search-input
const search = <input type="search" />;

// ruleid: ds-no-duplicate-search-component
function OrdersSearchBar() {
  return null;
}
// ok: ds-no-duplicate-search-component
function OrdersList() {
  return null;
}

// ruleid: ds-search-input-outside-filter-toolbar
const loose = <SearchInput value={query} />;
// ok: ds-search-input-outside-filter-toolbar, ds-no-hand-rolled-search-input
const slotted = <FilterToolbar search={<SearchInput value={query} />} />;

export {
  input,
  file,
  ground,
  border,
  restated,
  username,
  NameField,
  CodeField,
  inline,
  classifier,
  matches,
  isTagged,
  shared,
  search,
  OrdersSearchBar,
  OrdersList,
  loose,
  slotted,
};
