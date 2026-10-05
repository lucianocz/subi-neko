import { useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { ChunkJob, SubtitleChunk, VideoFile, WsEvent, WsJobData } from '../types';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
const PING_INTERVAL_MS = 25_000;
const CHUNK_JOB_TYPES = new Set([
  'translate_chunk',
  'validate_chunk',
  'repair_chunk',
  'polish_chunk',
  'review_chunk_final',
]);

function parseJsonObject(value?: string | null): Record<string, unknown> | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function chunkIndexFromJob(data: WsJobData): number | null {
  const payload = parseJsonObject(data.payload_json);
  const value = payload?.chunk_index;
  return typeof value === 'number' ? value : null;
}

function chunkJobFromEvent(data: WsJobData): ChunkJob | null {
  if (!data.job_type || !data.status || !data.scheduled_at || !data.updated_at) return null;
  return {
    id: data.job_id,
    job_type: data.job_type,
    status: data.status as ChunkJob['status'],
    attempt_count: data.attempt_count ?? 0,
    result: parseJsonObject(data.result_json),
    error_code: data.error_code ?? null,
    error_message: data.error_message ?? null,
    scheduled_at: data.scheduled_at,
    started_at: data.started_at ?? null,
    finished_at: data.finished_at ?? null,
    updated_at: data.updated_at,
  };
}

function applyChunkJobUpdate(queryClient: ReturnType<typeof useQueryClient>, data: WsJobData) {
  if (!data.job_type || !CHUNK_JOB_TYPES.has(data.job_type) || data.project_id == null || data.file_id == null) {
    return;
  }
  const chunkIndex = chunkIndexFromJob(data);
  const job = chunkJobFromEvent(data);
  if (chunkIndex == null || job == null) return;

  const queryKey = ['projects', data.project_id, 'files', data.file_id, 'chunks'];
  queryClient.setQueryData<SubtitleChunk[]>(queryKey, (old) => old?.map((chunk) => (
    chunk.chunk_index === chunkIndex
      ? { ...chunk, jobs: { ...chunk.jobs, [job.job_type]: job } }
      : chunk
  )));
  queryClient.invalidateQueries({ queryKey });
}

export function useJobSocket() {
  const queryClient = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    function connect() {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        pingRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send('ping');
        }, PING_INTERVAL_MS);
      };

      ws.onmessage = (event) => {
        let msg: WsEvent;
        try {
          msg = JSON.parse(event.data as string) as WsEvent;
        } catch {
          return;
        }

        // Dispatch for any hook that wants raw WS job events
        window.dispatchEvent(new CustomEvent('ws_job', { detail: msg }));

        if (msg.event === 'job_created' || msg.event === 'job_update' || msg.event === 'job_progress') {
          const { job_id } = msg.data;
          queryClient.invalidateQueries({ queryKey: ['jobs', job_id] });
          queryClient.invalidateQueries({ queryKey: ['jobs'] });
          if (msg.event === 'job_created' || msg.event === 'job_update') {
            applyChunkJobUpdate(queryClient, msg.data);
          }
          // Do NOT invalidate ['projects'] here — that cascades to file refetches.
          // project_updated fires after orchestration when state is actually settled.
        }

        if (msg.event === 'project_updated') {
          // Fired after orchestration completes — project/file state is now settled in DB.
          // The Subtitle Editor's pages live under ['subtitle-events', ...] (see
          // useSubtitleEditor.subtitleEventsKey), so this ['projects'] prefix
          // invalidation never refetches/rebuilds an open editor.
          queryClient.invalidateQueries({ queryKey: ['projects'] });
          // Invalidate all project stats (QA counts may have changed)
          queryClient.invalidateQueries({ queryKey: ['projects'], predicate: (q) => q.queryKey.includes('stats') });
        }

        if (msg.event === 'chunk_progress') {
          const { file_id, project_id, chunks_done, chunks_total } = msg.data;
          queryClient.setQueryData<VideoFile[]>(
            ['projects', project_id, 'files'],
            (old) => old?.map((f) => f.id === file_id ? { ...f, chunks_done, chunks_total } : f),
          );
          // Invalidate chunk detail if currently expanded
          queryClient.invalidateQueries({ queryKey: ['projects', project_id, 'files', file_id, 'chunks'] });
        }

        if (msg.event === 'scheduler_trigger') {
          // Invalidate scheduled tasks list so next_run_at refreshes
          queryClient.invalidateQueries({ queryKey: ['schedules'] });
        }
      };

      ws.onclose = () => {
        if (pingRef.current) clearInterval(pingRef.current);
        // Reconnect after 3 s
        setTimeout(connect, 3_000);
      };
    }

    connect();

    return () => {
      if (pingRef.current) clearInterval(pingRef.current);
      wsRef.current?.close();
    };
  }, [queryClient]);
}
