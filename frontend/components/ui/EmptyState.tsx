// FE-DS-02 (R5): port of `EmptyState` in bundle.js.
import type { ReactNode } from "react";
import { GlassSurface } from "./GlassSurface";
import { Icon, type IconName } from "./Icon";
import { truthy } from "./utils";

export interface EmptyStateProps {
  title: string;
  icon?: IconName;
  media?: ReactNode;
  actions?: ReactNode;
  variant?: "regular" | "strong";
  children?: ReactNode;
}

export function EmptyState({ title, icon, media, actions, variant = "strong", children }: Readonly<EmptyStateProps>) {
  return (
    <GlassSurface variant={variant} className="gdg-empty">
      {icon ? (
        <span className="gdg-empty__icon">
          <Icon name={icon} size={28} />
        </span>
      ) : (
        (media ?? null)
      )}
      <h3 className="gdg-empty__title">{title}</h3>
      {truthy(children) ? <p className="gdg-empty__body">{children}</p> : null}
      {truthy(actions) ? <div className="gdg-empty__actions">{actions}</div> : null}
    </GlassSurface>
  );
}
