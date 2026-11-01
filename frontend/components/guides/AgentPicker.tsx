"use client";
// MCP-M10 (FR-MCP-UI-05): the "Agent" SegmentedControl and its status Tag. The choice lives in the URL (`?agent=`), so a link opens
// the guide on an agent; the server page renders the steps for it. `path` is the page's own path, passed by the server page.
import { useOptimistic, startTransition } from "react";
import { useRouter } from "next/navigation";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Tag } from "@/components/ui/Tag";
import { AGENTS, isTested, type AgentId } from "@/lib/guides/agent";
import { MCP_GUIDE } from "@/lib/guides/content";

export function AgentPicker({ agent, path, local }: Readonly<{ agent: AgentId; path: string; local: boolean }>) {
  const router = useRouter();
  const [shown, setShown] = useOptimistic(agent);
  return (
    <div className="guides-picker">
      <SegmentedControl
        label={MCP_GUIDE.picker.label}
        options={AGENTS.map((a) => ({ value: a.value, label: a.label }))}
        value={shown}
        onChange={(v) => {
          const next = AGENTS.find((a) => a.value === v)?.value;
          if (next === undefined) return;
          startTransition(() => {
            setShown(next);
            router.replace(`${path}?agent=${next}`, { scroll: false });
          });
        }}
      />
      <Tag tone={isTested(shown) ? "success" : "neutral"}>{isTested(shown) ? MCP_GUIDE.picker.tested : MCP_GUIDE.picker.inProgress}</Tag>
      {local ? <Tag tone="info">{MCP_GUIDE.picker.local}</Tag> : null}
    </div>
  );
}
