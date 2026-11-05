import { useQuery, useMutation } from '@tanstack/react-query';
import client from '../api/client';
import type { ImportDirectory, SearchResult } from '../types';
import type { ImportBody, ImportIntent, Suggestion } from '../utils/tmdbImport';

/** POST /import body; the TMDB mapping and intent are optional (legacy import). */
export type ImportRequest = ImportBody;

export interface ImportResult {
  id: number;
  name: string;
  source_directory: string;
  status: string;
  /** Recorded intent only — importing never publishes anything. */
  web_publish_intent: ImportIntent | null;
  tmdb_mapped: boolean;
  tmdb_episodes: number;
  expected_episode_count: number | null;
}

export interface AppConfig { web_publishing_available: boolean; tmdb_available: boolean }

/** Same query key as useWebAvailability, so both share one cached response. */
export function useAppConfig() {
  return useQuery({
    queryKey: ['app-config'],
    queryFn: async () => (await client.get<AppConfig>('/config')).data,
    staleTime: 60_000,
  });
}

/** AniBridge's advisory TMDB match for a source identity. Never throws to the
 * user: an unavailable dataset comes back as status "unavailable". */
export function useTmdbSuggestion(provider: string, providerId: string, enabled: boolean) {
  return useQuery<Suggestion>({
    queryKey: ['import', 'tmdb-suggestion', provider, providerId],
    queryFn: async () => (await client.get<Suggestion>('/import/tmdb-suggestion', {
      params: { provider, provider_id: providerId },
    })).data,
    enabled,
    staleTime: 10 * 60 * 1000,
    retry: false,
  });
}

export async function translateImportDescription(description: string): Promise<{ description: string }> {
  return (await client.post<{ description: string }>('/import/translate-description', { description })).data;
}

export function useImportDirectories(enabled: boolean) {
  return useQuery<ImportDirectory[]>({
    queryKey: ['import', 'directories'],
    queryFn: async () => {
      const { data } = await client.get<ImportDirectory[]>('/import/directories');
      return data;
    },
    enabled,
    staleTime: 0,
    gcTime: 0,
  });
}

export function useMetadataSearch(provider: string, query: string) {
  return useQuery<SearchResult[]>({
    queryKey: ['metadata', 'search', provider, query],
    queryFn: async () => {
      const { data } = await client.get<SearchResult[]>('/metadata/search', {
        params: { provider, q: query },
      });
      return data;
    },
    enabled: query.length > 0,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
}

export function useImportProject() {
  return useMutation({
    mutationFn: async (body: ImportRequest) => {
      const { data } = await client.post<ImportResult>('/import', body);
      return data;
    },
  });
}
