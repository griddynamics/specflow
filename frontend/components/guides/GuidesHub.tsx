// MCP-M10 (FR-MCP-UI-01, 02): /guides. The header is the "Guides" breadcrumb (shell header row); the h1 is visually hidden, like Connections.
// The lead and the three topic cards follow; each OPEN link goes to its topic.
import { Card } from "@/components/ui/Card";
import { ButtonLink } from "@/components/ui/ButtonLink";
import { Icon } from "@/components/ui/Icon";
import { Tag } from "@/components/ui/Tag";
import { HUB, OPEN_LABEL, TOPIC_CARDS } from "@/lib/guides/content";
import { routesFor } from "@/lib/routes";
import { PageTitle } from "@/components/ui/TopBar";
import { GuideCrumbs } from "./GuideCrumbs";
import "./guides.css";

const routes = routesFor("production");

export function GuidesHub() {
  return (
    <div className="guides" data-testid="guides-hub">
      <GuideCrumbs />
      <PageTitle className="shell-sr">{HUB.title}</PageTitle>
      <p className="body-lg guides-muted guides-lead">{HUB.lead}</p>
      <ul className="guides-grid" aria-label="Guides">
        {TOPIC_CARDS.map((c) => (
          <li key={c.topic}>
            <Card variant="strong" eyebrow={c.eyebrow} title={c.title} className="guides-topic">
              <div className="guides-topic__body">
                <Icon name={c.icon} size={24} />
                <p className="body guides-muted">{c.summary}</p>
                <div>
                  <Tag>{c.tag}</Tag>
                </div>
                <ButtonLink
                  href={routes.guides(c.topic)}
                  variant="glass"
                  arrow
                  aria-label={`${OPEN_LABEL} ${c.title}`}
                  className="guides-topic__open"
                >
                  {OPEN_LABEL}
                </ButtonLink>
              </div>
            </Card>
          </li>
        ))}
      </ul>
    </div>
  );
}
