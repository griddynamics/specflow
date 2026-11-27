"use client";
// FR-GWEB-04 (answers.md "The question card", "Answering", "Own answers become edits", "Placement rules"): the answering part of the
// question card. The options are a real radio group (OptionCards: one Tab stop, arrow keys move and select) with the recommended option
// marked and preselected for a `major` gap, plus "Other" with the person's own text (1..4000 characters). "Preview edits" asks the server
// for the edits the answer would make (`edits_preview`) and shows them as before/after per section; the placement choice ("Amend the source
// section" / "Clarifications document") is preset from the preview and switching it asks again with that placement; a `contradiction` is
// always amended in its source. The answer button (in the card's footer: "Stage answer", "Change answer" or "Answer and resolve") sends
// the choice, the placement, the preview's edits and the gap's `updated_at` as read; `409 gap_stale` turns into "This gap changed since
// you opened it" with Reload (the choice stays when the reloaded gap still offers it). Own text needs a preview before the answer is
// sent, so the person sees the diff first.
import { useCallback, useState } from "react";
import { Button } from "@/components/ui/Button";
import { OTHER_VALUE, OptionCards } from "@/components/ui/OptionCards";
import type { AnswerResult, EditPlacement, EditPreview, Gap, GapAnswerChoice, GapsPort } from "@/lib/domain/gaps";
import type { Member } from "@/lib/domain/members";
import { PLACEMENT_LABEL, failureText, isStale, ownTextProblem, preselectedOption } from "@/components/shared/gaps/gaps-logic";

/** What the card needs from the screen to write: the port, the project, and how to re-read the gap and refresh the lists. */
export interface CardControl {
  gaps: GapsPort;
  projectId: string;
  /** The project's members: the names in the card's history. */
  members: Member[];
  /** The `?gap=` link of another gap (history, follow-ups). */
  href: (gapId: string) => string;
  /** Opens another gap's card in place. */
  onOpenGap: (gapId: string) => void;
  /** Re-reads this gap (the card shows the new one). */
  onReload: () => Promise<void>;
  /** The gap changed: the inbox and the other lists read again. */
  onChanged: () => void;
  /** The gap was dismissed: the card closes and the inbox reads again. */
  onDismissed: () => void;
}

interface PreviewState {
  key: string;
  phase: "loading" | "ready" | "failed";
  data?: EditPreview;
  error?: unknown;
}

const keyOf = (gap: Gap, choice: GapAnswerChoice, placement: EditPlacement | undefined) =>
  JSON.stringify([gap.id, gap.updatedAt, choice, placement ?? null]);

