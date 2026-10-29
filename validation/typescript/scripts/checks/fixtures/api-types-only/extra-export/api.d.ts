export interface paths {
  '/health': { get: never };
}
export type webhooks = Record<string, never>;
export interface components {
  schemas: { Health: { status: string } };
}
export type $defs = Record<string, never>;
export interface operations {
  health: never;
}
// ok: not a legitimate export -- simulates a hand-added UI type
export interface ExtraHeaderProps {
  title: string;
}
