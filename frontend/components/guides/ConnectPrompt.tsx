"use client";
// Step 3 of the MCP guide: the prompt that binds a repository, with the active project's name when the shell knows it (the id from
// the shell's current project, the name from the same project list the switcher reads) and `<project name>` otherwise. Outside a
// DataProvider (static render) the placeholder shows.
import { useContext } from "react";
import { useProjectList } from "@/components/shell/use-shell-data";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { DataContext, useCurrentProject } from "@/lib/data/provider";
import { connectPrompt } from "@/lib/guides/snippets";

function ActiveProjectPrompt({ caption }: Readonly<{ caption: string }>) {
  const id = useCurrentProject();
  const list = useProjectList();
  const name = id === null ? null : (list?.find((p) => p.id === id)?.name ?? null);
  return <CodeBlock caption={caption} text={connectPrompt(name)} />;
}

export function ConnectPrompt({ caption }: Readonly<{ caption: string }>) {
  const ctx = useContext(DataContext);
  return ctx === null ? <CodeBlock caption={caption} text={connectPrompt(null)} /> : <ActiveProjectPrompt caption={caption} />;
}
