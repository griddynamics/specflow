import { PageTitle } from "@/components/ui/TopBar";

// MCP-M10: the header of a topic page: title and lead (the breadcrumb already names the topic). The h1 is the page title (`title-2`). The hub has no such block: its
// header is the breadcrumb and its h1 is visually hidden (GuidesHub).
export function GuideHeader({ title, lead }: Readonly<{ title: string; lead: string }>) {
  return (
    <header className="guides-header">
      <PageTitle className="title-2">{title}</PageTitle>
      <p className="body-lg guides-muted">{lead}</p>
    </header>
  );
}
