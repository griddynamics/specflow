import type { ReactNode, CSSProperties, InputHTMLAttributes, ButtonHTMLAttributes } from 'react';

/** The Liquid Glass material. Every other component is built on it. */
export interface GlassSurfaceProps {
  /** regular = glass-fill (ink text only); strong = glass-fill-strong (any text); subtle = clear inset; tint = yellow lens. */
  variant?: 'regular' | 'strong' | 'subtle' | 'tint';
  /** Element to render. Default 'div'. */
  as?: string;
  /** Adds the fine vertical ribbing from the brand artwork. Large hero glass only. */
  ribbed?: boolean;
  /** Uses shadow-glass-float for layers above other glass. */
  floating?: boolean;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** primary = yellow capsule (one per view); glass = secondary; plain = text with yellow underline. */
  variant?: 'primary' | 'glass' | 'plain';
  size?: 'sm' | 'md' | 'lg';
  /** Trailing arrow, as on the website CTAs. */
  arrow?: boolean;
  children: ReactNode;
}

export interface CardProps {
  variant?: 'regular' | 'strong';
  /** UPPERCASE kicker. */
  eyebrow?: string;
  title?: string;
  children?: ReactNode;
  /** Actions or tags row. */
  footer?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

export interface NavBarProps {
  links: { label: string; href?: string; current?: boolean }[];
  /** Right-side slot, usually a primary Button ("LET'S TALK"). */
  action?: ReactNode;
  /** Full-colour logo for the light theme. Defaults to the GD horizontal logo. */
  logoSrc?: string;
  /** Full-colour logo for the dark theme. Defaults to the white-wordmark horizontal logo. */
  logoDarkSrc?: string;
}

export interface TabBarProps {
  tabs: { value: string; label: string }[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  label?: string;
}

export interface SegmentedControlProps {
  options: { value: string; label: string }[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  /** Accessible group name. */
  label: string;
}

export interface SwitchProps {
  label?: string;
  checked?: boolean;
  defaultChecked?: boolean;
  onChange?: (checked: boolean) => void;
  disabled?: boolean;
}

export interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  help?: string;
  /** Error message; replaces help and turns the field danger. */
  error?: string;
}

export interface TagProps {
  tone?: 'neutral' | 'accent' | 'info' | 'success' | 'danger';
  children: ReactNode;
}

export interface StatCardProps {
  value: string;
  /** Compact unit at half size: %, +, wk, ×. */
  unit?: string;
  label: string;
  variant?: 'regular' | 'strong';
}

export interface SheetProps {
  open?: boolean;
  title?: string;
  children?: ReactNode;
  actions?: ReactNode;
  onClose?: () => void;
}

/** Phosphor Regular glyph (MIT), the flagged substitute UI icon set. `GDGlass.Icon.names` lists what ships. */
export interface IconProps {
  name: 'house' | 'squares-four' | 'tree-structure' | 'flow-arrow' | 'stack' | 'folder-simple' | 'users' | 'chart-bar' | 'gear-six' | 'question'
    | 'sign-out' | 'caret-double-left' | 'caret-double-right' | 'database' | 'cloud' | 'cube' | 'hard-drives' | 'queue' | 'user' | 'globe'
    | 'lock-simple' | 'lightning' | 'plug' | 'cpu' | 'browser' | 'device-mobile' | 'robot' | 'sparkle' | 'shield-check' | 'git-branch'
    | 'arrows-split' | 'cloud-arrow-up' | 'magnifying-glass' | 'bell' | 'plus' | 'list-checks' | 'flag-checkered' | 'play-circle'
    | 'caret-down' | 'caret-right' | 'caret-up' | 'x' | 'check' | 'check-circle' | 'x-circle' | 'warning-circle' | 'paper-plane-right' | 'file-text'
    | 'file-md' | 'folder-open' | 'upload-simple' | 'chat-circle-text' | 'pencil-simple' | 'at' | 'link-simple' | 'arrow-counter-clockwise' | 'dots-three'
    | 'paperclip' | 'arrow-square-out' | 'funnel-simple' | 'minus' | 'corners-out' | 'kanban' | 'clock';
  /** 16, 20 (default) or 24. */
  size?: number;
  /** Accessible name when the icon stands alone; omit for decorative icons. */
  label?: string;
  className?: string;
}

export type SidebarEntry =
  | { value: string; label: string; icon: IconProps['name']; badge?: number | string }
  | { section: string };

export interface SidebarProps {
  items: SidebarEntry[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  /** Icon rail (76px) instead of icons + text (264px). */
  collapsed?: boolean;
  defaultCollapsed?: boolean;
  onToggle?: (collapsed: boolean) => void;
  user?: { name: string; role?: string };
  /** Expanded logo for the light theme. Defaults to the full-colour horizontal logo. */
  logoSrc?: string;
  /** Expanded logo for the dark theme. Defaults to the full-colour horizontal logo with a white wordmark. */
  logoDarkSrc?: string;
  /** Mark for the collapsed rail, both themes. Defaults to the full-colour GD mark. */
  markSrc?: string;
  label?: string;
  className?: string;
  style?: CSSProperties;
}

export type DiagramNodeKind = 'service' | 'app' | 'mobile' | 'gateway' | 'datastore' | 'storage' | 'queue' | 'ai' | 'external' | 'actor'
  | 'cloud' | 'security' | 'compute' | 'integration';

export interface DiagramNodeProps {
  kind?: DiagramNodeKind;
  title: string;
  /** Technology line, e.g. "PostgreSQL 15". */
  subtitle?: string;
  icon?: IconProps['name'];
  kindLabel?: string;
  badge?: string;
  /** Position inside a DiagramCanvas (px). Omit to render inline. */
  x?: number; y?: number; w?: number; h?: number;
  selected?: boolean;
  dim?: boolean;
  onClick?: () => void;
}

export type EdgeKind = 'sync' | 'async' | 'data' | 'flow';
export type Side = 'top' | 'right' | 'bottom' | 'left';

export interface DiagramEdgeSpec {
  from: string;
  to: string;
  kind?: EdgeKind;
  label?: string;
  fromSide?: Side; toSide?: Side;
  /** Shift the anchor along its side (px) to separate parallel lines. */
  fromOffset?: number; toOffset?: number;
  /** x (horizontal routes) or y (vertical routes) of the middle segment. */
  bend?: number;
  bidirectional?: boolean;
  /** Draw as selected / critical path. */
  highlight?: boolean;
  /** false hides the arrowhead. */
  arrow?: boolean;
}

export interface DiagramGroupProps {
  label: string;
  icon?: IconProps['name'];
  x: number; y: number; w: number; h: number;
  /** 'plain' = solid hairline instead of the dashed boundary. */
  tone?: 'boundary' | 'plain';
}

export interface ArchitectureDiagramProps {
  nodes: (DiagramNodeProps & { id: string; x: number; y: number })[];
  edges: DiagramEdgeSpec[];
  groups?: DiagramGroupProps[];
  width: number;
  height: number;
  /** Accessible summary of what the diagram shows. */
  label: string;
  legend?: boolean;
  legendKinds?: EdgeKind[];
  legendPosition?: 'bottom-left' | 'bottom-right' | 'top-left' | 'top-right';
  selectable?: boolean;
  defaultSelected?: string;
  grid?: boolean;
}

export interface ProcessFlowProps {
  lanes?: { id: string; label: string; icon?: IconProps['name'] }[];
  steps: { id: string; type?: 'start' | 'step' | 'subprocess' | 'decision' | 'end'; title: string; owner?: string; lane?: string; col: number }[];
  edges: DiagramEdgeSpec[];
  laneHeight?: number;
  colWidth?: number;
  width?: number;
  height?: number;
  numbered?: boolean;
  selectable?: boolean;
  defaultSelected?: string;
  label: string;
}

/* ---------- App components (part 3) ---------- */
export interface Person { id: string; name: string; email?: string; role?: string; handle?: string; kind?: 'bot'; src?: string }

export interface AvatarProps { name: string; src?: string; size?: 'sm' | 'md' | 'lg'; kind?: 'bot'; tone?: 'yellow' | 'data' | 'queue' | 'neutral'; title?: string | false }
export interface AvatarGroupProps { people: { name: string; src?: string; kind?: 'bot' }[]; max?: number; size?: 'sm' | 'md' | 'lg' }

export interface MenuItem { label?: string; icon?: IconProps['name']; avatar?: AvatarProps; description?: string; checked?: boolean; tone?: 'danger'; shortcut?: string; disabled?: boolean; onSelect?: (item: MenuItem) => void; section?: string; divider?: boolean }
export interface MenuProps {
  /** Content of the default pill trigger, or a render function for a custom trigger. */
  trigger: ReactNode | ((t: { open: boolean; toggle: () => void; id: string }) => ReactNode);
  items: MenuItem[];
  header?: ReactNode;
  align?: 'start' | 'end';
  label?: string;
  onSelect?: (item: MenuItem) => void;
  defaultOpen?: boolean;
  triggerClassName?: string;
}

export interface TopBarProps {
  projects: { id: string; name: string; meta?: string }[];
  projectId?: string;
  onProjectChange?: (id: string) => void;
  onNew?: () => void;
  newLabel?: string;
  user: { name: string; email?: string };
  /** Defaults to Settings, Reset demo, Sign out. */
  userItems?: MenuItem[];
  /** Left side: page title or Breadcrumbs. */
  children?: ReactNode;
}

export interface ModalProps {
  open?: boolean;
  title: string;
  eyebrow?: string;
  description?: string;
  onClose?: () => void;
  footer?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  children?: ReactNode;
  /** Position inside the nearest positioned parent instead of the viewport (previews, embedded demos). */
  contained?: boolean;
}

export interface SelectProps { options: { value: string; label: string; description?: string }[]; value?: string; defaultValue?: string; onChange?: (v: string) => void; label?: string; icon?: IconProps['name']; placeholder?: string }
export interface SearchFieldProps { value?: string; defaultValue?: string; onChange?: (v: string) => void; placeholder?: string; label?: string }
export interface PeoplePickerProps { people: Person[]; value?: string[]; defaultValue?: string[]; onChange?: (ids: string[]) => void; label?: string; help?: string; placeholder?: string; defaultOpen?: boolean }

export interface LinkFieldProps {
  label: string;
  placeholder?: string;
  icon?: IconProps['name'];
  defaultValue?: string;
  /** Resolve true for access, false for no access. Defaults to GDGlass.simulateAccess (fails for URLs containing "private"). */
  onTest?: (url: string) => boolean | Promise<boolean>;
  onStatus?: (s: 'ok' | 'fail') => void;
  onRemove?: () => void;
  help?: string; okText?: string; failText?: string; testLabel?: string;
  defaultStatus?: 'idle' | 'checking' | 'ok' | 'fail';
}

export interface OptionCardsProps {
  type?: 'choice' | 'yesno';
  options?: { value: string; label: string; description?: string }[];
  /** Adds a lettered "Other" card with a text box (value "__other"). */
  allowOther?: boolean; otherLabel?: string; otherPlaceholder?: string; onOtherChange?: (text: string) => void;
  value?: string; defaultValue?: string; onChange?: (v: string) => void;
  yesLabel?: string; noLabel?: string;
  label: string;
}

export interface DropzoneProps { onFiles?: (files: File[]) => void; title?: string; hint?: string; buttonLabel?: string; accept?: string; multiple?: boolean; primary?: boolean; compact?: boolean; icon?: IconProps['name'] }
export interface FileRowProps { name: string; size?: number | string; status?: 'ready' | 'uploading' | 'processing' | 'done' | 'error'; progress?: number; onRemove?: () => void; icon?: IconProps['name']; doneText?: string; errorText?: string }
export interface ProgressBarProps { value?: number; label?: string; indeterminate?: boolean; tone?: 'danger' }
export interface SpinnerProps { size?: number; label?: string }
export interface SkeletonProps { variant?: 'text' | 'block' | 'circle'; lines?: number; width?: number | string; height?: number | string }
export interface EmptyStateProps { title: string; icon?: IconProps['name']; media?: ReactNode; actions?: ReactNode; variant?: 'regular' | 'strong'; children?: ReactNode }
export interface TooltipProps { label: string; side?: 'top' | 'right' | 'bottom' | 'left'; children: ReactNode }
export interface ToastProps { title: string; message?: string; tone?: 'neutral' | 'success' | 'info' | 'danger'; action?: ReactNode; onClose?: () => void }
/** useToast() returns (toast: ToastProps & { duration?: number }) => void; needs <ToastProvider> above. */
export interface ToastProviderProps { children?: ReactNode }

export interface BreadcrumbsProps { items: { label: string; onClick?: () => void }[]; label?: string }
export interface FileTreeNode { id: string; name: string; type?: 'folder' | 'file'; children?: FileTreeNode[]; badge?: number | string }
export interface FileTreeProps { nodes: FileTreeNode[]; defaultExpanded?: string[]; selected?: string; defaultSelected?: string; onSelect?: (id: string) => void; title?: string; actions?: ReactNode; label?: string }
export interface ProseProps { html?: string; children?: ReactNode; paper?: boolean; className?: string; innerRef?: any }
export interface SelectionToolbarProps { x?: number; y?: number; onAction?: (id: string) => void; actions?: { id: string; label: string; icon: IconProps['name']; accent?: boolean }[] }
/** useTextSelection(containerRef) → [selection | null, clear]; selection = { text, range, x, y, bottom } relative to the container. */

export interface CommentItem { author: string; time?: string; text?: string; kind?: 'bot'; typing?: boolean }
export interface CommentThreadProps { quote?: string; comments: CommentItem[]; people?: Person[]; onSubmit?: (text: string) => void; onResolve?: () => void; resolved?: boolean }
export interface MentionInputProps { people: Person[]; value?: string; defaultValue?: string; onChange?: (v: string) => void; onSubmit?: (v: string) => void; placeholder?: string; rows?: number; label?: string }

export interface SeverityBadgeProps { level: 'critical' | 'high' | 'medium' | 'low'; label?: string }
export interface DecisionListProps { children?: ReactNode; columns?: string[]; label?: string; empty?: ReactNode; footer?: ReactNode }
export interface DecisionRowProps { title: string; subtitle?: string; domain: string; severity: SeverityBadgeProps['level']; assignee?: string; status?: 'open' | 'answered'; onClick?: () => void }
export interface DocLinksProps { links: { title: string; path: string; section?: string; onClick?: () => void }[]; onOpen?: (link: { title: string; path: string; section?: string }) => void }
export interface QuestionBlockProps {
  number?: string; question: string; help?: string;
  type?: 'choice' | 'yesno' | 'upload';
  options?: OptionCardsProps['options']; allowOther?: boolean;
  defaultValue?: string; onChange?: (v: string) => void; onFiles?: (f: File[]) => void;
  uploadTitle?: string; defaultCommentOpen?: boolean;
}

export interface ToolStep { label: string; detail?: string; status?: 'running' | 'done'; icon?: IconProps['name'] }
export interface ChatMessageProps { role: 'user' | 'assistant'; name?: string; steps?: ToolStep[]; citations?: { label: string; onClick?: () => void }[]; typing?: boolean; children?: ReactNode }
export interface ChatComposerProps { onSend?: (text: string) => void; placeholder?: string; suggestions?: string[]; onSuggestion?: (s: string) => void; value?: string; defaultValue?: string; onChange?: (v: string) => void; disabled?: boolean; label?: string }

export interface ConnectorCardProps { name: string; category: string; description?: string; status?: 'connected' | 'attention' | 'off' | 'checking'; logoSrc?: string; meta?: string; actionLabel?: string; onAction?: () => void; variant?: 'regular' | 'strong' }

/* ---------- React Flow (@xyflow/react 12) ---------- */
export interface FlowCanvasProps {
  /** React Flow nodes using GDGlass.flowNodeTypes: gdNode, gdStep, gdGroup, gdLane. */
  nodes: any[];
  /** React Flow edges plus `kind`: 'sync' | 'async' | 'data' | 'flow'. */
  edges: any[];
  height?: number;
  minimap?: boolean;
  controls?: boolean;
  legend?: boolean;
  legendKinds?: EdgeKind[];
  draggable?: boolean;
  nodeTypes?: Record<string, any>;
  hideAttribution?: boolean;
  className?: string;
}
