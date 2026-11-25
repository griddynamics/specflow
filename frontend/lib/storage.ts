// The one module that writes cookies and web storage (eslint.kit.config.mjs `storage`).

/** Sets a cookie for the whole site that lasts `maxAgeSeconds`. Browser only. */
export function writeCookie(name: string, value: string, maxAgeSeconds: number): void {
  document.cookie = `${name}=${value}; path=/; max-age=${String(maxAgeSeconds)}; samesite=lax`;
}
