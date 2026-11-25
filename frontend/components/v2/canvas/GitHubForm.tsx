"use client";
// GitHub repository: the repositories the caller's GitHub connection can read and the project does not have yet. Each one picked is added to
// the project (its clone check starts) and becomes its own node. The build does not read the clone (D19). Which repositories the app may see
// is chosen on GitHub, from Integrations.
import Link from "next/link";
import { useEffect, useState } from "react";
import { Button, Notice, PickList, PickRow } from "@graphico/ui";
import { githubFailureOf, githubRowOf, pickRowsOf, type PickRow as Pick } from "@/components/settings/github-logic";
import type { GitHubRepositoryList } from "@/lib/domain/github";
import type { FormProps } from "../connector-types";
import { useV2Ports, useV2Routes } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

interface Offer {
  active: boolean;
  rows: Pick[];
}

/** What the picker offers: the repositories the caller can read, minus those the project has; the ones over the size cap are listed but not pickable. */
function offerOf(active: boolean, available: GitHubRepositoryList, added: ReadonlySet<number>): Offer {
  return { active, rows: pickRowsOf(available.repositories, "", added).filter((r) => r.blocked !== "added") };
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
        if (alive)
          setOffer(offerOf(githubRowOf(connection).kind === "connected", available, new Set(list.repositories.map((r) => r.githubRepoId))));
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
    try {
      for (const id of picked) await github.add(projectId, id);
      reload.current();
      onClose();
    } catch (e) {
      setBusy(false);
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
  else if (!offer.active) body = <Notice>{CANVAS_COPY.githubConnectFirst}</Notice>;
  else if (offer.rows.length === 0) body = <Notice>{V2_COPY.noRepositories}</Notice>;
  else {
    body = (
      <PickList legend={V2_COPY.repositories}>
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
  return (
    <ConnectFrame
      onBack={onBack}
      action={
        <Button disabled={busy || picked.size === 0} onClick={() => void add()}>
          {busy ? CANVAS_COPY.githubAdding : CANVAS_COPY.githubAdd}
        </Button>
      }
    >
      {body}
      <Notice>{CANVAS_COPY.githubNote}</Notice>
      {problem === null ? null : <Notice role="alert">{problem}</Notice>}
      <Notice>
        <Link href={routes.integrations(projectId, { setup: "github" })}>{CANVAS_COPY.githubChoose}</Link>
      </Notice>
    </ConnectFrame>
  );
}
