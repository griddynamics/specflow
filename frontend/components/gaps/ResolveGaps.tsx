"use client";
// FR-GWEB-04 (docs/graph/gap/answers.md "Applying answers"; api.md "User interface"): "Resolve gaps", the Gaps screen's one yellow action
// in the header row, right of the tabs. Answering a gap stages its answer; "Resolve gaps" applies every staged answer of the project
// (anyone's, the AI's too) in one rebuild (`applyNow`, POST /gaps/apply), and the gaps become Resolved when it ends.
//  - useStaged: the gaps in `answered` and `applying`, every route, every page (`list`). The button names how many answers are staged
//    ("Resolve 3 gaps"); it is off with "Stage an answer first" when none is, and busy ("Resolving…") while a gap's rebuild runs, read
//    again every 5 s until it ends (then the lists read again). Off and busy use aria-disabled, so the button keeps the focus and its
//    reason (the tooltip and the accessible description) reaches the keyboard.
//  - ResolveDialog (Modal): each staged gap's title, the chosen answer (the option, or "Own answer") and who staged it, the first 50
//    and "and N more"; it says the rebuild resolves everyone's staged answers, not only yours. Confirm resolves and says the outcome in
//    a toast; `409 gap_wrong_state` means nothing waits any more; any other refusal stays in the dialog, in words.
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { Tooltip } from "@/components/ui/Tooltip";
import type { Gap, GapsPort } from "@/lib/domain/gaps";
import {
  NOTHING_STAGED_TOAST,
  answeredByText,
  failureText,
  isNothingStaged,
  resolveView,
  resolvedToast,
  stagedChoiceText,
  type ResolveView,
} from "./gaps-logic";

const PAGE = 200;
const POLL_MS = 5_000;
const LISTED = 50;

export interface Staged {
  /** The gaps whose answer is staged (`answered`), in the server's order. */
  gaps: Gap[];
  view: ResolveView;
}

/** Every gap in `answered` or `applying`, page by page. */
async function readStaged(gaps: GapsPort, projectId: string): Promise<Gap[]> {
  const all: Gap[] = [];
  let cursor: string | null = null;
  do {
    const page = await gaps.list(projectId, { status: ["answered", "applying"], limit: PAGE, ...(cursor === null ? {} : { cursor }) });
    all.push(...page.gaps);
    cursor = page.next;
  } while (cursor !== null);
  return all;
}

interface StagedRead {
  gaps: Gap[];
  resolving: boolean;
  failed: boolean;
}

/** The staged gaps (`answered`) and whether a rebuild runs (a gap `applying`). */
const splitStaged = (rows: Gap[]): StagedRead => ({
  gaps: rows.filter((g) => g.status === "answered"),
  resolving: rows.some((g) => g.status === "applying"),
  failed: false,
});

/** The staged answers and the button's state; read again on every `version` and every 5 s while a rebuild runs. `onSettled` runs when
 * a rebuild it saw running has ended. A failed read keeps what was shown (marked failed) and keeps polling while a rebuild ran. */
export function useStaged(gaps: GapsPort, projectId: string, version: number, onSettled: () => void): Staged {
  const [read, setRead] = useState<StagedRead>({ gaps: [], resolving: false, failed: false });
  const settled = useRef(onSettled);
  const wasResolving = useRef(false);
  useEffect(() => {
    settled.current = onSettled;
  }, [onSettled]);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const take = (next: StagedRead | null) => {
      setRead((r) => next ?? { ...r, failed: true });
      const resolving = next === null ? wasResolving.current : next.resolving;
      if (wasResolving.current && !resolving) settled.current();
      wasResolving.current = resolving;
      if (resolving) timer = setTimeout(again, POLL_MS);
    };
    function again() {
      readStaged(gaps, projectId).then(
        (rows) => {
          if (alive) take(splitStaged(rows));
        },
        () => {
          if (alive) take(null);
        },
      );
    }
    again();
    return () => {
      alive = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [gaps, projectId, version]);
  return { gaps: read.gaps, view: resolveView(read.gaps.length, read.resolving, read.failed) };
}

/** The header row's "Resolve gaps" button; off and busy keep the focus (aria-disabled) and give their reason. */
export function ResolveButton({ view, onOpen }: Readonly<{ view: ResolveView; onOpen: () => void }>) {
  const hintId = useId();
  const off = view.state !== "ready";
  return (
    <>
      <Tooltip label={view.hint} side="bottom">
        <Button
          variant="primary"
          aria-disabled={off ? true : undefined}
          aria-describedby={hintId}
          className={view.state === "busy" ? "gaps-resolve gaps-resolve--busy" : "gaps-resolve"}
          onClick={() => {
            if (!off) onOpen();
          }}
          data-testid="gaps-resolve"
          data-state={view.state}
        >
          {view.state === "busy" ? <span className="gdg-spinner gaps-resolve__spin" aria-hidden /> : null}
          {view.label}
        </Button>
      </Tooltip>
      <span id={hintId} className="shell-sr" data-testid="gaps-resolve-hint">
        {view.hint}
      </span>
    </>
  );
}

/** The confirmation of "Resolve gaps": what the rebuild applies, then the outcome in a toast. */
export function ResolveDialog({
  gaps,
  projectId,
  staged,
  names,
  onClose,
  onResolved,
}: Readonly<{
  gaps: GapsPort;
  projectId: string;
  staged: Gap[];
  /** Member names by user id: who staged each answer. */
  names: Readonly<Record<string, string>>;
  onClose: () => void;
  /** The rebuild started, or nothing waited any more: the lists read again. */
  onResolved: () => void;
}>) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = resolveView(staged.length, false).label;
  const more = staged.length - LISTED;
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      toast({ tone: "success", ...resolvedToast(await gaps.applyNow(projectId)) });
    } catch (e) {
      if (!isNothingStaged(e)) {
        setError(`The gaps could not be resolved. ${failureText(e)}`);
        setBusy(false);
        return;
      }
      toast({ tone: "info", ...NOTHING_STAGED_TOAST });
    }
    onClose();
    onResolved();
  };
  return (
    <Modal
      open
      size="md"
      title={`${label}?`}
      description="One rebuild applies every staged answer in this project, not only yours. The gaps become Resolved when it ends."
      onClose={onClose}
      closeDisabled={busy}
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void confirm()}
            disabled={busy || staged.length === 0}
            data-testid="gaps-resolve-confirm"
          >
            {label}
          </Button>
        </>
      }
    >
      <div className="gap-dialog" data-testid="gaps-resolve-dialog">
        <ul className="gaps-staged" aria-label="Staged answers" data-testid="gaps-resolve-list">
          {staged.slice(0, LISTED).map((g) => (
            <li key={g.id} className="gaps-staged__item" data-testid="gaps-resolve-item">
              <b className="gaps-staged__title">{g.title === "" ? g.id : g.title}</b>
              {g.clarification === null ? null : (
                <span className="gaps-staged__meta body-sm">
                  {stagedChoiceText(g.clarification)} · Staged by {answeredByText(g.clarification, names)}
                </span>
              )}
            </li>
          ))}
        </ul>
        {more > 0 ? (
          <p className="gap-text body-sm" data-testid="gaps-resolve-more">
            and {more} more
          </p>
        ) : null}
        {error === null ? null : (
          <p className="gap-alert body-sm" role="alert" data-testid="gaps-resolve-error">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
