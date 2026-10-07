/** Branding dialog helpers. Pure; time text goes through the shared QC time helpers. */
import type { QcBranding, QcBrandingSave } from '../types/qc.ts';
import { formatTimeField, parseTimeField } from './qcTime.ts';

/** What the dialog edits: the start is text until Save validates it. */
export interface BrandingDraft {
  enabled: boolean;
  template: string | null;
  start: string;
  scale: boolean;
}

export function draftFromBranding(config: Pick<QcBranding, 'enabled' | 'template_filename' | 'start_offset_ms' | 'scale_to_script_playres'>): BrandingDraft {
  return {
    enabled: config.enabled,
    template: config.template_filename,
    start: formatTimeField(config.start_offset_ms),
    scale: config.scale_to_script_playres,
  };
}

export type BrandingDraftResult = { ok: true; body: QcBrandingSave } | { ok: false; reason: string };

/** Validate the draft locally (the backend stays authoritative). */
export function parseBrandingDraft(draft: BrandingDraft): BrandingDraftResult {
  const start = parseTimeField(draft.start);
  if (start === null) return { ok: false, reason: 'Start must be a time like 00:20:00.000.' };
  if (draft.enabled && !draft.template) return { ok: false, reason: 'Choose a template to enable branding.' };
  return {
    ok: true,
    body: {
      enabled: draft.enabled,
      template_filename: draft.template,
      start_offset_ms: start,
      scale_to_script_playres: draft.scale,
    },
  };
}

/** Unsaved changes? Compares parsed values so `0:20:00` equals `00:20:00.000`. */
export function isBrandingDirty(draft: BrandingDraft, saved: QcBrandingSave): boolean {
  const parsed = parseTimeField(draft.start);
  return draft.enabled !== saved.enabled
    || draft.template !== saved.template_filename
    || parsed !== saved.start_offset_ms
    || draft.scale !== saved.scale_to_script_playres;
}

/**
 * Fonts can only change when the template or the on/off state changed while
 * branding is (or was) on; a Start-only move never touches font requirements.
 */
export function brandingNeedsFontRefresh(prev: QcBrandingSave, next: QcBrandingSave): boolean {
  if (prev.enabled !== next.enabled) return true;
  return next.enabled && prev.template_filename !== next.template_filename;
}

/** Header button state indicator. */
export function brandingIndicator(config: Pick<QcBranding, 'enabled'> | null | undefined): 'on' | 'off' {
  return config?.enabled ? 'on' : 'off';
}

/** Template choices: the stored name stays selectable even if it vanished from disk. */
export function templateOptions(templates: readonly string[], current: string | null): string[] {
  return current && !templates.includes(current) ? [current, ...templates] : [...templates];
}
