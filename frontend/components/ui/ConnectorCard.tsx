// FE-CON-01, FE-CON-02, FE-DS-02 (R5): port of `ConnectorCard` in bundle.js. Vendor logo (or a monogram placeholder), a status
// Tag with the word (never colour alone), name, category, description, meta and the action button. `logoSrc` comes from the
// caller (P14: simple-icons); no logo is fetched here.
import { initials } from "./Avatar";
import { Button } from "./Button";
import { GlassSurface } from "./GlassSurface";
import { Tag, type TagProps } from "./Tag";
import { cx, nonEmpty, truthy } from "./utils";

export interface ConnectorCardProps {
  name: string;
  category: string;
  description?: string;
  status?: "connected" | "attention" | "off" | "checking";
  logoSrc?: string;
  meta?: string;
  actionLabel?: string;
  onAction?: () => void;
  variant?: "regular" | "strong";
}

type ConnectorStatus = NonNullable<ConnectorCardProps["status"]>;

const CONN: Record<ConnectorStatus, [NonNullable<TagProps["tone"]>, string]> = {
  connected: ["success", "Connected"],
  attention: ["warning", "Needs attention"],
  off: ["neutral", "Not connected"],
  checking: ["pending", "Checking"],
};

/** The button text when the card sets none. */
function defaultAction(off: boolean, status: ConnectorStatus | undefined): string {
  if (off) return "Connect";
  return status === "attention" ? "Fix" : "Manage";
}

export function ConnectorCard({
  name,
  category,
  description,
  status,
  logoSrc,
  meta,
  actionLabel,
  onAction,
  variant,
}: Readonly<ConnectorCardProps>) {
  const st = CONN[status ?? "off"];
  const off = status === "off" || !status;
  return (
    <GlassSurface variant={variant ?? "strong"} className={cx("gdg-conn", `is-${status ?? "off"}`)}>
      <div className="gdg-conn__top">
        <span className="gdg-conn__logo" data-testid="conn-logo">
          {truthy(logoSrc) ? <img src={logoSrc} alt="" width={30} height={30} /> : <span className="gdg-conn__mono">{initials(name)}</span>}
        </span>
        <Tag tone={st[0]}>{st[1]}</Tag>
      </div>
      <h3 className="gdg-conn__name">{name}</h3>
      <p className="gdg-conn__cat" data-testid="conn-category">
        {category}
      </p>
      {truthy(description) ? (
        <p className="gdg-conn__desc" data-testid="conn-description">
          {description}
        </p>
      ) : null}
      <div className="gdg-conn__foot">
        {truthy(meta) ? <span className="gdg-conn__meta">{meta}</span> : <span />}
        <Button variant={off ? "glass" : "plain"} size="sm" onClick={onAction}>
          {nonEmpty(actionLabel) ?? defaultAction(off, status)}
        </Button>
      </div>
    </GlassSurface>
  );
}
