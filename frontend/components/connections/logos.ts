// FE-CON-02.AC4, FE-CON-01.AC1 (O6, GAPS "## P14"): vendor marks come UNCHANGED from the `simple-icons` package (one-colour marks,
// path data and brand hex from the package; nothing is drawn, traced or recoloured). Only the marks the connectors use are named
// imports (the package has `sideEffects: false`, so the other ~3000 icons are tree-shaken). The mark is turned into an SVG data URI
// for `ConnectorCard logoSrc` (an <img>): no request, no hot-linking.
// Slack is not in simple-icons 16.34.0 (removed at the owner's request): it stays a text-only tile (the card's monogram).
import {
  siCircleci,
  siConfluence,
  siDatadog,
  siFigma,
  siGithub,
  siGitlab,
  siGoogledrive,
  siJira,
  siLinear,
  siNotion,
  siSentry,
} from "simple-icons";

export interface Mark {
  title: string;
  hex: string;
  path: string;
}

/** Slug (MOCKS §9 `logoSlug`) to the package's icon object. */
const MARKS: Record<string, Mark> = {
  github: siGithub,
  googledrive: siGoogledrive,
  jira: siJira,
  confluence: siConfluence,
  datadog: siDatadog,
  circleci: siCircleci,
  figma: siFigma,
  gitlab: siGitlab,
  linear: siLinear,
  notion: siNotion,
  sentry: siSentry,
};

export function markFor(slug: string): Mark | undefined {
  return Object.hasOwn(MARKS, slug) ? MARKS[slug] : undefined;
}

/** The mark's own SVG (package path, package brand colour, 24x24 viewBox) as a data URI; `undefined` for a text-only tile. */
export function logoSrc(slug: string): string | undefined {
  const m = markFor(slug);
  if (!m) return undefined;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#${m.hex}" d="${m.path}"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
