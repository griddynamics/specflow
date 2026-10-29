"use client";
// FE-DATA-01, SPL-DEMO-01: the one client boundary for data. `DataProvider` builds the production ports (http plus the
// `unavailable` adapter) and a small UI store once, in the initializer, so nothing is fetched here and nothing persists.
import { createContext, useCallback, useContext, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { routesFor, type RouteBuilder } from "@/lib/routes";
import { composeProductionPorts } from "./compose";
import { createHttpAdapter } from "./http";
import type { DataPorts, UiHandoff } from "./ports";
import { createUiHandoff, createUiStore, type UiStore } from "./ui-store";

export interface DataContextValue {
  ui: UiStore;
  ports: DataPorts;
  routes: RouteBuilder;
  handoff: UiHandoff;
}
export const DataContext = createContext<DataContextValue | null>(null);

function useCtx(): DataContextValue {
  const ctx = useContext(DataContext);
  if (!ctx) throw new Error("DataProvider is missing");
  return ctx;
}

export function DataProvider(props: Readonly<{ children: ReactNode }>) {
  const [ui] = useState(createUiStore);
  const value = useMemo<DataContextValue>(
    () => ({
      ui,
      ports: composeProductionPorts(createHttpAdapter()),
      routes: routesFor("production"),
      handoff: createUiHandoff(ui),
    }),
    [ui],
  );
  return <DataContext.Provider value={value}>{props.children}</DataContext.Provider>;
}

export const usePorts = (): DataPorts => useCtx().ports;
export const useRoutes = (): RouteBuilder => useCtx().routes;
export const useHandoff = (): UiHandoff => useCtx().handoff;
export function useCurrentProject(): string | null {
  const { ui } = useCtx();
  const get = () => ui.get().currentProjectId;
  const subscribe = useCallback((listener: () => void) => ui.subscribe(listener), [ui]);
  return useSyncExternalStore(subscribe, get, get);
}
export function useSetCurrentProject(): (id: string) => void {
  const { ui } = useCtx();
  return (id) => {
    ui.update((s) => ({ ...s, currentProjectId: id }));
  };
}
