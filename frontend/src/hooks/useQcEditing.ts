import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { RefObject } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import client from '../api/client';
import type { QaIssue } from '../types';
import type { QcEvent, QcEventDetail, QcEventList } from '../types/qc';
import {
  defaultStyle, diffDraft, draftFromEvent, isUntouchedNewEvent, mergeSaved, validateNewEvent,
} from '../utils/qcDraft';
import type { EventDraft, NewEventDraft } from '../utils/qcDraft';
import { neighbourAfterRemoval, nextMeta, upsertEvent } from '../utils/qcEvents';
import { applyIssueResolved, applyIssueSummary, mergeEventIntoDetail, summarizeIssues } from '../utils/qcQa';
import { classifyMutationRevision, createLatestGate } from '../utils/qcRevision';
import { videoTimeToMs } from '../utils/qcTime';
import { qcErrorDetail, qcKey } from './useQcEvents';
import type { JassubControls } from './useJassub';
import type { useQcFonts } from './useQcFonts';
import { fetchQcPreview, qcPreviewKey } from './useQcPreview';
import { useQcStale } from './useQcStale';

const NEW_EVENT_DEFAULT_MS = 2000;

type Status = { state: 'idle' | 'busy' | 'error'; message: string | null };
const IDLE: Status = { state: 'idle', message: null };

interface Args {
  projectId: number;
  fileId: number;
  showHidden: boolean;
  list: QcEventList;
  /** Revision the loaded preview was built at (the page's initial known revision). */
  initialRevision: number | null;
  renderer: JassubControls;
  fonts: ReturnType<typeof useQcFonts>;
  videoRef: RefObject<HTMLVideoElement | null>;
}

const eventsKey = (projectId: number, fileId: number, showHidden: boolean) =>
  [...qcKey(projectId, fileId), 'events', { showHidden }] as const;

const revisionOf = (headers: unknown): number =>
  Number((headers as Record<string, unknown> | undefined)?.['x-output-revision'] ?? NaN);

/**
 * Everything that edits: selection, the per-event draft, the single
 * `flushSelected()` that every "leave this event" action goes through, the
 * mutations (save / hide / restore / create), local cache updates, and the
 * authoritative preview refresh. DATA SAVE status and PREVIEW REFRESH status are
 * kept separate: a failed refresh never un-saves anything.
 *
 * Concurrency rules:
 * - saves are serialized on a promise chain, and each run diffs against the
 *   query cache (updated synchronously on success), so a blur followed at once by
 *   a row click sends ONE request;
 * - preview refreshes use a latest-wins gate: only the newest refresh may touch
 *   the renderer or state, so a slow older fetch can never overwrite a newer one;
 * - font loads are serialized and diffed against what the renderer already holds.
 */
