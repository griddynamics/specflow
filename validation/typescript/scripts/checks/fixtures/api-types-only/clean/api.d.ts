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