/** The answer form's state and actions; the card renders the form and puts the answer button in its footer. */
export function useGapAnswer(gap: Gap, control: CardControl | undefined) {
  const [picked, setPicked] = useState<string | undefined>(() => {
    const rank = preselectedOption(gap);
    if (rank !== undefined) return String(rank);
    return gap.clarification?.mode === "own" ? OTHER_VALUE : undefined;
  });
  const [ownText, setOwnText] = useState(() => (gap.clarification?.mode === "own" ? (gap.clarification.text ?? "") : ""));
  const [placement, setPlacement] = useState<EditPlacement | undefined>(undefined);
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [result, setResult] = useState<AnswerResult | null>(null);

  // A choice the (reloaded) gap no longer offers is no choice.
  const choiceValue = picked === OTHER_VALUE || gap.options.some((o) => String(o.rank) === picked) ? picked : undefined;
  const choice: GapAnswerChoice | null = (() => {
    if (choiceValue === undefined) return null;
    if (choiceValue === OTHER_VALUE) return ownTextProblem(ownText) === null ? { ownText: ownText.trim() } : null;
    return { optionRank: Number(choiceValue) };
  })();
  const contradiction = gap.kind === "contradiction";
  const asked = contradiction ? "amend" : placement;
  const shown = preview !== null && choice !== null && preview.key === keyOf(gap, choice, asked) ? preview : null;
  const shownPlacement: EditPlacement | undefined = shown?.data?.placement ?? asked;

  const requestPreview = useCallback(
    (c: GapAnswerChoice, pl: EditPlacement | undefined) => {
      if (control === undefined) return;
      const key = keyOf(gap, c, pl);
      setError(null);
      setPreview({ key, phase: "loading" });
      control.gaps.previewEdits(control.projectId, gap.id, c, pl).then(
        (data) => {
          setPreview((p) => (p?.key === key ? { key, phase: "ready", data } : p));
        },
        (e: unknown) => {
          if (isStale(e)) setStale(true);
          setPreview((p) => (p?.key === key ? { key, phase: "failed", error: e } : p));
        },
      );
    },
    [control, gap],
  );

  const choose = (value: string) => {
    setPicked(value);
    setPlacement(undefined);
    setResult(null);
    setError(null);
  };
  const changePlacement = (pl: EditPlacement) => {
    if (contradiction || choice === null) return;
    setPlacement(pl);
    requestPreview(choice, pl);
  };
  const ownProblem = choiceValue === OTHER_VALUE ? ownTextProblem(ownText) : null;
  const needsPreview = choiceValue === OTHER_VALUE && shown?.phase !== "ready";
  const canSubmit = control !== undefined && choice !== null && !busy && !needsPreview && shown?.phase !== "loading";

  const submit = async () => {
    if (control === undefined || choice === null || !canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const data = shown?.phase === "ready" ? shown.data : undefined;
      const r = await control.gaps.answer(control.projectId, gap.id, {
        choice,
        placement: data?.placement ?? asked,
        edits: data?.edits ?? [],
        updatedAt: gap.updatedAt,
      });
      setResult(r);
      control.onChanged();
      await control.onReload();
    } catch (e) {
      if (isStale(e)) setStale(true);
      else setError(failureText(e));
    } finally {
      setBusy(false);
    }
  };

  const reload = async () => {
    if (control === undefined) return;
    setBusy(true);
    try {
      await control.onReload();
      setStale(false);
      setError(null);
    } catch (e) {
      setError(failureText(e));
    } finally {
      setBusy(false);
    }
  };

  return {
    choiceValue,
    choice,
    ownText,
    setOwnText: (t: string) => {
      setOwnText(t);
      setResult(null);
    },
    ownProblem,
    choose,
    preview: shown,
    previewNow: () => {
      if (choice !== null) requestPreview(choice, asked);
    },
    placement: shownPlacement,
    changePlacement,
    contradiction,
    needsPreview,
    canSubmit,
    busy,
    error,
    stale,
    reload,
    result,
    submit,
  };
}

export type GapAnswerState = ReturnType<typeof useGapAnswer>;

/** The heading of a section: its document lineage and heading path, or the generated document. */
function sectionTitle(s: EditPreview["sections"][number], placement: EditPlacement): string {
  const path = s.headingPath.join(" › ");
  if (placement === "document" && s.lineage === null) return path === "" ? "Clarifications document" : `Clarifications › ${path}`;
  return [s.lineage ?? "", path].filter((x) => x !== "").join(" › ") || "Section";
}