export function useQcEditing({ projectId, fileId, showHidden, list, initialRevision, renderer, fonts, videoRef }: Args) {
  const queryClient = useQueryClient();
  const base = `/projects/${projectId}/files/${fileId}/qc`;

  const [selectedId, setSelectedIdState] = useState<number | null>(null);
  const selectedIdRef = useRef<number | null>(null);
  const [draft, setDraftState] = useState<EventDraft | null>(null);
  const draftRef = useRef<EventDraft | null>(null);
  const [newDraft, setNewDraftState] = useState<NewEventDraft | null>(null);
  const newDraftRef = useRef<NewEventDraft | null>(null);
  const newInitialRef = useRef<NewEventDraft | null>(null);
  const [newInitial, setNewInitial] = useState<NewEventDraft | null>(null);
  const [newError, setNewError] = useState<string | null>(null);

  const [save, setSave] = useState<Status>(IDLE);
  const [action, setAction] = useState<Status>(IDLE);
  const [resolvingIssueId, setResolvingIssueId] = useState<number | null>(null);
  const [preview, setPreview] = useState<Status>(IDLE);
  const [fontError, setFontError] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);
  const [reveal, setReveal] = useState<{ id: number; nonce: number } | null>(null);

  const inflight = useRef(0);
  const gate = useRef(createLatestGate()).current;
  const saveChain = useRef<Promise<boolean>>(Promise.resolve(true));
  const fontChain = useRef<Promise<unknown>>(Promise.resolve());

  const [knownState, setKnownState] = useState<number | null>(null);
  const known = knownState ?? initialRevision;
  const knownRef = useRef(known);
  const { stale, markStale } = useQcStale(projectId, fileId, known, () => inflight.current > 0);

  // Latest props/values for the stable callbacks below (updated after each render).
  const latest = useRef({ showHidden, renderer, fonts, styles: list.styles, markStale });
  useEffect(() => {
    latest.current = { showHidden, renderer, fonts, styles: list.styles, markStale };
    knownRef.current = known;
  });

  const busy = useCallback(async <T,>(fn: () => Promise<T>): Promise<T> => {
    inflight.current += 1;
    try {
      return await fn();
    } finally {
      inflight.current -= 1;
    }
  }, []);

  // -- cache access --------------------------------------------------------

  const readList = useCallback(
    () => queryClient.getQueryData<QcEventList>(eventsKey(projectId, fileId, latest.current.showHidden)),
    [queryClient, projectId, fileId],
  );
  const getEvent = useCallback((id: number) => readList()?.events.find((e) => e.id === id), [readList]);

  /** Apply one mutated event to every cached list variant (hidden shown / not). */
  const applyToCaches = useCallback((updated: QcEvent, created: boolean, revision: number) => {
    for (const q of queryClient.getQueryCache().findAll({ queryKey: [...qcKey(projectId, fileId), 'events'] })) {
      const includeHidden = (q.queryKey[4] as { showHidden: boolean } | undefined)?.showHidden ?? false;
      queryClient.setQueryData<QcEventList>(q.queryKey, (old) => {
        if (!old) return old;
        const previous = old.events.find((e) => e.id === updated.id);
        const events = upsertEvent(old.events, updated, includeHidden);
        return {
          ...old,
          ...nextMeta(old, events, previous, updated, created),
          output_revision: Number.isFinite(revision) ? Math.max(old.output_revision, revision) : old.output_revision,
          events,
        };
      });
    }
    // Watched matches / issues of the detail panel depend on the new text.
    queryClient.invalidateQueries({ queryKey: [...qcKey(projectId, fileId), 'event', updated.id] });
  }, [queryClient, projectId, fileId]);

  const acceptRevision = useCallback((revision: number) => {
    const verdict = classifyMutationRevision(knownRef.current ?? revision, revision);
    if (verdict.kind === 'external') {
      // Somebody else changed the output too: our view is incomplete. Keep the old
      // known revision and flag the page instead of pretending it is current.
      latest.current.markStale(knownRef.current ?? revision);
      return;
    }
    knownRef.current = verdict.known;
    setKnownState(verdict.known);
  }, []);

  // -- selection state -----------------------------------------------------

  const setDraft = useCallback((d: EventDraft | null) => {
    draftRef.current = d;
    setDraftState(d);
  }, []);
  const setNewDraft = useCallback((d: NewEventDraft | null) => {
    newDraftRef.current = d;
    setNewDraftState(d);
  }, []);

  const selectNow = useCallback((event: QcEvent | null) => {
    selectedIdRef.current = event?.id ?? null;
    setSelectedIdState(event?.id ?? null);
    setDraft(event ? draftFromEvent(event) : null);
    setNewDraft(null);
    setNewError(null);
    setSave(IDLE);
  }, [setDraft, setNewDraft]);

  // -- preview refresh -------------------------------------------------------

  const refreshOutput = useCallback(async (opts: { fonts: boolean }) => {
    const token = gate.begin();
    setPreview({ state: 'busy', message: null });
    await busy(async () => {
      try {
        if (opts.fonts) {
          // Fonts first: the renderer must hold them before the track that needs them.
          const run = fontChain.current.then(() => latest.current.fonts.refresh(latest.current.renderer.addFonts));
          fontChain.current = run.catch(() => undefined);
          try {
            await run;
            setFontError(null);
          } catch (e) {
            setFontError(qcErrorDetail(e) ?? 'Could not refresh fonts.');
          }
        }
        const fetched = await fetchQcPreview(projectId, fileId);
        if (!gate.isCurrent(token)) return;
        await latest.current.renderer.setTrack(fetched.text);
        if (!gate.isCurrent(token)) return;
        queryClient.setQueryData(qcPreviewKey(projectId, fileId), fetched);
        setPreview(IDLE);
      } catch (e) {
        if (gate.isCurrent(token)) {
          setPreview({ state: 'error', message: qcErrorDetail(e) ?? 'Could not refresh the subtitle preview.' });
        }
      }
    });
  }, [busy, gate, projectId, fileId, queryClient]);

  const retryPreview = useCallback(() => refreshOutput({ fonts: true }), [refreshOutput]);

  // -- saving an existing event ---------------------------------------------

  const saveDraft = useCallback(async (): Promise<boolean> => {
    const d = draftRef.current;
    if (!d) return true;
    const event = getEvent(d.id);
    if (!event) return true;
    const patch = diffDraft(d, event);
    if (Object.keys(patch).length === 0) return true;

    setSave({ state: 'busy', message: null });
    return busy(async () => {
      try {
        const res = await client.patch<QcEvent>(`${base}/events/${d.id}`, patch);
        const saved = res.data;
        const revision = revisionOf(res.headers);
        const sortKeyMoved = saved.start_ms !== event.start_ms;
        applyToCaches(saved, false, revision);
        acceptRevision(revision);
        if (draftRef.current?.id === saved.id) setDraft(mergeSaved(draftRef.current, patch, saved));
        setSave(IDLE);
        if (sortKeyMoved && selectedIdRef.current === saved.id) {
          setReveal((r) => ({ id: saved.id, nonce: (r?.nonce ?? 0) + 1 }));
        }
        const textChanged = patch.translated_text !== undefined && patch.translated_text !== (event.translated_text ?? '');
        // Saved data is final; the preview refresh reports its own status.
        void refreshOutput({ fonts: textChanged });
        return true;
      } catch (e) {
        setSave({ state: 'error', message: qcErrorDetail(e) ?? 'Could not save the change.' });
        return false;
      }
    });
  }, [acceptRevision, applyToCaches, base, busy, getEvent, refreshOutput, setDraft]);

  // -- manual event ----------------------------------------------------------

  const createEvent = useCallback(async (selectAfter: boolean): Promise<boolean> => {
    const d = newDraftRef.current;
    if (!d) return true;
    const errors = validateNewEvent(d, latest.current.styles);
    if (errors.length > 0) {
      setNewError(errors.join(' '));
      return false;
    }
    setAction({ state: 'busy', message: null });
    return busy(async () => {
      try {
        const res = await client.post<QcEvent>(`${base}/events`, {
          start_ms: d.startMs,
          end_ms: d.endMs,
          translated_text: d.text,
          style: d.style,
          ...(d.speaker.trim() ? { speaker: d.speaker.trim() } : {}),
        });
        const created = res.data;
        applyToCaches(created, true, revisionOf(res.headers));
        acceptRevision(revisionOf(res.headers));
        setAction(IDLE);
        if (selectAfter) {
          selectNow(created);
          setReveal((r) => ({ id: created.id, nonce: (r?.nonce ?? 0) + 1 }));
        } else {
          setNewDraft(null);
          setNewError(null);
        }
        void refreshOutput({ fonts: true });
        return true;
      } catch (e) {
        setAction(IDLE);
        setNewError(qcErrorDetail(e) ?? 'Could not create the event.');
        return false;
      }
    });
  }, [acceptRevision, applyToCaches, base, busy, refreshOutput, selectNow, setNewDraft]);

  /**
   * THE one "leave the current event" gate. Saves the selected event if dirty, or
   * creates/discards a pending manual draft. Resolves `false` (and leaves
   * everything as it was, error shown) when the user must act first.
   */
  const flushSelected = useCallback((): Promise<boolean> => {
    const run = async (): Promise<boolean> => {
      const nd = newDraftRef.current;
      if (nd) {
        if (newInitialRef.current && isUntouchedNewEvent(nd, newInitialRef.current)) {
          setNewDraft(null);
          setNewError(null);
          return true;
        }
        if (validateNewEvent(nd, latest.current.styles).length > 0) {
          setNewError('Finish the new event (or Cancel) before leaving it.');
          return false;
        }
        return createEvent(false);
      }
      return saveDraft();
    };
    const next = saveChain.current.then(run);
    saveChain.current = next.catch(() => false);
    return next;
  }, [createEvent, saveDraft, setNewDraft]);

  // -- user actions ----------------------------------------------------------

  const select = useCallback(async (event: QcEvent): Promise<boolean> => {
    if (event.id === selectedIdRef.current && !newDraftRef.current) return true;
    if (!(await flushSelected())) return false;
    // Take the freshest copy: the flush may just have changed this very list.
    selectNow(getEvent(event.id) ?? event);
    return true;
  }, [flushSelected, getEvent, selectNow]);

  const changeDraft = useCallback((d: EventDraft) => {
    setDraft(d);
    setSave((s) => (s.state === 'error' ? IDLE : s));
  }, [setDraft]);

  const startNewEvent = useCallback(async () => {
    if (newDraftRef.current) return;
    if (!(await flushSelected())) return;
    const el = videoRef.current;
    const startMs = el ? videoTimeToMs(el.currentTime) : 0;
    const initial: NewEventDraft = {
      text: '', startMs, endMs: startMs + NEW_EVENT_DEFAULT_MS, style: defaultStyle(latest.current.styles), speaker: '',
    };
    newInitialRef.current = initial;
    setNewInitial(initial);
    selectedIdRef.current = null;
    setSelectedIdState(null);
    setDraft(null);
    setNewError(null);
    setSave(IDLE);
    setNewDraft(initial);
  }, [flushSelected, setDraft, setNewDraft, videoRef]);

  const cancelNewEvent = useCallback(() => {
    setNewDraft(null);
    setNewError(null);
  }, [setNewDraft]);

  const toggleHidden = useCallback(async () => {
    const id = selectedIdRef.current;
    if (id === null) return;
    if (!(await flushSelected())) return;
    const event = getEvent(id);
    if (!event) return;
    const hiding = !event.is_hidden;
    const events = readList()?.events ?? [];
    const neighbour = neighbourAfterRemoval(events, events.findIndex((e) => e.id === id));
    setAction({ state: 'busy', message: null });
    await busy(async () => {
      try {
        const res = await client.post<QcEvent>(`${base}/events/${id}/${hiding ? 'hide' : 'restore'}`);
        applyToCaches(res.data, false, revisionOf(res.headers));
        acceptRevision(revisionOf(res.headers));
        setAction(IDLE);
        if (hiding && !latest.current.showHidden) selectNow(neighbour);
        else if (draftRef.current?.id === id) setDraft(draftFromEvent(res.data));
        // Hiding can only shrink the font set; restoring can bring one back.
        void refreshOutput({ fonts: !hiding });
      } catch (e) {
        setAction({ state: 'error', message: qcErrorDetail(e) ?? `Could not ${hiding ? 'hide' : 'restore'} the event.` });
      }
    });
  }, [acceptRevision, applyToCaches, base, busy, flushSelected, getEvent, readList, refreshOutput, selectNow, setDraft]);

  /**
   * Resolve one QA issue through the legacy editor's endpoint (review metadata
   * only: no text/timing change, no output revision bump, no preview refresh).
   * The cached detail and the list row are patched in place from the response —
   * neither the event list nor the detail is refetched.
   */
  const resolveIssue = useCallback(async (issueId: number) => {
    const id = selectedIdRef.current;
    if (id === null) return;
    setResolvingIssueId(issueId);
    try {
      const res = await client.post<{ id: number; issues: QaIssue[] }>(
        `/projects/${projectId}/files/${fileId}/qa-issues/${issueId}/resolve`,
      );
      const summary = summarizeIssues(res.data.issues);
      queryClient.setQueryData<QcEventDetail>(
        [...qcKey(projectId, fileId), 'event', id],
        (old) => (old ? applyIssueResolved(old, issueId) : old),
      );
      for (const q of queryClient.getQueryCache().findAll({ queryKey: [...qcKey(projectId, fileId), 'events'] })) {
        queryClient.setQueryData<QcEventList>(q.queryKey, (old) => {
          if (!old) return old;
          return { ...old, events: old.events.map((e) => (e.id === id ? applyIssueSummary(e, summary) : e)) };
        });
      }
      setAction((a) => (a.state === 'error' ? IDLE : a));
    } catch (e) {
      setAction({ state: 'error', message: qcErrorDetail(e) ?? 'Could not resolve the issue.' });
    } finally {
      setResolvingIssueId(null);
    }
  }, [projectId, fileId, queryClient]);

  /**
   * Put the original AI translation back. A pending draft is saved first (the
   * usual flush gate), then the server restores it, locks the event and answers
   * with the authoritative row (text, flags, recomputed CPS); the preview and
   * the renderer follow like after any text edit.
   */
  const restoreAi = useCallback(async () => {
    const id = selectedIdRef.current;
    if (id === null) return;
    if (!(await flushSelected())) return;
    const event = getEvent(id);
    if (!event) return;
    setAction({ state: 'busy', message: null });
    await busy(async () => {
      try {
        const res = await client.post<QcEvent>(`${base}/events/${id}/restore-ai`);
        const saved = res.data;
        const revision = revisionOf(res.headers);
        queryClient.setQueryData<QcEventDetail>(
          [...qcKey(projectId, fileId), 'event', id],
          (old) => (old ? mergeEventIntoDetail(old, saved) : old),
        );
        applyToCaches(saved, false, revision);
        acceptRevision(revision);
        if (draftRef.current?.id === id) setDraft(draftFromEvent(saved));
        setAction(IDLE);
        // The server bumps the revision only when the text really changed.
        if ((saved.translated_text ?? '') !== (event.translated_text ?? '')) void refreshOutput({ fonts: true });
      } catch (e) {
        setAction({ state: 'error', message: qcErrorDetail(e) ?? 'Could not restore the AI translation.' });
      }
    });
  }, [acceptRevision, applyToCaches, base, busy, flushSelected, getEvent, projectId, fileId, queryClient, refreshOutput, setDraft]);

  /** Show-hidden toggle helper: leave a hidden selection that is about to vanish. */
  const dropHiddenSelection = useCallback(() => {
    const id = selectedIdRef.current;
    if (id !== null && getEvent(id)?.is_hidden) selectNow(null);
  }, [getEvent, selectNow]);

  const reload = useCallback(async () => {
    if (!(await flushSelected())) return;
    setReloading(true);
    try {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [...qcKey(projectId, fileId), 'events'], refetchType: 'active' }),
        queryClient.invalidateQueries({ queryKey: [...qcKey(projectId, fileId), 'event'] }),
      ]);
      const fresh = readList();
      if (fresh) {
        knownRef.current = fresh.output_revision;
        setKnownState(fresh.output_revision);
        const id = selectedIdRef.current;
        const still = id !== null ? fresh.events.find((e) => e.id === id) : undefined;
        if (!newDraftRef.current) selectNow(still ?? null);
      }
      await refreshOutput({ fonts: true });
      const shown = queryClient.getQueryData<{ revision: number }>(qcPreviewKey(projectId, fileId));
      if (fresh && shown && shown.revision !== fresh.output_revision) latest.current.markStale(fresh.output_revision);
    } finally {
      setReloading(false);
    }
  }, [flushSelected, queryClient, projectId, fileId, readList, refreshOutput, selectNow]);

  const getVideoTimeMs = useCallback((): number | null => {
    const el = videoRef.current;
    return el ? videoTimeToMs(el.currentTime) : null;
  }, [videoRef]);

  // Warn before the tab is closed with an unsaved edit.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      const d = draftRef.current;
      const ev = d ? getEvent(d.id) : undefined;
      const dirtyNow = (d && ev && Object.keys(diffDraft(d, ev)).length > 0)
        || (newDraftRef.current && newInitialRef.current && !isUntouchedNewEvent(newDraftRef.current, newInitialRef.current))
        || inflight.current > 0;
      if (dirtyNow) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [getEvent]);

  const events = list.events;
  const selected = useMemo(
    () => (selectedId !== null ? events.find((e) => e.id === selectedId) ?? null : null),
    [events, selectedId],
  );
  const dirty = (() => {
    if (newDraft) return newInitial ? !isUntouchedNewEvent(newDraft, newInitial) : true;
    return draft !== null && selected !== null && draft.id === selected.id
      && Object.keys(diffDraft(draft, selected)).length > 0;
  })();

  return {
    selectedId, selected, draft, newDraft, newError, dirty,
    save, action, resolvingIssueId, preview, fontError, reloading, reveal, stale, known,
    setNewDraft, changeDraft, select, flushSelected, startNewEvent, cancelNewEvent,
    createEvent: () => createEvent(true),
    toggleHidden, resolveIssue, restoreAi, dropHiddenSelection, reload, retryPreview, getVideoTimeMs,
  };
}
