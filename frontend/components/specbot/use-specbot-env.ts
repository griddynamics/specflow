"use client";
// What the SpecBot screen reads from the ports: the assistant port, the "Ask SpecBot" hand-over and the routes.
import { useHandoff, usePorts, useRoutes } from "@/lib/data/provider";

export function useSpecBotEnv() {
  return { assistant: usePorts().assistant, handoff: useHandoff(), routes: useRoutes() };
}
