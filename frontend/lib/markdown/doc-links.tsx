"use client";
// FE-MC-08, FE-MC-05, TECH-SPECS §1.4: the `a` override for MarkdownProse (`components={{ a }}`), moved here from P09 (DECISIONS P09-3).
//  - relative document links (`../04-architecture/x.md#section`) open the file inside Files at the section (a real `href` from
//    `filesLink`, so "open in new tab" works; a plain click calls `onOpen`);
//  - `#hash` links scroll the current page or pane to the heading;
//  - `http(s)` links open in a new tab with `rel="noopener noreferrer"`;
//  - everything else (other schemes, unknown documents, files that are not Markdown) renders as plain text (TECH-SPECS §1.4).
import type { MouseEvent } from "react";
import type { Components } from "react-markdown";
import type { DocRef } from "@/lib/domain/common";
import type { RouteBuilder } from "@/lib/routes";
import { filesLink, resolveDocHref } from "./links";
import { scrollToSection } from "./scroll";

export interface DocLinkOptions {
  /** Path of the document the Markdown belongs to (relative links resolve against its folder). */
  currentPath: string;
  projectId: string;
  routes: Pick<RouteBuilder, "file">;
  /** Called on a plain click on a document link; the caller changes the URL (see `openDocRef`). */
  onOpen: (ref: DocRef) => void;
  /** When given, a link to a path it rejects renders inert (the file is not in the project). */
  isKnown?: (path: string) => boolean;
}

const plainClick = (e: MouseEvent) => e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;

/** Build once per document (`useMemo`): a new object means new component types and a remount of the whole Markdown tree. */
export function docLinkComponents(o: DocLinkOptions): Pick<Components, "a"> {
  return {
    a({ href, children, title, id }) {
      if (href === undefined || href === "") return <>{children}</>;
      if (/^https?:\/\//i.test(href))
        return (
          <a href={href} id={id} title={title} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        );
      if (href.startsWith("#")) {
        const target = decodeHash(href);
        return (
          <a
            href={href}
            id={id}
            title={title}
            onClick={(e) => {
              if (!plainClick(e)) return;
              e.preventDefault();
              scrollToSection(target);
            }}
          >
            {children}
          </a>
        );
      }
      const ref = resolveDocHref(o.currentPath, href);
      if (!ref || (o.isKnown && !o.isKnown(ref.path))) return <>{children}</>; // plain text (TECH-SPECS §1.4)
      return (
        <a
          href={filesLink(o.routes, o.projectId, ref.path, ref.anchor)}
          id={id}
          title={title}
          onClick={(e) => {
            if (!plainClick(e)) return;
            e.preventDefault();
            o.onOpen(ref);
          }}
        >
          {children}
        </a>
      );
    },
  };
}

function decodeHash(href: string): string {
  const raw = href.slice(1);
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}
