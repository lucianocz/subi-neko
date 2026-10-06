/** Client-side mirror of the backend's option validation (the server stays authoritative). */

export interface CpsLimitErrors {
  soft?: string;
  hard?: string;
}

/**
 * `soft < hard`, both positive. Values may be `''`/NaN while a NumberInput is
 * being edited; those are "not validated yet", not errors.
 */
export function validateCpsLimits(soft: number | string, hard: number | string): CpsLimitErrors {
  const errors: CpsLimitErrors = {};
  const s = typeof soft === 'number' ? soft : NaN;
  const h = typeof hard === 'number' ? hard : NaN;
  if (Number.isFinite(s) && s <= 0) errors.soft = 'Must be greater than 0.';
  if (Number.isFinite(h) && h <= 0) errors.hard = 'Must be greater than 0.';
  if (Number.isFinite(s) && Number.isFinite(h) && s > 0 && h > 0 && s >= h) {
    errors.soft = `Must be lower than the hard limit (${h}).`;
  }
  return errors;
}

/** Human message for a failed options save (server `detail` when there is one). */
export function optionsErrorMessage(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  return error instanceof Error ? error.message : 'Unknown error';
}
