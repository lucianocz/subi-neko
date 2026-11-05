// TMDB helpers shared by the WEB Publish wizard and the project import wizard.
// Pure (no React, no network) so both dialogs and the node test runner can use them.
import type { TitleDetails, WebConfig } from '../hooks/useWebPublishing.ts';
import { apiErrorDetail } from './projectArchive.ts';

export const posterUrl = (path: string) => `https://image.tmdb.org/t/p/w342${path}`;

/** Both flows persist the same approved metadata without changing provenance. */
export function normalizeApprovedConfig(config: WebConfig): WebConfig {
  return { ...config, metadata: { ...config.metadata,
    title_english: config.metadata.title_english.trim(),
    title_romaji: config.metadata.title_romaji?.trim() || null,
    description: config.metadata.description?.trim() || null,
  } };
}

export function apiMessage(error: unknown): string {
  return apiErrorDetail(error).message ?? 'Request failed. Please retry.';
}

/** The editable WEB config a freshly selected TMDB title starts from. */
export function titleConfig(detail: TitleDetails): WebConfig {
  return {
    tmdb_title_id: detail.tmdb_title_id,
    media_type: detail.media_type,
    episode_structure: detail.episode_structure,
    order_kind: detail.order_kind,
    group_id: detail.group_id,
    season: detail.season,
    metadata: detail.metadata,
    poster_path: detail.poster_path,
  };
}
