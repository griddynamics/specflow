---
type: Design
title: "GD Liquid Glass logos"
description: "The Grid Dynamics full-colour SVG logos per theme and the mark-only SVG, and when to use each"
status: stable
tags: [design-system]
---
Grid Dynamics logos. Since D21 the system ships the full-colour SVG logos seen on the Grid Dynamics website, one folder per theme, plus a mark-only SVG derived from them.

## Full-colour SVGs (use these)
- `Full color, black/SVG/` is for the **light** theme (dark `#333333` wordmark); `Full color, white/SVG/` is for the **dark** theme (white wordmark). The mark has the same five colours in both (`#F29100 #EA5B25 #0069B3 #A9AA00 #62B8E7`).
- Variants in each folder: `horizontal` (mark + wordmark, 2192×400: NavBar, Sidebar, sign-in), `vertical` (mark above the wordmark), each also as `-prepare` (with the "PREPARE TO GROW" tagline), `-tm` (with the trademark sign) and `-prepare-tm`. Use the plain `-horizontal` file in product UI. The files are the owner's originals: do not optimise or reformat them (the app copies are compared byte for byte).
- `full-color-black-mark.svg` and `full-color-white-mark.svg`: the mark alone, no "Grid Dynamics" text (collapsed Sidebar, favicon). The two files are byte-identical (the mark does not change with the theme); one lives in each folder so each theme folder is complete. Derivation, deterministic: from `full-color-<c>-vertical.svg` keep the six `<path>` elements whose fill is one of the five mark colours, verbatim and in source order; drop the 12 wordmark paths (`#333333` / `white`), all `<g>`, `<defs>` and clip paths; write `<svg xmlns="http://www.w3.org/2000/svg" width="654" height="654" viewBox="313 -4 654 654" fill="none">`, the paths, `</svg>` (2,111 B). Rendered at the same viewBox it equals the vertical logo's mark pixel for pixel.

## Theme pair rule
Render both theme files and let the CSS show one: `.gdg-logo-light` on the black-folder file, `.gdg-logo-dark` on the white-folder file. `bundle.css` carries the rule (`.gdg-logo-dark:not([data-theme="dark"] *), [data-theme="dark"] .gdg-logo-light { display: none; }`). Never invert or recolour these logos with CSS filters. Sidebar and NavBar do this themselves (`logoSrc`, `logoDarkSrc`, `markSrc`).

## Favicon
`app/icon.svg` of the app is the mark SVG, byte for byte. `app/favicon.ico` is a PNG-in-ICO with three frames (16, 32, 48 px) rendered from the same mark on a transparent ground (sharp, `fit: contain`).

Never commit `.DS_Store` files from these folders (the repository `.gitignore` lists them).
