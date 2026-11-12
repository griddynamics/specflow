"use client";
// MCP-TOOLING session 10: Plan › Work. The findings agents reported (answer the blocking ones first) and who holds which claim. Each
// section reads on its own; an answer or a release re-reads both (a resolved blocking finding frees its story, so the claims move too).
import { useState } from "react";
import { ClaimsBoard } from "./ClaimsBoard";
import { FindingsSection } from "./FindingsSection";
import "./work.css";

export function WorkTab({ projectId }: Readonly<{ projectId: string }>) {
  const [refresh, setRefresh] = useState(0);
  const changed = () => {
    setRefresh((n) => n + 1);
  };
  return (
    <div className="work" data-testid="plan-work" data-project={projectId}>
      <FindingsSection projectId={projectId} refresh={refresh} onChanged={changed} />
      <ClaimsBoard projectId={projectId} refresh={refresh} onChanged={changed} />
    </div>
  );
}
