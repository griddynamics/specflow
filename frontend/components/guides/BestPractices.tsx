// MCP-M10: Best practices. Three strong Cards, each a list of hairline rows: icon, advice (headline), reason (body, ink-2).
import { Card } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { BEST_PRACTICES } from "@/lib/guides/content";
import { GuideHeader } from "./GuideHeader";
import "./guides.css";

export function BestPractices() {
  return (
    <div className="guides" data-testid="guide-best-practices">
      <GuideHeader title={BEST_PRACTICES.title} lead={BEST_PRACTICES.lead} />
      <div className="guides-stack">
        {BEST_PRACTICES.groups.map((g) => (
          <Card key={g.title} variant="strong" eyebrow={g.eyebrow} title={g.title}>
            <ul className="guides-tips">
              {g.tips.map((t) => (
                <li key={t.advice} className="guides-tip">
                  <Icon name="check-circle" size={20} />
                  <div>
                    <p className="headline">{t.advice}</p>
                    <p className="body guides-muted">{t.reason}</p>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        ))}
      </div>
    </div>
  );
}
