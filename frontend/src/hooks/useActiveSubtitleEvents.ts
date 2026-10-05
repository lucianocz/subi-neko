import { useEffect, useMemo, useRef, useState } from 'react';
import type { QcEvent } from '../types/qc';
import { buildActiveIndex, findActiveIndices, sameNumberList } from '../utils/qcActiveIndex';

export interface ActiveEvents {
  /** Positions in the events array (ascending = by start time). */
  indices: number[];
  ids: ReadonlySet<number>;
}

const NONE: ActiveEvents = { indices: [], ids: new Set() };

/**
 * Which events are on screen right now, following the video clock.
 *
 * - Clock: `requestVideoFrameCallback` (its `mediaTime` is the presented frame's
 *   timestamp — the same time the renderer draws at). Without it: a
 *   requestAnimationFrame loop while playing plus `seeked`/`timeupdate`/`pause`.
 * - The current time is never kept in React state. State only changes when the
 *   *set* of active events changes, so a normal frame re-renders nothing.
 * - The index is rebuilt only when the events array identity changes.
 */
export function useActiveSubtitleEvents(video: HTMLVideoElement | null, events: readonly QcEvent[]) {
  const index = useMemo(() => buildActiveIndex(events), [events]);
  const [active, setActive] = useState<ActiveEvents>(NONE);
  const lastIndices = useRef<number[] | null>(null);

  useEffect(() => {
    if (!video) return;
    lastIndices.current = null; // events changed: force the next update through

    const update = (timeSec: number) => {
      const indices = findActiveIndices(index, timeSec * 1000);
      if (lastIndices.current && sameNumberList(indices, lastIndices.current)) return;
      lastIndices.current = indices;
      setActive(indices.length === 0
        ? NONE
        : { indices, ids: new Set(indices.map((i) => events[i].id)) });
    };

    let frameCb: number | null = null;
    let raf: number | null = null;
    const hasRvfc = typeof video.requestVideoFrameCallback === 'function';

    const onFrame: VideoFrameRequestCallback = (_now, meta) => {
      update(meta.mediaTime);
      frameCb = video.requestVideoFrameCallback(onFrame);
    };
    const rafLoop = () => {
      update(video.currentTime);
      raf = video.paused ? null : requestAnimationFrame(rafLoop);
    };
    const onTimeEvent = () => update(video.currentTime);
    const onPlay = () => {
      if (!hasRvfc && raf === null) raf = requestAnimationFrame(rafLoop);
    };

    if (hasRvfc) frameCb = video.requestVideoFrameCallback(onFrame);
    video.addEventListener('seeked', onTimeEvent);
    if (!hasRvfc) video.addEventListener('timeupdate', onTimeEvent);
    video.addEventListener('loadeddata', onTimeEvent);
    video.addEventListener('emptied', onTimeEvent);
    video.addEventListener('play', onPlay);
    // Initial/after-events-change sync, off the effect's synchronous path.
    const initial = requestAnimationFrame(() => update(video.currentTime));
    if (!hasRvfc && !video.paused) onPlay();

    return () => {
      cancelAnimationFrame(initial);
      if (frameCb !== null) video.cancelVideoFrameCallback(frameCb);
      if (raf !== null) cancelAnimationFrame(raf);
      video.removeEventListener('seeked', onTimeEvent);
      video.removeEventListener('timeupdate', onTimeEvent);
      video.removeEventListener('loadeddata', onTimeEvent);
      video.removeEventListener('emptied', onTimeEvent);
      video.removeEventListener('play', onPlay);
    };
  }, [video, index, events]);

  return active;
}
