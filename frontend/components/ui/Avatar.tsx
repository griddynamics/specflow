// FE-DS-02 (R5): port of `Avatar` and `AvatarGroup` in bundle.js. Initials, optional photo, or the SpecBot sparkle.
import { Icon } from "./Icon";
import { cx, truthy } from "./utils";

export type AvatarSize = "sm" | "md" | "lg";

export interface AvatarProps {
  name: string;
  src?: string;
  size?: AvatarSize;
  kind?: "bot";
  tone?: "yellow" | "data" | "queue" | "neutral";
  title?: string | false;
  className?: string;
}

const TONES = ["yellow", "data", "queue", "neutral"] as const;

/** Up to two initials, like `initials` in bundle.js ("@specbot" -> "S"). */
export function initials(name: string): string {
  return (name || "?")
    .replace(/^@/, "")
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

/** Stable tone from the name, like `toneOf` in bundle.js. */
export function toneOf(name: string): (typeof TONES)[number] {
  let s = 0;
  for (let i = 0; i < (name || "").length; i++) s = (s + name.charCodeAt(i)) % 997;
  return TONES[s % TONES.length];
}

const AVATAR_PX = { sm: 28, md: 40, lg: 56 } as const;
const BOT_ICON_PX = { sm: 13, md: 16, lg: 20 } as const;

function AvatarFace({ name, src, size, bot }: Readonly<{ name: string; src?: string; size: AvatarSize; bot: boolean }>) {
  if (truthy(src)) return <img src={src} alt="" width={AVATAR_PX[size]} height={AVATAR_PX[size]} />; // the CSS sizes it to the avatar
  if (bot) return <Icon name="sparkle" size={BOT_ICON_PX[size]} />;
  return <>{initials(name)}</>;
}

export function Avatar({ name, src, size = "md", kind, tone, title, className }: Readonly<AvatarProps>) {
  const bot = kind === "bot";
  return (
    <span
      className={cx("gdg-avatar", `gdg-avatar--${size}`, `gdg-avatar--${bot ? "bot" : (tone ?? toneOf(name))}`, className)}
      title={title === false ? undefined : (title ?? name)}
      role="img"
      aria-label={name}
    >
      <AvatarFace name={name} src={src} size={size} bot={bot} />
    </span>
  );
}
