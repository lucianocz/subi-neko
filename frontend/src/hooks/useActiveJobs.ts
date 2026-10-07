import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import client from '../api/client';
import type { Job, Project, WsEvent } from '../types';

export function useActiveJobs(): { jobs: Job[]; hiddenCount: number } {
  const [jobs, setJobs] = useState<Map<number, Job>>(new Map());
  const mounted = useRef(true);
  const queryClient = useQueryClient();

  // Initial fetch
  useEffect(() => {
    mounted.current = true;
    client.get<Job[]>('/jobs/active').then(({ data }) => {
      if (!mounted.current) return;
      setJobs(new Map(data.map((j) => [j.id, j])));
    });
    return () => {
      mounted.current = false;
    };
  }, []);

  // Real-time updates from WS events dispatched by useJobSocket
  useEffect(() => {
    function getProjectName(projectId: number): string | undefined {
      const projects = queryClient.getQueryData<Project[]>(['projects']);
      return projects?.find((p) => p.id === projectId)?.name;
    }

    const handler = async (e: Event) => {
      const msg = (e as CustomEvent<WsEvent>).detail;

      if (msg.event === 'job_progress') {
        const { job_id, progress, message } = msg.data;
        setJobs((prev) => {
          const next = new Map(prev);
          const job = next.get(job_id);
          if (job) next.set(job_id, { ...job, progress, message });
          return next;
        });
        return;
      }

      if (msg.event === 'job_created') {
        const { job_id, status, job_type, project_id, payload_json, scheduled_at } = msg.data;
        setJobs((prev) => {
          if (prev.has(job_id)) return prev;
          const now = new Date().toISOString();
          const placeholder: Job = {
            id: job_id,
            job_type: job_type ?? 'unknown',
            status: (status as Job['status']) ?? 'queued',
            project_id: project_id ?? 0,
            project_name: project_id ? getProjectName(project_id) : undefined,
            file_id: null,
            dedupe_key: '',
            priority: 100,
            payload_json: payload_json ?? null,
            result_json: null,
            attempt_count: 0,
            max_attempts: 3,
            error_code: null,
            error_message: null,
            scheduled_at: scheduled_at ?? now,
            started_at: null,
            finished_at: null,
            created_at: now,
            updated_at: now,
          };
          return new Map(prev).set(job_id, placeholder);
        });
        // Fetch full details only if project_name wasn't available in cache
        if (project_id && !getProjectName(project_id)) {
          try {
            const { data } = await client.get<Job>(`/jobs/${job_id}`);
            if (!mounted.current) return;
            setJobs((prev) => {
              const next = new Map(prev);
              if (data.status === 'queued' || data.status === 'running') {
                next.set(data.id, data);
              } else if (prev.has(data.id)) {
                next.set(data.id, data);
              }
              return next;
            });
          } catch { /* ignore */ }
        }
        return;
      }

      if (msg.event === 'job_update') {
        const { job_id, status, started_at } = msg.data as { job_id: number; status?: string; started_at?: string };
        if (status) {
          if (status === 'queued' || status === 'running') {
            let isNew = false;
            setJobs((prev) => {
              const next = new Map(prev);
              const existing = next.get(job_id);
              if (existing) {
                const updates: Partial<Job> = { status: status as Job['status'] };
                if (started_at) updates.started_at = started_at;
                next.set(job_id, { ...existing, ...updates });
              } else {
                isNew = true;
              }
              return next;
            });
            if (isNew) {
              try {
                const { data } = await client.get<Job>(`/jobs/${job_id}`);
                if (!mounted.current) return;
                if (data.status === 'queued' || data.status === 'running') {
                  setJobs((prev) => new Map(prev).set(data.id, data));
                }
              } catch { /* ignore */ }
            }
          } else {
            setJobs((prev) => {
              const next = new Map(prev);
              next.delete(job_id);
              return next;
            });
          }
          return;
        }
        try {
          const { data } = await client.get<Job>(`/jobs/${job_id}`);
          if (!mounted.current) return;
          setJobs((prev) => {
            const next = new Map(prev);
            if (data.status === 'queued' || data.status === 'running') {
              next.set(job_id, data);
            } else {
              next.delete(job_id);
            }
            return next;
          });
        } catch { /* ignore */ }
        return;
      }
    };

    window.addEventListener('ws_job', handler);
    return () => window.removeEventListener('ws_job', handler);
  }, [queryClient]);

  const sorted = Array.from(jobs.values()).sort((a, b) => {
    // Running first, then queued; within same status sort by scheduled_at
    const statusOrder = (s: string) => s === 'running' ? 0 : 1;
    const diff = statusOrder(a.status) - statusOrder(b.status);
    if (diff !== 0) return diff;
    return new Date(a.scheduled_at).getTime() - new Date(b.scheduled_at).getTime();
  });

  const MAX_VISIBLE = 5;
  const visible = sorted.slice(0, MAX_VISIBLE);
  const hiddenCount = Math.max(0, sorted.length - MAX_VISIBLE);

  return { jobs: visible, hiddenCount };
}
