"use client";

/**
 * Active-batch bookkeeping — lets the top nav return to the Financial
 * Operations Center after the user visits Ledger / Exception Desk.
 *
 * Deliberately dependency-free (no react-query) so the dashboard layout can
 * import it without pulling query hooks into the shell.
 */

export const ACTIVE_BATCH_KEY = "finrecon:activeBatch";

const STORE_VERSION = 2;

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

interface ActiveBatchEntry {
  v: number;
  batch_id: string;
  vendor_code: string;
}

/** Remember the batch the user is watching (survives page reloads). */
export function rememberActiveBatch(
  batchId: string,
  vendorCode: string
): void {
  try {
    const entry: ActiveBatchEntry = {
      v: STORE_VERSION,
      batch_id: batchId,
      vendor_code: vendorCode,
    };
    window.sessionStorage.setItem(ACTIVE_BATCH_KEY, JSON.stringify(entry));
  } catch {
    /* sessionStorage unavailable — non-fatal */
  }
}

/**
 * Read the last-watched batch id for THIS vendor (or null). A pointer left
 * by a different tenant is never returned — no cross-tenant nav leakage.
 */
export function readActiveBatch(vendorCode: string): string | null {
  try {
    const raw = window.sessionStorage.getItem(ACTIVE_BATCH_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ActiveBatchEntry;
    if (parsed?.v !== STORE_VERSION) return null;
    return parsed.vendor_code === vendorCode ? parsed.batch_id : null;
  } catch {
    return null;
  }
}

/** Drop the remembered pointer (e.g. on sign-out). */
export function clearActiveBatch(): void {
  try {
    window.sessionStorage.removeItem(ACTIVE_BATCH_KEY);
  } catch {
    /* sessionStorage unavailable — non-fatal */
  }
}