function PreviewView({ state }: Readonly<{ state: NonNullable<GapAnswerState["preview"]> }>) {
  if (state.phase === "loading") {
    return (
      <p className="gap-text body-sm" role="status" aria-busy="true" data-testid="gap-preview-loading">
        Preparing the edits…
      </p>
    );
  }
  if (state.phase === "failed" || state.data === undefined) {
    return (
      <p className="gap-alert body-sm" role="alert" data-testid="gap-preview-error">
        {isStale(state.error) ? "This gap changed since you opened it." : `The edits could not be prepared. ${failureText(state.error)}`}
      </p>
    );
  }
  const data = state.data;
  return (
    <div className="gap-preview" data-testid="gap-preview" data-placement={data.placement}>
      {data.fallback === null ? null : (
        <p className="gap-text body-sm" data-testid="gap-preview-fallback">
          Specflow could not turn this answer into edits of the source section, so it goes to the clarifications document as written.
        </p>
      )}
      {data.sections.length > 0 && (
        <ul className="gap-preview__list" aria-label="Edits preview">
          {data.sections.map((s, i) => (
            <li key={`${s.fragmentId ?? "doc"}-${String(i)}`} className="gap-preview__sec" data-testid="gap-preview-section">
              <h4 className="gap-preview__where body-sm">{sectionTitle(s, data.placement)}</h4>
              <div className="gap-preview__cols">
                <div className="gap-preview__col">
                  <span className="gap-preview__label eyebrow">Before</span>
                  <p className="gap-preview__text body-sm" data-testid="gap-preview-before">
                    {s.before === "" ? "(new section)" : s.before}
                  </p>
                </div>
                <div className="gap-preview__col gap-preview__col--after">
                  <span className="gap-preview__label eyebrow">After</span>
                  <p className="gap-preview__text body-sm" data-testid="gap-preview-after">
                    {s.after}
                  </p>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The options, "Other", the preview, the placement and the messages of an answerable gap. */
export function AnswerForm({ gap, a }: Readonly<{ gap: Gap; a: GapAnswerState }>) {
  const recommended = gap.options.find((o) => o.recommended)?.rank;
  // A development gap whose investigation still runs has no options yet; an own answer is accepted at once.
  const preparing = gap.options.some(Boolean) ? undefined : "Options are being prepared. You can answer in your own words now.";
  return (
    <div className="gap-answer" data-testid="gap-answer-form">
      <section className="gdg-q">
        <div className="gdg-q__head">
          <span className="gdg-q__num">Q</span>
          <div>
            <h3 className="gdg-q__title">{gap.question}</h3>
            {preparing === undefined ? null : (
              <p className="gdg-q__help" data-testid="gap-no-options">
                {preparing}
              </p>
            )}
          </div>
        </div>
        <div data-testid="gap-options">
          <OptionCards
            label={gap.question}
            options={gap.options.map((o) => ({
              value: String(o.rank),
              label: o.label,
              description: o.detail === "" ? undefined : o.detail,
            }))}
            allowOther
            otherLabel="Other: answer in your own words"
            otherPlaceholder="Write your answer (up to 4,000 characters)"
            defaultOther={a.ownText}
            onOtherChange={a.setOwnText}
            value={a.choiceValue}
            onChange={a.choose}
            recommended={recommended === undefined ? undefined : String(recommended)}
          />
        </div>
      </section>
      {a.ownProblem !== null && a.ownText !== "" ? (
        <p className="gap-alert body-sm" role="alert" data-testid="gap-own-error">
          {a.ownProblem}
        </p>
      ) : null}
      <div className="gap-answer__row">
        <Button variant="glass" size="sm" disabled={a.choice === null || a.busy} onClick={a.previewNow} data-testid="gap-preview-button">
          Preview edits
        </Button>
        {a.needsPreview && a.choice !== null ? <span className="gap-text body-sm">Preview the edits before you answer.</span> : null}
      </div>
      {a.preview === null ? null : <PreviewView state={a.preview} />}
      {a.preview?.phase === "ready" ? <PlacementChoice a={a} /> : null}
    </div>
  );
}

function PlacementChoice({ a }: Readonly<{ a: GapAnswerState }>) {
  if (a.contradiction) {
    return (
      <p className="gap-text body-sm" data-testid="gap-placement-fixed">
        {PLACEMENT_LABEL.amend}: a contradiction is always fixed in its source section.
      </p>
    );
  }
  return (
    <div className="gap-placement" data-testid="gap-placement">
      <h4 className="gap-sec__title eyebrow">Where the answer goes</h4>
      <OptionCards
        label="Where the answer goes"
        options={[
          { value: "amend", label: PLACEMENT_LABEL.amend, description: "The answer is written into the section the gap is about." },
          { value: "document", label: PLACEMENT_LABEL.document, description: "The answer goes into a document Specflow keeps." },
        ]}
        value={a.placement}
        onChange={(v) => {
          a.changePlacement(v === "document" ? "document" : "amend");
        }}
      />
    </div>
  );
}

/** The outcome messages under the form: stale (with Reload), a refusal in words, or what happens to the recorded answer (staged, or
 * applied at once). */
export function AnswerMessages({ a, outcome }: Readonly<{ a: GapAnswerState; outcome: string | null }>) {
  return (
    <>
      {a.stale ? (
        <div className="gap-stale" role="alert" data-testid="gap-stale">
          <p className="gap-text">This gap changed since you opened it.</p>
          <Button variant="glass" size="sm" onClick={() => void a.reload()} disabled={a.busy} data-testid="gap-stale-reload">
            Reload
          </Button>
        </div>
      ) : null}
      {a.error === null ? null : (
        <p className="gap-alert body-sm" role="alert" data-testid="gap-answer-error">
          {a.error}
        </p>
      )}
      {outcome === null ? null : (
        <p className="gap-done body-sm" role="status" data-testid="gap-answer-result">
          {outcome}
        </p>
      )}
    </>
  );
}
