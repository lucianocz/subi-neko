import { useCallback, useEffect, useRef, useState } from 'react';
import JASSUB from 'jassub';
// Explicit URLs: the Vite build emits the worker + WASM as hashed assets under
// /assets, same-origin, so no dev-server-only `import.meta.url` resolution is
// relied on (JASSUB's defaults are only used when these options are absent).
import workerUrl from 'jassub/dist/worker/worker.js?worker&url';
import wasmUrl from 'jassub/dist/wasm/jassub-worker.wasm?url';
import modernWasmUrl from 'jassub/dist/wasm/jassub-worker-modern.wasm?url';
import defaultFontUrl from 'jassub/dist/default.woff2?url';

async function fetchBytes(url: string): Promise<Uint8Array> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Could not load ${url}: HTTP ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}

export type JassubStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface JassubControls {
  status: JassubStatus;
  error: string | null;
  /**
   * Swap the ASS track on the live renderer (no re-creation). When the video is
   * paused the frame is repainted explicitly, otherwise the old overlay would
   * stay until the next seek. Resolves without doing anything if the renderer
   * failed to start (the page already shows that); rejects if the swap fails.
   */
  setTrack(ass: string): Promise<void>;
  /** Register extra fonts (additive; JASSUB cannot unload). Call BEFORE setTrack. */
  addFonts(fonts: readonly Uint8Array[]): Promise<void>;
  /** Re-measure the live renderer after a layout change (e.g. fullscreen). No-op if it isn't running. */
  resize(): Promise<void>;
}

/**
 * Owns ONE JASSUB renderer per video element.
 *
 * It is built once, from the first non-null `ass` + `fonts` (the PoC's hard
 * requirement: every font is already in memory before the renderer exists, so
 * the track is never visible without its fonts). Later changes to `ass`/`fonts`
 * are ignored by construction — the editing flow updates the live instance via
 * `setTrack` / `addFonts` instead, so the video element is never recreated. No
 * local-font queries and no remote font lookups (`queryFonts: false`).
 *
 * Unmount (incl. StrictMode's simulated one) destroys the instance; a build that
 * finishes after cancellation destroys itself, so two renderers never coexist.
 */
export function useJassub(
  video: HTMLVideoElement | null,
  ass: string | null,
  fonts: readonly Uint8Array[] | null,
): JassubControls {
  const [state, setState] = useState<{ video: HTMLVideoElement; error: string | null } | null>(null);
  const instanceRef = useRef<Promise<JASSUB> | null>(null);
  const initial = useRef({ ass, fonts });
  useEffect(() => {
    initial.current = { ass, fonts };
  });
  const ready = ass !== null && fonts !== null;

  useEffect(() => {
    if (!video || !ready) return;
    let cancelled = false;
    let instance: JASSUB | null = null;
    let resolveInstance: (j: JASSUB) => void = () => {};
    let rejectInstance: (e: unknown) => void = () => {};
    instanceRef.current = new Promise<JASSUB>((res, rej) => { resolveInstance = res; rejectInstance = rej; });
    instanceRef.current.catch(() => { /* surfaced through `error` */ });

    (async () => {
      try {
        // The fallback font (glyphs the file's fonts lack, e.g. Czech diacritics)
        // is preloaded like the others: left as a lazy `availableFonts` URL it would
        // load only on first miss and flash tofu boxes meanwhile.
        const fallback = await fetchBytes(defaultFontUrl);
        if (cancelled) return;
        const { ass: subContent, fonts: preload } = initial.current;
        const created = new JASSUB({
          video,
          subContent: subContent!,
          fonts: [...preload!, fallback],
          defaultFont: 'liberation sans',
          queryFonts: false,
          workerUrl,
          wasmUrl,
          modernWasmUrl,
        });
        instance = created;
        await created.ready;
        if (cancelled) return;
        resolveInstance(created);
        setState({ video, error: null });
      } catch (e) {
        if (cancelled) return;
        console.error('JASSUB initialization failed', e);
        rejectInstance(e);
        setState({ video, error: e instanceof Error ? e.message : String(e) });
      }
    })();

    return () => {
      cancelled = true;
      instanceRef.current = null;
      const toDestroy = instance;
      instance = null;
      toDestroy?.destroy().catch((e) => console.warn('JASSUB destroy failed', e));
    };
  }, [video, ready]);

  const setTrack = useCallback(async (text: string) => {
    const pending = instanceRef.current;
    if (!pending) return;
    let jassub: JASSUB;
    try {
      jassub = await pending;
    } catch {
      return; // renderer failed to start: nothing to update
    }
    await jassub.renderer.setTrack(text);
    // A paused video produces no frame callbacks, so force the repaint.
    if (video?.paused) await jassub.resize(true);
  }, [video]);

  const addFonts = useCallback(async (extra: readonly Uint8Array[]) => {
    const pending = instanceRef.current;
    if (!pending || extra.length === 0) return;
    let jassub: JASSUB;
    try {
      jassub = await pending;
    } catch {
      return;
    }
    await jassub.renderer.addFonts([...extra]);
  }, []);

  const resize = useCallback(async () => {
    const pending = instanceRef.current;
    if (!pending) return;
    try {
      await (await pending).resize(true);
    } catch {
      /* renderer failed to start or was destroyed meanwhile */
    }
  }, []);

  const current = state && state.video === video ? state : null;
  const status: JassubStatus = !video || !ready
    ? 'idle'
    : current === null ? 'loading' : current.error ? 'error' : 'ready';
  return { status, error: current?.error ?? null, setTrack, addFonts, resize };
}
