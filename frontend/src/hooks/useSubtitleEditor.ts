import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { QueryClient, QueryKey } from '@tanstack/react-query';
import client from '../api/client';
import type {
  ProjectStats,
  SubtitleEventEditorRow,
  SubtitleEventPage,
  VideoFile,
} from '../types';

export const SUBTITLE_EVENTS_PAGE_SIZE = 1000;

export interface SubtitleEventFilters {
  showInfo: boolean;
  showResolved: boolean;
  issuesOnly: boolean;
}

interface SubtitleEventPageParams extends SubtitleEventFilters {
  page: number;
  pageSize: number;
}

// Deliberately NOT under the ['projects', ...] prefix: the many broad
// invalidateQueries({ queryKey: ['projects'] }) calls (live project_updated
// events, mutations) would otherwise refetch and rebuild the whole editor
// page. The editor is a snapshot kept current by the row/summary patches
// below, and it is dropped on close so reopening always starts fresh.
export function subtitleEventsKey(projectId: number, fileId: number | null): QueryKey {
  return ['subtitle-events', projectId, fileId];
}

function pageKey(projectId: number, fileId: number | null, params: SubtitleEventPageParams): QueryKey {
  return [...subtitleEventsKey(projectId, fileId), params];
}

export function useSubtitleEvents(
  projectId: number,
  fileId: number | null,
  enabled: boolean,
  page: number,
  filters: SubtitleEventFilters,
  pageSize: number = SUBTITLE_EVENTS_PAGE_SIZE,
) {
  const params: SubtitleEventPageParams = { page, pageSize, ...filters };
  return useQuery<SubtitleEventPage>({
    queryKey: pageKey(projectId, fileId, params),
    queryFn: async () => {
      const { data } = await client.get<SubtitleEventPage>(
        `/projects/${projectId}/files/${fileId}/subtitle-events`,
        {
          params: {
            page,
            page_size: pageSize,
            show_info: filters.showInfo,
            show_resolved: filters.showResolved,
            issues_only: filters.issuesOnly,
          },
        },
      );
      return data;
    },
    enabled: enabled && fileId !== null,
    placeholderData: keepPreviousData,
    // Live in-place patches keep the data current; nothing should silently
    // refetch (and recreate) the rows underneath an editing user.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

/** Apply `update` to every cached page of one file, with that page's params. */
function updateCachedPages(
  queryClient: QueryClient,
  projectId: number,
  fileId: number,
  update: (data: SubtitleEventPage, params: SubtitleEventPageParams) => SubtitleEventPage,
) {
  for (const query of queryClient.getQueryCache().findAll({ queryKey: subtitleEventsKey(projectId, fileId) })) {
    const params = query.queryKey[3] as SubtitleEventPageParams | undefined;
    const data = query.state.data as SubtitleEventPage | undefined;
    if (!data || !params) continue;
    const next = update(data, params);
    if (next !== data) queryClient.setQueryData<SubtitleEventPage>(query.queryKey, next);
  }
}

/** Replace one event (by id) in every cached page that contains it. */
export function patchSubtitleEventRow(
  queryClient: QueryClient,
  projectId: number,
  fileId: number,
  row: SubtitleEventEditorRow,
) {
  updateCachedPages(queryClient, projectId, fileId, (data) => (
    data.items.some((item) => item.id === row.id)
      ? { ...data, items: data.items.map((item) => (item.id === row.id ? row : item)) }
      : data
  ));
}

/** Shift the header's watched-word total after a local text change. */
export function adjustWatchedOccurrences(
  queryClient: QueryClient,
  projectId: number,
  fileId: number,
  delta: number,
) {
  if (delta === 0) return;
  updateCachedPages(queryClient, projectId, fileId, (data) => ({
    ...data,
    summary: {
      ...data.summary,
      watched_occurrences: Math.max(0, data.summary.watched_occurrences + delta),
    },
  }));
}

function findCachedIssue(queryClient: QueryClient, projectId: number, fileId: number, issueId: number) {
  for (const query of queryClient.getQueryCache().findAll({ queryKey: subtitleEventsKey(projectId, fileId) })) {
    const data = query.state.data as SubtitleEventPage | undefined;
    if (!data) continue;
    for (const row of data.items) {
      const issue = row.issues.find((item) => item.id === issueId);
      if (issue) return issue;
    }
  }
  return undefined;
}

interface UpdateSubtitleEventPayload {
  projectId: number;
  fileId: number;
  eventId: number;
  translated_text: string | null;
}

interface RevertSubtitleEventPayload {
  projectId: number;
  fileId: number;
  eventId: number;
}

interface ResolveQaIssuePayload {
  projectId: number;
  fileId: number;
  issueId: number;
}

export function useUpdateSubtitleEvent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: UpdateSubtitleEventPayload) => {
      const { data } = await client.put<SubtitleEventEditorRow>(
        `/projects/${payload.projectId}/files/${payload.fileId}/subtitle-events/${payload.eventId}`,
        {
          translated_text: payload.translated_text,
        },
      );
      return data;
    },
    // The response carries the authoritative row (incl. recalculated CPS);
    // only that row is replaced — no page refetch.
    onSuccess: (data, variables) => {
      patchSubtitleEventRow(queryClient, variables.projectId, variables.fileId, data);
    },
  });
}

