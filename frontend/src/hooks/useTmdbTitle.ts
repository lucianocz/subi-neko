import { webApi } from './useWebPublishing';

/** Shared React Query options for one TMDB title's details (WEB wizard + import wizard). */
export function titleQuery(media: string, id: string) {
  return {
    queryKey: ['web-title', media, id],
    queryFn: () => webApi.details(media, id),
    staleTime: 60_000,
    retry: false as const,
  };
}
