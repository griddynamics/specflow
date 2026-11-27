"use client";
// FR-GWEB-04 (api.md "User interface", Gaps screen, noise report; detection.md "Relevance filter"): the sections the latest finished gap run
// judged tracking or mixed, with how it was decided, their signals and how many nodes they removed from the checks, and "Mark as not spec"
// per section (a `section` noise rule whose value is the lineage and the heading path joined by " › "). Below, the project's noise rules:
// each can be removed, and a new one (document, section or pattern) added. A rule decides the next gap run's relevance; removing it ends that.
// `503 gap_unavailable` shows the "not available" state; another failure offers a retry.
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { GlassSurface } from "@/components/shared/ui/GlassSurface";
import { OptionCards } from "@/components/ui/OptionCards";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";
import { TextField } from "@/components/ui/TextField";
import { NOISE_SCOPES, type GapNoise, type GapsPort, type NoiseRule, type NoiseScope, type NoiseSection } from "@/lib/domain/gaps";
import {
  DECIDED_BY_LABEL,
  RELEVANCE_LABEL,
  SCOPE_LABEL,
  failureOf,
  failureText,
  headingText,
  removedText,
  sectionRuleValue,
} from "@/components/shared/gaps/gaps-logic";
import { GapsUnavailable } from "./GapsUnavailable";

type Load = GapNoise | "unavailable" | "failed" | null;

/** The latest gap run's noise report; `tries` reads it again. */
function useNoise(gaps: GapsPort, projectId: string, tries: number): Load {
  const [data, setData] = useState<Load>(null);
  useEffect(() => {
    let alive = true;
    gaps.noise(projectId).then(
      (n) => {
        if (alive) setData(n);
      },
      (e: unknown) => {
        if (alive) setData(failureOf(e));
      },
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId, tries]);
  return data;
}

/** The noise rules, re-read after every change; a failed read leaves the list out (the report still shows). */
function useRules(gaps: GapsPort, projectId: string) {
  const [rules, setRules] = useState<NoiseRule[] | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let alive = true;
    gaps.noiseRules(projectId).then(
      (r) => {
        if (alive) setRules(r);
      },
      () => {
        if (alive) setRules(null);
      },
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId, version]);
  const reread = useCallback(() => {
    setVersion((n) => n + 1);
  }, []);
  return { rules, reread };
}

