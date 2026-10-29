// Stand-in for a component library's props (a design system's ButtonProps declares `onClick`): the
// activation props arrive from a declaration file, not from the project's own sources.
export interface LibraryButtonProps {
  onClick?: () => void;
  ariaLabel?: string;
  children?: unknown;
}
export interface LibraryLinkProps {
  to: string;
  ariaLabel?: string;
  children?: unknown;
}
