/**
 * Backend origin when frontend is served separately (e.g. Railway).
 * Empty in dev: Vite proxy serves `/api` on the same host.
 */
export function apiOrigin(): string {
  return (import.meta.env.VITE_BACKEND_URL || '').replace(/\/$/, '');
}

/** Absolute API URL, or same-origin path when `VITE_BACKEND_URL` is unset. */
export function apiUrl(path: string): string {
  const p = path.startsWith('/') ? path : `/${path}`;
  const base = apiOrigin();
  return base ? `${base}${p}` : p;
}
