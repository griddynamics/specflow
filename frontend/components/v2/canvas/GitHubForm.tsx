"use client";
// GitHub repository: the repositories the caller's GitHub connection can read and the project does not have yet. Each one picked is added to
// the project (its clone check starts) and becomes its own node. The build does not read the clone (D19). Which repositories the app may see
// is chosen on GitHub, from Integrations. What the API says about the caller decides what the form offers: a member it refuses sees one
// read-only line (`canManage`), nothing is guessed from a role here.
import Link from "next/link";
import { useEffect, useState } from "react";
import { Button, Notice, PickList, PickRow } from "@graphico/ui";
import { githubFailureOf, githubRowOf, pickRowsOf, type PickRow as Pick } from "@/components/shared/settings/github-logic";
import type { GitHubRepositoryList } from "@/lib/domain/github";
import type { FormProps } from "../connector-types";
import { HowTo } from "../HowTo";
import { useV2Ports, useV2Routes } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

export interface Offer {
  active: boolean;
  /** The caller may add repositories (`github.list`'s `canManage`). */
  canManage: boolean;
  rows: Pick[];
}

/** What the picker offers: the repositories the caller can read, minus those the project has; the ones over the size cap are listed but not pickable. */
export function offerOf(active: boolean, canManage: boolean, available: GitHubRepositoryList, added: ReadonlySet<number>): Offer {
  return { active, canManage, rows: pickRowsOf(available.repositories, "", added).filter((r) => r.blocked !== "added") };
}

/** After a failed add: the offer without the repositories that did get in, and the picks without them. */
export function withoutAdded(offer: Offer, added: ReadonlySet<number>): Offer {
  return { ...offer, rows: offer.rows.filter((r) => !added.has(r.repo.githubRepoId)) };
}

export function GitHubForm({ projectId, reload, onBack, onClose }: Readonly<FormProps>) {
  const { github } = useV2Ports();
  const routes = useV2Routes();
  const [offer, setOffer] = useState<Offer | "failed" | null>(null);
  const [picked, setPicked] = useState<ReadonlySet<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([github.connection(), github.available(), github.list(projectId)]).then(
      ([connection, available, list]) => {
        if (!alive) return;
        const added = new Set(list.repositories.map((r) => r.githubRepoId));
        setOffer(offerOf(githubRowOf(connection).kind === "connected", list.canManage, available, added));
      },
      () => {
        if (alive) setOffer("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [github, projectId]);

  const add = async () => {
    setBusy(true);
    setProblem(null);
    const added = new Set<number>();
    try {
      for (const id of picked) {
        await github.add(projectId, id);
        added.add(id);
      }
      reload.current();
      onClose();
    } catch (e) {
      // Some may be in: read the canvas again, and leave only the ones that are not.
      reload.current();
      setBusy(false);
      setOffer((o) => (o === null || o === "failed" ? o : withoutAdded(o, added)));
      setPicked((now) => new Set([...now].filter((id) => !added.has(id))));
      const f = githubFailureOf(e);
      setProblem(f.kind === "gone" ? V2_COPY.actionFailed : f.text);
    }
  };
  const toggle = (id: number, on: boolean) => {
    setPicked((now) => (on ? new Set([...now, id]) : new Set([...now].filter((x) => x !== id))));
  };

  let body;
  if (offer === null) body = <Notice>{V2_COPY.loading}</Notice>;
  else if (offer === "failed") body = <Notice role="alert">{V2_COPY.repositoriesFailed}</Notice>;
  else if (!offer.canManage) body = <Notice>{V2_COPY.readOnlyGithub}</Notice>;
  else if (!offer.active) body = <Notice>{CANVAS_COPY.githubConnectFirst}</Notice>;
  else if (offer.rows.length === 0) body = <Notice>{V2_COPY.noRepositories}</Notice>;
  else {
    body = (
      <PickList legend={CANVAS_COPY.githubLegend}>
        {offer.rows.map(({ repo, blocked }) => (
          <PickRow
            key={repo.githubRepoId}
            label={repo.fullName}
            note={blocked === "too_large" ? V2_COPY.tooLarge : undefined}
            checked={picked.has(repo.githubRepoId)}
            disabled={busy || blocked !== null}
            onChange={(on) => {
              toggle(repo.githubRepoId, on);
            }}
          />
        ))}
      </PickList>
    );
  }
  const refused = offer !== null && offer !== "failed" && !offer.canManage;
  return (
    <ConnectFrame
      onBack={onBack}
      action={
        refused ? undefined : (
          <Button disabled={busy || picked.size === 0} onClick={() => void add()}>
            {busy ? CANVAS_COPY.githubAdding : CANVAS_COPY.githubAdd}
          </Button>
        )
      }
    >
      {body}
      {refused ? null : <Notice>{CANVAS_COPY.githubNote}</Notice>}
      {problem === null ? null : <Notice role="alert">{problem}</Notice>}
      {refused ? null : (
        <Notice>
          <Link href={routes.integrations(projectId, { setup: "github" })}>{CANVAS_COPY.githubChoose}</Link>
        </Notice>
      )}
      {refused ? null : <HowTo steps={CANVAS_COPY.howGithubInstall} />}
    </ConnectFrame>
  );
}
