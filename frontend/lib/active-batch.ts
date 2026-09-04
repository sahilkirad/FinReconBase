"use client";

/**
 * Active-batch bookkeeping — lets the top nav return to the Financial
 * Operations Center after the user visits Ledger / Exception Desk.
 *
 * Deliberately dependency-free (no react-query) so the dashboard layout can
 * import it without pulling query hooks into the shell.
 */

export const ACTIVE_BATCH_KEY = "finrecon:activeBatch";

export const BATCH_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True when the pathname is the live Financial Operations route. */
export function isLiveBatchPath(pathname: string): boolean {
  const match = pathname.match(/^\/reconciliation\/([0-9a-f-]{36})$/i);
  return Boolean(match && BATCH_ID_RE.test(match[1]));
}

/** Extract the batch id from a live-path pathname (or null). */
export function batchIdFromPath(pathname: string): string | null {
  const match = pathname.match(/^\/reconciliation\/([0-9a-f-]{36})$/i);
  if (!match || !BATCH_ID_RE.test(match[1])) return null;
  return match[1];
}

/** Remember the batch the user is watching (survives page reloads). */
export function rememberActiveBatch(batchId: string): void {
  try {
    window.sessionStorage.setItem(ACTIVE_BATCH_KEY, batchId);
  } catch {
    /* sessionStorage unavailable — non-fatal */
  }
}

/** Read the last-watched batch id (or null). */
export function readActiveBatch(): string | null {
  try {
    return window.sessionStorage.getItem(ACTIVE_BATCH_KEY);
  } catch {
    return null;
  }
}
