import { useCallback, useEffect, useRef, useState } from 'react';
import client from '../api/client';
import type { WsEvent } from '../types';
import { qcPreviewUrl } from './useQcPreview';

const DEBOUNCE_MS = 1500;

/**
 * Detects that the output changed behind an open QC page (another tab, a
 * pipeline run, a font/style option) WITHOUT refetching thousands of events.
 *
 * `project_updated` carries no revision, so on each (debounced) one for this
 * project we send a conditional GET of the preview with the ETag of the revision
 * we hold: 304 = unchanged (cheap), 200 = the output moved on -> `stale`.
 * The page's OWN edits advance `knownRevision` from the mutation response, so
 * their websocket echo answers 304. While one of our requests is in flight
 * (`isBusy`) the check is postponed, never run against a half-applied state.
 * Reload is explicit (the page's button); nothing is refetched silently.
 */
export function useQcStale(
  projectId: number,
  fileId: number,
  knownRevision: number | null,
  isBusy: () => boolean,
) {
  const [staleFor, setStaleFor] = useState<number | null>(null);
  const revisionRef = useRef(knownRevision);
  const busyRef = useRef(isBusy);

  useEffect(() => {
    revisionRef.current = knownRevision;
    busyRef.current = isBusy;
  });

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    function schedule() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(check, DEBOUNCE_MS);
    }

    async function check() {
      if (busyRef.current()) return schedule();
      const revision = revisionRef.current;
      if (revision == null) return;
      try {
        const res = await client.get(qcPreviewUrl(projectId, fileId), {
          responseType: 'text',
          transformResponse: (d) => d,
          headers: { 'If-None-Match': `"qc-${projectId}-${fileId}-${revision}"` },
          validateStatus: (s) => s === 200 || s === 304,
        });
        if (disposed || res.status === 304) return;
        // An edit of ours may have landed while the request was out.
        if (busyRef.current() || revisionRef.current !== revision) return schedule();
        const latest = Number(res.headers['x-output-revision'] ?? revision);
        if (latest !== revision) setStaleFor(revision);
      } catch {
        // QC may have become unavailable or the network blipped: keep the page as is.
      }
    }

    function onWs(e: Event) {
      const msg = (e as CustomEvent<WsEvent>).detail;
      if (msg?.event !== 'project_updated') return;
      // Option changes (font replacement, target language) broadcast without a
      // project id: they affect every project's output, so they count for this one.
      const target = msg.data?.project_id;
      if (target != null && target !== projectId) return;
      schedule();
    }

    window.addEventListener('ws_job', onWs);
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      window.removeEventListener('ws_job', onWs);
    };
  }, [projectId, fileId]);

  /** Flag the page stale right away (a mutation response showed an external jump). */
  const markStale = useCallback((revision: number) => setStaleFor(revision), []);

  // "Stale" is tied to the revision it was detected against, so a reload that
  // brings a newer revision clears it without any reset effect.
  return { stale: staleFor !== null && staleFor === knownRevision, markStale };
}
