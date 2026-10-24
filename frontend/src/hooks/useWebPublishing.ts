import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../api/client';
import type { EpisodeChoice, WebFile, WebMapping } from '../utils/webPublishing';

export interface TitleMetadata {
  title_english: string; title_romaji: string | null; year: number | null;
  original_language: string; original_description: string | null; description: string | null;
}
export interface WebSeason { external_id: string; season_number: number; name_english: string | null; year: number | null }
export interface WebConfig {
  tmdb_title_id: string; media_type: 'tv' | 'movie'; episode_structure: 'SEASONED' | 'FLAT';
  order_kind: 'standard' | 'episode_group'; group_id: string | null; season: WebSeason | null;
  metadata: TitleMetadata; poster_path: string | null;
}
export interface WebResult {
  stage: string; error: { code: string; message: string } | null;
  files: { file_id: number; filename: string; outcome: string; error?: string }[];
  metadata: { stage: string; outcome: string }[];
  poster: { outcome: string; error?: string } | null;
}
export interface TitleDetails extends WebConfig { romaji_titles: string[]; number_of_seasons: number | null }
export interface WebState {
  version: number; config: WebConfig | null; locked: boolean; season_locked: boolean; state: string; files: WebFile[];
  release_label: string; job: { id: number; status: string; result: WebResult | null; error: string | null;
    file_ids: number[]; release_label: string; changelog: string | null } | null;
}
export interface Catalog { seasons: WebSeason[]; groups: { id: string; name: string }[]; episodes_by_season: Record<string, EpisodeChoice[]> }
export interface SearchResult { id: string; media_type: 'tv' | 'movie'; title: string; year: string; poster_path: string | null; title_romaji: string | null; number_of_seasons: number | null }

export function useWebAvailability() {
  return useQuery({ queryKey: ['app-config'], queryFn: async () =>
    (await client.get<{ web_publishing_available: boolean }>('/config')).data, staleTime: 60_000 });
}

export function useWebState(projectId: number, enabled: boolean) {
  return useQuery({ queryKey: ['projects', projectId, 'web'], enabled,
    queryFn: async () => (await client.get<WebState>(`/projects/${projectId}/web`)).data,
    refetchInterval: q => q.state.data?.state === 'publishing' ? 2000 : false, staleTime: 15_000 });
}

export function useSaveWeb(projectId: number) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: async (body: { version: number; config: WebConfig; mappings?: ({ file_id: number } & WebMapping)[] }) =>
    (await client.put<WebState>(`/projects/${projectId}/web`, body)).data,
    onSuccess: data => qc.setQueryData(['projects', projectId, 'web'], data) });
}

export function usePublishWeb(projectId: number) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: async (body: { version: number; file_ids: number[]; release_label: string; changelog: string | null }) =>
    (await client.post<{ job_id: number }>(`/projects/${projectId}/web/publish`, body)).data,
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['projects', projectId, 'web'] }); void qc.invalidateQueries({ queryKey: ['jobs'] }); } });
}

export const webApi = {
  translate: async (projectId: number, version: number, config: WebConfig) =>
    (await client.post<{ description: string }>(`/projects/${projectId}/web/translate-description`, { version, config })).data,
  search: async (q: string) => (await client.get<SearchResult[]>('/web/tmdb/search', { params: { q } })).data,
  details: async (media_type: string, title_id: string, refresh = false) =>
    (await client.get<TitleDetails>('/web/tmdb/details', { params: { media_type, title_id, refresh } })).data,
  catalog: async (config: WebConfig, refresh = false) => (await client.get<Catalog>('/web/tmdb/catalog', {
    params: { title_id: config.tmdb_title_id, group_id: config.group_id, season_number: config.season?.season_number, refresh },
  })).data,
  lookup: async (config: WebConfig) => (await client.post<null | { episode_structure: WebConfig['episode_structure'];
    episode_order: { kind: WebConfig['order_kind']; group_id: string | null } }>('/web/tmdb/lookup', config)).data,
};
