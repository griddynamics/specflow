// FE-DS-02 (R5): port of `Skeleton` in bundle.js. Widths and heights are caller props (layout, not styling).
export interface SkeletonProps {
  variant?: "text" | "block" | "circle";
  lines?: number;
  width?: number | string;
  height?: number | string;
}

export function Skeleton({ variant = "text", lines = 3, width, height }: Readonly<SkeletonProps>) {
  if (variant === "text") {
    return (
      <span className="gdg-skel-group" aria-hidden>
        {Array.from({ length: lines }, (_, i) => (
          <span key={i} className="gdg-skel gdg-skel--line" style={{ width: i === lines - 1 ? "62%" : "100%" }} />
        ))}
      </span>
    );
  }
  return <span className={`gdg-skel gdg-skel--${variant}`} style={{ width, height }} aria-hidden />;
}
