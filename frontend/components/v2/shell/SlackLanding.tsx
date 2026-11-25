"use client";
// `/?slack=linked|<code>`: where the link route of `/specflow link` lands the browser. One toast, then the parameter leaves the URL.
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef } from "react";
import { useToast } from "@/components/ui/Toast";
import { slackLandingToast } from "../integrations/slack-logic";
import { useV2Routes } from "../use-v2-data";

export function SlackLanding() {
  const code = useSearchParams().get("slack");
  const toast = useToast();
  const router = useRouter();
  const routes = useV2Routes();
  const shown = useRef(false);
  useEffect(() => {
    if (code === null || shown.current) return;
    shown.current = true;
    toast(slackLandingToast(code));
    router.replace(routes.home(), { scroll: false });
  }, [code, toast, router, routes]);
  return null;
}
