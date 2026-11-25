"use client";
// The data of the Integrations page: the three integrations of the project, who the caller is and whether they own the project (the
// owner may disconnect what another member connected, AC6). `connect` and `disconnect` run the port call, say the result in a toast and
// read the list again. Ports only.
import { useCallback, useEffect, useRef, useState } from "react";
import { useToast } from "@/components/ui/Toast";
import { usePorts } from "@/lib/data/provider";
import type { BindableApp, Integration, IntegrationApp } from "@/lib/domain/integrations";
import { V2_COPY } from "../v2-copy";
import { disconnectsThroughSlackRoute, integrationFailure } from "./integration-logic";

export interface IntegrationsData {
  rows: Integration[];
  meId: string | null;
  isOwner: boolean;
}

export function useIntegrations(projectId: string, onGone: () => void) {
  const { integrations, people, members, slack } = usePorts();
  const toast = useToast();
  const [data, setData] = useState<IntegrationsData | "failed" | null>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const load = useCallback(async (): Promise<void> => {
    try {
      const [rows, me, owner] = await Promise.all([
        integrations.list(projectId),
        people.me().then(
          (p) => p.id,
          () => null,
        ),
        members.list(projectId).then(
          (s) => s.role === "owner",
          () => false,
        ),
      ]);
      if (live.current) setData({ rows, meId: me, isOwner: owner });
    } catch (e: unknown) {
      if (!live.current) return;
      if (integrationFailure(e).kind === "gone") onGone();
      else setData((d) => (d === null || d === "failed" ? "failed" : d)); // a failed refresh keeps what is shown
    }
  }, [integrations, people, members, projectId, onGone]);
  useEffect(() => {
    void load();
  }, [load]);

  const run = useCallback(
    async (action: () => Promise<void>, done: string): Promise<boolean> => {
      try {
        await action();
        toast({ title: done, tone: "success" });
        await load();
        return true;
      } catch (e: unknown) {
        const failure = integrationFailure(e);
        if (failure.kind === "gone") onGone();
        else toast({ title: failure.text, tone: "danger" });
        await load();
        return false;
      }
    },
    [toast, load, onGone],
  );
  const connect = useCallback(
    (app: BindableApp) => run(() => integrations.connect(projectId, app).then(() => undefined), V2_COPY.connectedToast),
    [run, integrations, projectId],
  );
  const disconnect = useCallback(
    (app: IntegrationApp) =>
      run(
        () => (disconnectsThroughSlackRoute(app) ? slack.unbind(projectId) : integrations.disconnect(projectId, app)),
        V2_COPY.disconnectedToast,
      ),
    [run, integrations, slack, projectId],
  );
  return { data, load, connect, disconnect };
}
