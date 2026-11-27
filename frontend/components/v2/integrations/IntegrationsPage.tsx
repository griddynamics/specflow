"use client";
// Mission Control V2, README "Integrations": one card per app, three to a row. It only connects: no switches, no list of uses, no page
// title (the header names the page). A card is grey and dashed until a member connects the app, then ink; the health word comes from the
// API. An OAuth return lands here with `?github=`, `?atlassian=`, `?linear=`, `?box=`, `?dropbox=` or `?notion=`: the connection is bound to the
// project, and `next` sends the browser back to the canvas form that asked.
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Dialog, Notice, Page } from "@graphico/ui";
import { useToast } from "@/components/ui/Toast";
import { canDisconnect, type BindableApp, type IntegrationApp, type IntegrationCardId } from "@/lib/domain/integrations";
import { parseIntegrationsQuery } from "@/lib/routes";
import type { McpAddress } from "../connector-types";
import { useV2Routes } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { IntegrationCards } from "./IntegrationCards";
import { integrationFailure, isLinked, returnToast } from "./integration-logic";
import { INTEGRATION_CARDS } from "./registry";
import { McpSetup } from "./McpSetup";
import { useIntegrations } from "./use-integrations";

const titleOf = (card: IntegrationCardId | null): string => {
  if (card === null) return V2_COPY.integrations;
  return card === "mcp" ? V2_COPY.mcpCard.name : V2_COPY.appNames[card];
};

const isBindable = (app: IntegrationApp): app is BindableApp => app !== "slack";

/** The app whose sign-in or install page the browser just came back from, by the query parameter it set. */
function returnedAppOf(query: {
  github: string | null;
  atlassian: string | null;
  linear: string | null;
  box: string | null;
  dropbox: string | null;
  notion: string | null;
}): BindableApp | null {
  if (query.github !== null) return "github";
  if (query.linear !== null) return "linear";
  if (query.box !== null) return "box";
  if (query.dropbox !== null) return "dropbox";
  if (query.notion !== null) return "notion";
  return query.atlassian === null ? null : "atlassian";
}

export function IntegrationsPage({ projectId, mcp }: Readonly<{ projectId: string; mcp: McpAddress }>) {
  const routes = useV2Routes();
  const router = useRouter();
  const toast = useToast();
  const query = parseIntegrationsQuery(useSearchParams());
  const [open, setOpen] = useState<IntegrationCardId | null>(query.setup);
  const returned = useRef(false); // the return code of an app is handled once per mount

  const goHome = useCallback(() => {
    router.replace(routes.home());
  }, [router, routes]);
  const { data, load, connect, disconnect } = useIntegrations(projectId, goHome);
  const next = query.next;

  // Back from an app's sign-in or install page: bind the account to the project, say what failed, then leave the code out of the URL.
  const code = query.github ?? query.linear ?? query.box ?? query.dropbox ?? query.notion ?? query.atlassian;
  const returnedApp = returnedAppOf(query);
  useEffect(() => {
    if (returnedApp === null || code === null || returned.current) return;
    returned.current = true;
    const clean = routes.integrations(projectId, { setup: returnedApp, next: next ?? undefined });
    if (isLinked(code)) {
      void connect(returnedApp).then((bound) => {
        // GitHub still needs its repositories picked here; Atlassian, Linear, Box, Dropbox and Notion are done, so the form that asked comes back.
        if (bound && next !== null && returnedApp !== "github") router.replace(next);
        else router.replace(clean, { scroll: false });
      });
    } else {
      toast(returnToast(returnedApp, code));
      router.replace(clean, { scroll: false });
    }
  }, [returnedApp, code, connect, toast, router, routes, projectId, next]);

  const done = () => {
    setOpen(null);
    if (next !== null) router.push(next);
  };

  if (data === null || data === "failed") {
    return (
      <Page>
        {data === null ? (
          <Notice>{V2_COPY.loading}</Notice>
        ) : (
          <>
            <Notice role="alert">{V2_COPY.loadFailed}</Notice>
            <div>
              <Button
                size="sm"
                onClick={() => {
                  void load();
                }}
              >
                {V2_COPY.tryAgain}
              </Button>
            </div>
          </>
        )}
      </Page>
    );
  }
  const current = INTEGRATION_CARDS.find((c) => c.app === open);
  const currentRow = data.rows.find((r) => r.app === open);
  const bind = (app: IntegrationApp | undefined) => (): Promise<boolean> =>
    app !== undefined && isBindable(app) ? connect(app) : Promise.resolve(false);
  const close = () => {
    setOpen(null);
  };
  return (
    <Page>
      <IntegrationCards rows={data.rows} onOpen={setOpen} />
      <Dialog open={open !== null} onClose={close} title={titleOf(open)} eyebrow={V2_COPY.integrationEyebrow}>
        {open === "mcp" ? <McpSetup projectId={projectId} mcp={mcp} onDone={done} /> : null}
        {current === undefined || currentRow === undefined ? null : (
          <current.Setup
            projectId={projectId}
            integration={currentRow}
            canDisconnect={canDisconnect(currentRow, data.meId, data.isOwner)}
            returnTo={routes.integrations(projectId, { setup: current.app, next: next ?? undefined })}
            onConnect={bind(current.app)}
            onDisconnect={() => disconnect(current.app)}
            onRefresh={() => {
              void load();
            }}
            onError={(e) => {
              const failure = integrationFailure(e);
              if (failure.kind === "gone") goHome();
              else toast({ title: failure.text, tone: "danger" });
            }}
            onClose={close}
            onDone={done}
          />
        )}
      </Dialog>
    </Page>
  );
}
