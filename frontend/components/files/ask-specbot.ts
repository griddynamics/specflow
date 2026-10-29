"use client";
// FE-REV-01.AC4, FE-DEMO-09 (PLAN a.7, TECH-SPECS §1.5): the "Ask SpecBot" action of the Files selection toolbar.
// Opens SpecBot with EXACTLY the selected text (no prefix, PA22) waiting in the in-memory hand-over slot; SpecBot pre-fills its
// composer with it, does not send it, and starts with an empty conversation. Nothing travels in the URL.
import { useRouter } from "next/navigation";
import { useHandoff, useRoutes } from "@/lib/data/provider";

/** Returns the handler the toolbar calls with the exact selected text. */
export function useAskSpecBot(projectId: string): (selectedText: string) => void {
  const handoff = useHandoff();
  const routes = useRoutes();
  const router = useRouter();
  return (selectedText) => {
    handoff.setSpecBotPrefill(projectId, selectedText);
    router.push(routes.specbot(projectId));
  };
}
