/**
 * OKLCH -> WCAG relative luminance / contrast ratio.
 *
 * `contrast.mjs` needs a real WCAG 2.1 contrast check on a palette declared
 * in OKLCH. No widely installed colour library converts OK-LCH (CIE-LCH is a
 * different space), so this is the small custom fallback: Björn Ottosson's
 * published OKLab <-> linear-sRGB matrices (the reference implementation
 * every OKLCH-aware tool, including browsers, implements), pinned by unit
 * tests against the achromatic reference points (`oklch(1 0 0)` = white,
 * `oklch(0 0 0)` = black) so a transcription slip fails loudly.
 *
 * WCAG's relative-luminance formula (WCAG 2.1 SC 1.4.3) wants linear-light
 * sRGB channels; the OKLab -> linear-sRGB matrix already produces exactly
 * that, so no gamma re-encoding step is needed for a contrast ratio.
 */

const OKLCH_RE = /^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+%?))?\s*\)$/i;

/** Parses a CSS `oklch(L C H)` (or `oklch(L C H / A)`) string into
 * `{l, c, h}` (`l` 0..1, `c` chroma, `h` degrees), or `null` if the text is
 * not an `oklch()` function (a raw hex or `var()` reference, etc.). */
function parseOklch(text) {
  const m = OKLCH_RE.exec(text.trim());
  if (!m) return null;
  return { l: Number(m[1]), c: Number(m[2]), h: Number(m[3]) };
}

/** OKLCH -> linear-light sRGB, each channel unclamped (a colour outside the
 * sRGB gamut yields a value outside 0..1; luminance clamps it). */
function oklchToLinearSrgb({ l, c, h }) {
  const hRad = (h * Math.PI) / 180;
  const a = c * Math.cos(hRad);
  const b = c * Math.sin(hRad);

  const l_ = l + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = l - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = l - 0.0894841775 * a - 1.291485548 * b;

  const l3 = l_ ** 3;
  const m3 = m_ ** 3;
  const s3 = s_ ** 3;

  return [
    +4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
    -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
    -0.0041960863 * l3 - 0.7034186147 * m3 + 1.707614701 * s3,
  ];
}

/** WCAG 2.1 relative luminance (0 black .. 1 white) of an `oklch()` string. */
export function relativeLuminance(oklchText) {
  const parsed = parseOklch(oklchText);
  if (!parsed) return null;
  const [r, g, b] = oklchToLinearSrgb(parsed).map((v) => Math.min(1, Math.max(0, v)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio (1..21) between two `oklch()` strings, or `null` if
 * either fails to parse. */
export function contrastRatio(oklchA, oklchB) {
  const la = relativeLuminance(oklchA);
  const lb = relativeLuminance(oklchB);
  if (la === null || lb === null) return null;
  const [lighter, darker] = la >= lb ? [la, lb] : [lb, la];
  return (lighter + 0.05) / (darker + 0.05);
}
