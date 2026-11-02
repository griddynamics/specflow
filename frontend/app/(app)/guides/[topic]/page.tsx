// MCP-M10 (FR-MCP-UI-01..08, E7): /guides/{topic}. Only the three topics exist: `generateStaticParams` lists them and `dynamicParams = false`
// answers 404 for any other; `notFound()` right after `params` is the second guard. `params` and `searchParams` are Promises (Next.js 16).
// The MCP topic reads the server-only config and passes plain props (E11).
import { notFound } from "next/navigation";
import { BestPractices } from "@/components/guides/BestPractices";
import { GuideCrumbs } from "@/components/guides/GuideCrumbs";
import { McpGuide } from "@/components/guides/McpGuide";
import { SpecflowGuide } from "@/components/guides/SpecflowGuide";
import { parseAgent } from "@/lib/guides/agent";
import { topicTitle } from "@/lib/guides/content";
import { guideConfig } from "@/lib/guides/guide-config";
import { GUIDE_TOPICS, parseGuideTopic } from "@/lib/routes";

export const dynamicParams = false;

export function generateStaticParams() {
  return GUIDE_TOPICS.map((topic) => ({ topic }));
}

export default async function Page({
  params,
  searchParams,
}: Readonly<{ params: Promise<{ topic: string }>; searchParams: Promise<Record<string, string | string[] | undefined>> }>) {
  const topic = parseGuideTopic((await params).topic);
  if (topic === null) notFound();
  const agent = parseAgent((await searchParams).agent);
  return (
    <div className="guides-page">
      <GuideCrumbs title={topicTitle(topic)} />
      {topic === "specflow-101" ? <SpecflowGuide /> : null}
      {topic === "best-practices" ? <BestPractices /> : null}
      {topic === "mcp-server" ? <McpGuide {...guideConfig()} agent={agent} /> : null}
    </div>
  );
}