function SectionRow({ s, marked, onMark }: Readonly<{ s: NoiseSection; marked: boolean; onMark: () => Promise<void> }>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <li className="noise__row" data-testid="noise-section">
      <div className="noise__head">
        <h3 className="noise__title headline">{headingText(s)}</h3>
        <Tag tone={s.relevance === "tracking" ? "pending" : "warning"}>{RELEVANCE_LABEL[s.relevance]}</Tag>
        {marked ? (
          <span data-testid="noise-marked">
            <Tag tone="success">Marked as not spec</Tag>
          </span>
        ) : (
          <Button
            variant="glass"
            size="sm"
            disabled={busy}
            aria-label={`Mark as not spec: ${headingText(s)}`}
            onClick={() => {
              setBusy(true);
              setError(null);
              onMark().then(
                () => {
                  setBusy(false);
                },
                (e: unknown) => {
                  setError(failureText(e));
                  setBusy(false);
                },
              );
            }}
            data-testid="noise-mark-not-spec"
          >
            Mark as not spec
          </Button>
        )}
      </div>
      <p className="noise__meta body-sm">
        {DECIDED_BY_LABEL[s.by]} · {removedText(s.removed.length)}
      </p>
      {s.signals.length === 0 ? null : (
        <ul className="gap-nodes__list" aria-label="Signals">
          {s.signals.map((signal) => (
            <li key={signal}>
              <Tag>{signal}</Tag>
            </li>
          ))}
        </ul>
      )}
      {error === null ? null : (
        <p className="gap-alert body-sm" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

function AddRule({ onAdd }: Readonly<{ onAdd: (scope: NoiseScope, value: string, reason: string) => Promise<void> }>) {
  const [scope, setScope] = useState<NoiseScope>("section");
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ok = value.trim().length > 0 && value.trim().length <= 500 && reason.trim().length > 0 && reason.trim().length <= 500;
  const add = async () => {
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await onAdd(scope, value.trim(), reason.trim());
      setValue("");
      setReason("");
    } catch (e) {
      setError(failureText(e));
    } finally {
      setBusy(false);
    }
  };
  const HELP: Record<NoiseScope, string> = {
    document: "A document's lineage or path, for example Local files/meeting-notes.md.",
    section: "The lineage and the heading path joined by ›, for example upload:roadmap › Shop roadmap › Release plan.",
    pattern: "A regular expression searched in document paths, titles and headings.",
  };
  return (
    <form
      className="noise-add"
      aria-label="Add a noise rule"
      data-testid="noise-rule-add-form"
      onSubmit={(e) => {
        e.preventDefault();
        void add();
      }}
    >
      <h3 className="gap-sec__title eyebrow">Add a rule</h3>
      <div data-testid="noise-rule-scope">
        <OptionCards
          label="What is not spec"
          options={NOISE_SCOPES.map((s) => ({ value: s, label: SCOPE_LABEL[s] }))}
          value={scope}
          onChange={(v) => {
            setScope(NOISE_SCOPES.find((s) => s === v) ?? "section");
          }}
        />
      </div>
      <TextField
        label="Value"
        value={value}
        maxLength={500}
        help={HELP[scope]}
        onChange={(e) => {
          setValue(e.target.value);
        }}
        data-testid="noise-rule-value"
      />
      <TextField
        label="Reason"
        value={reason}
        maxLength={500}
        onChange={(e) => {
          setReason(e.target.value);
        }}
        data-testid="noise-rule-reason"
      />
      {error === null ? null : (
        <p className="gap-alert body-sm" role="alert" data-testid="noise-rule-error">
          {error}
        </p>
      )}
      <div>
        <Button type="submit" variant="glass" size="sm" disabled={!ok || busy} data-testid="noise-rule-add">
          Add rule
        </Button>
      </div>
    </form>
  );
}

function Rules({
  rules,
  onRemove,
  onAdd,
}: Readonly<{
  rules: NoiseRule[] | null;
  onRemove: (ruleId: string) => Promise<void>;
  onAdd: (scope: NoiseScope, value: string, reason: string) => Promise<void>;
}>) {
  const [error, setError] = useState<string | null>(null);
  return (
    <section className="gap-sec noise-rules" aria-label="Noise rules" data-testid="noise-rules">
      <h2 className="gap-sec__title eyebrow">Noise rules</h2>
      <p className="gaps-count body-sm">Sections these rules match are read as tracking notes by the next gap run.</p>
      <RuleList rules={rules} onRemove={onRemove} onError={setError} />
      {error === null ? null : (
        <p className="gap-alert body-sm" role="alert">
          {error}
        </p>
      )}
      <AddRule onAdd={onAdd} />
    </section>
  );
}

function RuleList({
  rules,
  onRemove,
  onError,
}: Readonly<{ rules: NoiseRule[] | null; onRemove: (ruleId: string) => Promise<void>; onError: (text: string | null) => void }>) {
  if (rules === null) return null;
  if (rules.length === 0) {
    return (
      <div data-testid="noise-rules-empty">
        <EmptyState title="No noise rules yet" icon="funnel-simple">
          Mark a section above as not spec, or add a rule below.
        </EmptyState>
      </div>
    );
  }
  return (
    <GlassSurface variant="strong" className="noise">
      <ul className="noise__list" aria-label="Noise rules">
        {rules.map((r) => (
          <li key={r.ruleId} className="noise__row" data-testid="noise-rule" data-rule={r.ruleId}>
            <div className="noise__head">
              <Tag>{SCOPE_LABEL[r.scope]}</Tag>
              <span className="noise__value">{r.value}</span>
              <Button
                variant="plain"
                size="sm"
                aria-label={`Remove rule: ${r.value}`}
                onClick={() => {
                  onError(null);
                  onRemove(r.ruleId).catch((e: unknown) => {
                    onError(failureText(e));
                  });
                }}
                data-testid="noise-rule-remove"
              >
                Remove
              </Button>
            </div>
            <p className="noise__meta body-sm">{r.reason}</p>
          </li>
        ))}
      </ul>
    </GlassSurface>
  );
}

export function NoiseReport({ gaps, projectId }: Readonly<{ gaps: GapsPort; projectId: string }>) {
  const [tries, setTries] = useState(0);
  const data = useNoise(gaps, projectId, tries);
  const { rules, reread } = useRules(gaps, projectId);
  const add = async (scope: NoiseScope, value: string, reason: string) => {
    await gaps.addNoiseRule(projectId, { scope, value, reason });
    reread();
  };
  const remove = async (ruleId: string) => {
    try {
      await gaps.removeNoiseRule(projectId, ruleId);
    } finally {
      reread(); // a rule removed meanwhile (404) leaves the list as well
    }
  };
  const ruleRows = <Rules rules={rules} onRemove={remove} onAdd={add} />;
  if (data === null) return <Skeleton variant="block" height={200} />;
  if (data === "unavailable") return <GapsUnavailable />;
  if (data === "failed") {
    return (
      <EmptyState
        title="The noise report could not be loaded"
        icon="warning-circle"
        actions={
          <Button
            onClick={() => {
              setTries((n) => n + 1);
            }}
          >
            Try again
          </Button>
        }
      >
        Check your connection and try again.
      </EmptyState>
    );
  }
  let report;
  if (data.runId === null) {
    report = (
      <div data-testid="noise-none">
        <EmptyState title="No gap run has finished yet" icon="funnel-simple">
          The noise report lists the sections a gap run set aside as tracking notes rather than specification.
        </EmptyState>
      </div>
    );
  } else if (data.sections.length === 0) {
    report = (
      <div data-testid="noise-empty">
        <EmptyState title="No noise found" icon="check-circle">
          The latest gap run read every section as specification.
        </EmptyState>
      </div>
    );
  } else {
    const ruled = new Set((rules ?? []).filter((r) => r.scope === "section").map((r) => r.value));
    report = (
      <GlassSurface variant="strong" className="noise">
        <ul className="noise__list" aria-label="Noise report" data-testid="noise-report">
          {data.sections.map((s) => {
            const value = sectionRuleValue(s.lineage, s.headingPath);
            return (
              <SectionRow
                key={`${s.lineage}|${s.headingPath.join("/")}`}
                s={s}
                marked={ruled.has(value)}
                onMark={() => add("section", value, "not spec")}
              />
            );
          })}
        </ul>
      </GlassSurface>
    );
  }
  return (
    <div className="noise-tab">
      {report}
      {ruleRows}
    </div>
  );
}
