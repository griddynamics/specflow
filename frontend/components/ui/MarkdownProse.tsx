// FE-FILES-02, FE-DOC-01, FE-DS-02 (TECH-SPECS §11): Markdown source rendered into <Prose> with react-markdown + remark-gfm.
// rehype-slug gives headings the ids MOCKS.md expects (GitHub slugs), so DocLinks and Citations anchors resolve.
// Raw HTML in the source is not rendered (react-markdown default). Server-compatible.
// Safe defaults (P05 fix1): `a` and `img` overrides below. P11 adds the real document-link rewriting through `components`
// (caller overrides win over these defaults).
import type { ReactNode, Ref } from "react";
import Markdown, { type Components } from "react-markdown";
import rehypeSlug from "rehype-slug";
import remarkGfm from "remark-gfm";
import { Prose } from "./Prose";
import { truthy } from "./utils";

export interface MarkdownProseProps {
  source: string;
  paper?: boolean;
  className?: string;
  innerRef?: Ref<HTMLElement>;
  /** Element overrides (for example link handling in the Files view). They replace the defaults below. */
  components?: Components;
}

const REMARK = [remarkGfm];
const REHYPE = [rehypeSlug];

/** How a link target is rendered by default: `external` (http/https, new tab), `hash` (same page), or `plain` (no navigation). */
export function linkKind(href: string | undefined): "external" | "hash" | "plain" {
  if (!truthy(href)) return "plain";
  if (href.startsWith("#")) return "hash";
  return /^https?:\/\//i.test(href) ? "external" : "plain";
}

/** Image sources allowed by default: same-origin relative paths and data:image URLs. No remote hosts (no tracking pixels, R10). */
export function imageSrcAllowed(src: string | undefined): boolean {
  if (!truthy(src)) return false;
  if (/^data:image\//i.test(src)) return true;
  if (src.startsWith("//") || /^[a-z][a-z0-9+.-]*:/i.test(src)) return false;
  return true;
}

const DEFAULTS: Components = {
  a({ href, children }) {
    const kind = linkKind(href);
    if (kind === "external")
      return (
        <a href={href} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      );
    if (kind === "hash") return <a href={href}>{children}</a>;
    return <a>{children}</a>; // relative or unsafe target: styled like a link, goes nowhere (P11 rewrites document links)
  },
  img({ src, alt, width, height }): ReactNode {
    const s = typeof src === "string" ? src : undefined;
    // The width and height the source declares are kept (they reserve the box before the file loads). A plain Markdown image
    // declares none: its size comes from the loaded file (`.gdg-prose img` caps the width), and no attribute is written.
    return imageSrcAllowed(s) ? <img src={s} alt={alt ?? ""} width={width} height={height} /> : <span>{alt}</span>;
  },
};

export function MarkdownProse({ source, paper, className, innerRef, components }: Readonly<MarkdownProseProps>) {
  return (
    <Prose paper={paper} className={className} innerRef={innerRef}>
      <Markdown remarkPlugins={REMARK} rehypePlugins={REHYPE} components={{ ...DEFAULTS, ...components }}>
        {source}
      </Markdown>
    </Prose>
  );
}
