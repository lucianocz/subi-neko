import type { QaIssue } from './index';

/** Compact row of `GET .../qc/events` — the whole file lives in browser memory. */
export interface QcEvent {
  id: number;
  line_index: number;
  start_ms: number;
  end_ms: number;
  style: string;
  speaker: string | null;
  content_type: string;
  event_type: string;
  translated_text: string | null;
  source_text: string;
  cps: number | null;
  is_hidden: boolean;
  is_manual: boolean;
  is_user_edited: boolean;
  is_locked: boolean;
  issue_count: number;
  max_issue_severity: string | null;
  watched_count: number;
}

export interface QcEventList {
  file_id: number;
  filename: string;
  qc_available: boolean;
  output_revision: number;
  event_count: number;
  total_count: number;
  hidden_count: number;
  /** HARD limit: red, and the only one QA uses. */
  cps_limit: number;
  /** SOFT limit: orange readability hint, never QA. */
  soft_cps_limit: number;
  /** Style names linked to the file (manual-event style selector). */
  styles: string[];
  events: QcEvent[];
}

export interface QcWatchedMatch {
  word: string;
  word_type: 'original' | 'translated';
}

export interface QcEventDetail extends QcEvent {
  original_ai_translated_text: string | null;
  is_approved: boolean;
  original_start_ms: number;
  original_end_ms: number;
  layer: number;
  name: string | null;
  character_name: string | null;
  character_gender: string | null;
  speaker_gender: string | null;
  issues: QaIssue[];
  watched_matches: QcWatchedMatch[];
}

export interface QcFont {
  id: string;
  family_names: string[];
  url: string;
  source: 'attachment' | 'configured';
  filename: string;
  media_type: string;
  bold: boolean;
  italic: boolean;
}

export interface QcFontManifest {
  file_id: number;
  replace_incompatible_fonts: boolean;
  required_families: string[];
  fonts: QcFont[];
  missing_families: string[];
  attachment_error: string | null;
}

export interface QcPreview {
  /** Authoritative translated ASS (backend `build_ass`). */
  text: string;
  /** `X-Output-Revision` the body was built at. */
  revision: number;
}

/** `GET|PUT .../qc/branding` — per-file render-time branding overlay config. */
export interface QcBranding {
  enabled: boolean;
  template_filename: string | null;
  start_offset_ms: number;
  /** Output revision after the call (own-save tracking). */
  output_revision: number;
}

export interface QcBrandingSave {
  enabled: boolean;
  template_filename: string | null;
  start_offset_ms: number;
}
