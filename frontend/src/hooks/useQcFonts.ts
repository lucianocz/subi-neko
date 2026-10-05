import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import client from '../api/client';
import type { QcFont, QcFontManifest } from '../types/qc';
import { QC_GC_TIME_MS, qcKey } from './useQcEvents';

export interface QcFontData {
  /** Raw bytes of every font that downloaded, one entry per face (bold/italic
   * faces of a family are separate entries — nothing is collapsed). */
  bytes: Uint8Array[];
  /** Ids of the fonts in `bytes`. */
  ids: string[];
  /** Manifest fonts whose download failed (filename), for the warning. */
  failed: string[];
}

async function fetchFont(font: QcFont): Promise<Uint8Array> {
  const res = await fetch(font.url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}

async function fetchManifest(projectId: number, fileId: number): Promise<QcFontManifest> {
  const { data } = await client.get<QcFontManifest>(`/projects/${projectId}/files/${fileId}/qc/fonts`);
  return data;
}

export interface FontRefreshResult {
  manifest: QcFontManifest;
  /** Font files that were newly downloaded and handed to `addFonts`. */
  added: number;
}

/**
 * Two steps, in this order: the manifest, then the bytes of every font in it.
 * The renderer is only created once BOTH are done (see useJassub) — a track shown
 * before its fonts are loaded renders nothing / flashes missing glyphs.
 * Neither step is allowed to block the page: failures become warnings.
 *
 * After an output-affecting edit `refresh` re-reads the manifest and downloads
 * ONLY the fonts the renderer does not hold yet (tracked by content-hash id in
 * `loaded`), handing them to `addFonts` before the caller swaps the track.
 * Fonts are never unloaded.
 */
export function useQcFonts(projectId: number, fileId: number) {
  const manifest = useQuery<QcFontManifest>({
    queryKey: [...qcKey(projectId, fileId), 'fonts'],
    queryFn: () => fetchManifest(projectId, fileId),
    staleTime: Infinity,
    gcTime: QC_GC_TIME_MS,
    retry: 1,
  });

  const fontIds = manifest.data?.fonts.map((f) => f.id).join(',');
  const data = useQuery<QcFontData>({
    queryKey: [...qcKey(projectId, fileId), 'font-data', fontIds],
    enabled: manifest.data != null,
    queryFn: async () => {
      const fonts = manifest.data!.fonts;
      const results = await Promise.allSettled(fonts.map(fetchFont));
      const bytes: Uint8Array[] = [];
      const ids: string[] = [];
      const failed: string[] = [];
      results.forEach((r, i) => {
        if (r.status === 'fulfilled') {
          bytes.push(r.value);
          ids.push(fonts[i].id);
        } else failed.push(fonts[i].filename);
      });
      return { bytes, ids, failed };
    },
    staleTime: Infinity,
    gcTime: QC_GC_TIME_MS,
    structuralSharing: false,
  });

  // The fonts the live renderer was built with, then those added since. The
  // initial query result is frozen on first use: later manifest changes must not
  // re-trigger the bulk load (the renderer is only ever extended incrementally).
  const loaded = useRef<Set<string> | null>(null);
  const initialData = data.data;
  useEffect(() => {
    if (loaded.current === null && initialData) loaded.current = new Set(initialData.ids);
  }, [initialData]);

  const [latest, setLatest] = useState<{ manifest: QcFontManifest; failed: string[] } | null>(null);

  const refresh = useCallback(async (
    addFonts: (fonts: readonly Uint8Array[]) => Promise<void>,
  ): Promise<FontRefreshResult> => {
    const next = await fetchManifest(projectId, fileId);
    const have = loaded.current ?? (loaded.current = new Set());
    const wanted = next.fonts.filter((f) => !have.has(f.id));
    const results = await Promise.allSettled(wanted.map(fetchFont));
    const bytes: Uint8Array[] = [];
    const okFonts: QcFont[] = [];
    results.forEach((r, i) => {
      if (r.status === 'fulfilled') {
        bytes.push(r.value);
        okFonts.push(wanted[i]);
      }
    });
    if (bytes.length > 0) await addFonts(bytes);
    okFonts.forEach((f) => have.add(f.id));
    setLatest({ manifest: next, failed: next.fonts.filter((f) => !have.has(f.id)).map((f) => f.filename) });
    return { manifest: next, added: bytes.length };
  }, [projectId, fileId]);

  return {
    manifest: latest?.manifest ?? manifest.data,
    manifestError: manifest.error,
    fontData: data.data,
    /** Fonts (by filename) in the current manifest that failed to download. */
    failed: latest?.failed ?? data.data?.failed ?? [],
    /** Settled one way or the other (data or failure) — safe to init the renderer. */
    ready: data.data != null || manifest.isError || data.isError,
    refresh,
  };
}
