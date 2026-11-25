// The selection model of the two checklists of Add source (the issues of a Jira epic, the files of a Drive folder), free of React. The selection
// is the ids left out: empty (everything checked) is the default, so a new check starts with every row picked and a change of the selection
// never touches the check.
export type Unchecked = ReadonlySet<string>;

/** Something a list shows as one checkbox row: its id is what `exclude` carries. */
export interface Pickable {
  id: string;
}

/** The id with its row's own toggled state: a checked row leaves the set, an unchecked one joins it. */
export function toggle(unchecked: Unchecked, id: string, checked: boolean): Unchecked {
  const next = new Set(unchecked);
  if (checked) next.delete(id);
  else next.add(id);
  return next;
}

/** Select all: nothing left out. */
export const checkAll = (): Unchecked => new Set<string>();

/** Select none: every row left out. */
export const checkNone = (rows: readonly Pickable[]): Unchecked => new Set(rows.map((r) => r.id));

/** The ids to send as `exclude`, in the order of the list. */
export const excludeOf = (rows: readonly Pickable[], unchecked: Unchecked): string[] =>
  rows.filter((r) => unchecked.has(r.id)).map((r) => r.id);

/** How many rows an add would take. */
export const pickedCount = (rows: readonly Pickable[], unchecked: Unchecked): number => rows.length - excludeOf(rows, unchecked).length;