export function useRevertSubtitleEvent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: RevertSubtitleEventPayload) => {
      const { data } = await client.post<SubtitleEventEditorRow>(
        `/projects/${payload.projectId}/files/${payload.fileId}/subtitle-events/${payload.eventId}/revert`,
      );
      return data;
    },
    onSuccess: (data, variables) => {
      patchSubtitleEventRow(queryClient, variables.projectId, variables.fileId, data);
    },
  });
}

export function useResolveQaIssue() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: ResolveQaIssuePayload) => {
      const { data } = await client.post<SubtitleEventEditorRow>(
        `/projects/${payload.projectId}/files/${payload.fileId}/qa-issues/${payload.issueId}/resolve`,
      );
      return data;
    },
    onMutate: async (variables) => {
      const issue = findCachedIssue(queryClient, variables.projectId, variables.fileId, variables.issueId);
      return { issue };
    },
    onSuccess: (data, variables, context) => {
      patchSubtitleEventRow(queryClient, variables.projectId, variables.fileId, data);
      const issue = context?.issue;
      if (!issue) return;
      // Header summary: one fewer unresolved issue; the per-type badge count
      // only drops when resolved issues are hidden (otherwise it still shows).
      updateCachedPages(queryClient, variables.projectId, variables.fileId, (page, params) => ({
        ...page,
        summary: {
          ...page.summary,
          unresolved_issue_count: Math.max(0, page.summary.unresolved_issue_count - 1),
          issue_counts: params.showResolved
            ? page.summary.issue_counts
            : page.summary.issue_counts
              .map((item) => (
                item.severity === issue.severity && item.qa_type === issue.qa_type
                  ? { ...item, count: item.count - 1 }
                  : item
              ))
              .filter((item) => item.count > 0),
        },
      }));
      const isError = ['blocker', 'critical', 'error', 'high'].includes(issue.severity.toLowerCase());
      queryClient.setQueryData<VideoFile[]>(
        ['projects', variables.projectId, 'files'],
        (files) => files?.map((file) => {
          if (file.id !== variables.fileId) return file;
          return {
            ...file,
            qa_issues: Math.max(0, file.qa_issues - 1),
            qa_errors: isError ? Math.max(0, file.qa_errors - 1) : file.qa_errors,
            qa_warnings: isError ? file.qa_warnings : Math.max(0, file.qa_warnings - 1),
          };
        }),
      );
      queryClient.setQueryData<ProjectStats>(
        ['projects', variables.projectId, 'stats'],
        (stats) => stats
          ? {
              qa_errors: isError ? Math.max(0, stats.qa_errors - 1) : stats.qa_errors,
              qa_warnings: isError ? stats.qa_warnings : Math.max(0, stats.qa_warnings - 1),
            }
          : stats,
      );
    },
  });
}
