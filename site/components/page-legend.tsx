import type { JSX, ReactNode } from "react";

const LEGEND: { label: string; icon: ReactNode }[] = [
  {
    label: "Documents",
    icon: (
      <>
        <path d="M5 2h7l4 4v12H5z M12 2v4h4" />
        <path className="l" d="M8 10h6M8 13h5" />
      </>
    ),
  },
  {
    label: "Confluence",
    icon: (
      <>
        <path d="M4 3h12v15H4z" />
        <path className="f" d="M4 3h12v4H4z" />
        <path className="l" d="M7 11h6M7 14h4" />
      </>
    ),
  },
  {
    label: "Jira",
    icon: (
      <>
        <rect x="2" y="5" width="16" height="11" rx="2" />
        <path d="M5 10.5l1.6 1.6L9.5 9" />
        <path className="l" d="M11.5 9.5h4M11.5 12.5h3" />
      </>
    ),
  },
  {
    label: "Slack",
    icon: (
      <>
        <path d="M3 4h14v9H8l-4 3.5V13H3z" />
        <path className="l" d="M6 7.5h8M6 10h5" />
      </>
    ),
  },
  {
    label: "Meeting notes",
    icon: (
      <>
        <path d="M4 4h12v14H4z" />
        <path className="l" d="M7 2v4M10 2v4M13 2v4M7 10h6M7 13h5" />
      </>
    ),
  },
  {
    label: "Recordings",
    icon: (
      <>
        <rect x="2" y="4" width="16" height="12" rx="2" />
        <path className="f" d="M8 7.5v5l4.5-2.5z" />
      </>
    ),
  },
];

/** Names the kinds of page while they are on screen; the caption takes over once they move. */
export function PageLegend({ hidden }: Readonly<{ hidden: boolean }>): JSX.Element {
  return (
    <span className="ut-legend" aria-label="Kinds of pages" hidden={hidden}>
      {LEGEND.map(({ label, icon }) => (
        <span key={label}>
          <svg viewBox="0 0 20 20" aria-hidden="true">
            {icon}
          </svg>
          {label}
        </span>
      ))}
    </span>
  );
}
