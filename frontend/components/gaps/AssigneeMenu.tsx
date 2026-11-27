"use client";
// FR-GWEB-06 (docs/graph/gap/answers.md "Who answers"): the assignee of a gap in its inbox row, changed in place. A click on the cell
// opens a menu of Unassigned and the project's members (the caller marked "(you)", the current choice checked); choosing one assigns the
// gap (`PUT .../assignee`) and the lists read again. The card has no assignee control. A resolved gap keeps its assignee as history:
// its cell shows it without a menu. The menu sits beside the row's button (DecisionRow `aside`), so a click on it never opens the card.
import { useState } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Icon } from "@/components/shared/ui/Icon";
import { Menu, type MenuItem } from "@/components/ui/Menu";
import type { Gap } from "@/lib/domain/gaps";
import type { Member } from "@/lib/domain/members";
import { ASSIGNED_TO_NOBODY, assigneeChoices, assigneeText } from "@/components/shared/gaps/gaps-logic";
import "./gaps.css";

/** What the rows need to assign: the project's members, and the assignment (it rejects when the assignment was not saved). */
export interface AssignControl {
  people: readonly Member[];
  assign: (gap: Gap, userId: string | null) => Promise<void>;
}

function Who({ name }: Readonly<{ name: string | null }>) {
  if (name === null) return <span className="gdg-muted">Unassigned</span>;
  return (
    <>
      <Avatar name={name} size="sm" title={false} />
      <span className="gaps-assignee__name">{name}</span>
    </>
  );
}

export function AssigneeMenu({ gap, control }: Readonly<{ gap: Gap; control: AssignControl }>) {
  const [busy, setBusy] = useState(false);
  const name = gap.assignedTo === null ? null : assigneeText(gap.assignedTo, control.people);
  if (gap.status === "resolved") {
    return (
      <span className="gaps-assignee gaps-assignee--fixed" data-testid="gap-assignee">
        <Who name={name} />
      </span>
    );
  }
  const current = gap.assignedTo ?? ASSIGNED_TO_NOBODY;
  const choose = async (value: string) => {
    if (busy || value === current) return;
    setBusy(true);
    try {
      await control.assign(gap, value === ASSIGNED_TO_NOBODY ? null : value);
    } catch {
      // the screen says the assignment was not saved; the cell keeps the assignee the list read
    } finally {
      setBusy(false);
    }
  };
  const items: MenuItem[] = assigneeChoices(control.people, gap.assignedTo).map((choice) => ({
    label: choice.label,
    ...(choice.value === ASSIGNED_TO_NOBODY
      ? { icon: "user" as const }
      : { avatar: { name: assigneeText(choice.value, control.people), title: false } }),
    checked: choice.value === current,
    onSelect: () => void choose(choice.value),
  }));
  return (
    <Menu
      label="Assign to"
      align="end"
      className="gaps-assignee"
      items={items}
      trigger={({ open, toggle, id }) => (
        <button
          type="button"
          className="gaps-assignee__trigger"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={id}
          aria-label={`Assigned: ${name ?? "Unassigned"}. Change the assignee`}
          aria-busy={busy}
          disabled={busy}
          onClick={toggle}
          data-testid="gap-assignee"
        >
          <Who name={name} />
          <Icon name="caret-down" size={14} className="gdg-menu__caret" />
        </button>
      )}
    />
  );
}
