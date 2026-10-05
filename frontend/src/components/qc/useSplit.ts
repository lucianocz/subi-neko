import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, RefObject } from 'react';

const STORAGE_KEY = 'subi-neko.qc.layout.v1';

type LayoutKey = 'h' | 'v';

function readStored(key: LayoutKey): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const value = raw ? (JSON.parse(raw) as Partial<Record<LayoutKey, unknown>>)[key] : null;
    return typeof value === 'number' && value > 0 && value < 1 ? value : null;
  } catch {
    return null;
  }
}

function store(key: LayoutKey, value: number) {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const all = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...all, [key]: value }));
  } catch {
    // Persistence is a convenience only (private mode, blocked storage, ...).
  }
}

interface SplitOptions {
  /** `h`: left|right (drag along x). `v`: top|bottom (drag along y). */
  key: LayoutKey;
  defaultFraction: number;
  /** Minimum size in px of the first / second pane. */
  minFirst: number;
  minSecond: number;
}

/**
 * Draggable splitter state: the first pane's share (0..1) of `containerRef`,
 * persisted in localStorage (never the DB). Updates are rAF-throttled and the
 * value is saved on release, so dragging stays smooth.
 */
export function useSplit(containerRef: RefObject<HTMLElement | null>, opts: SplitOptions) {
  const { key, defaultFraction, minFirst, minSecond } = opts;
  const horizontal = key === 'h';
  const [fraction, setFraction] = useState(() => readStored(key) ?? defaultFraction);
  const [dragging, setDragging] = useState(false);
  const latest = useRef(fraction);
  const raf = useRef<number | null>(null);

  const clamp = useCallback((value: number, total: number) => {
    if (total <= 0) return value;
    const lo = Math.min(minFirst / total, 0.5);
    const hi = Math.max(1 - minSecond / total, 0.5);
    return Math.min(hi, Math.max(lo, value));
  }, [minFirst, minSecond]);

  const apply = useCallback((value: number) => {
    latest.current = value;
    if (raf.current === null) {
      raf.current = requestAnimationFrame(() => {
        raf.current = null;
        setFraction(latest.current);
      });
    }
  }, []);

  useEffect(() => () => {
    if (raf.current !== null) cancelAnimationFrame(raf.current);
  }, []);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(true);
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const box = containerRef.current?.getBoundingClientRect();
    if (!dragging || !box) return;
    const total = horizontal ? box.width : box.height;
    const pos = horizontal ? e.clientX - box.left : e.clientY - box.top;
    apply(clamp(pos / total, total));
  };
  const finish = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    setDragging(false);
    store(key, latest.current);
  };
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const box = containerRef.current?.getBoundingClientRect();
    const dec = horizontal ? 'ArrowLeft' : 'ArrowUp';
    const inc = horizontal ? 'ArrowRight' : 'ArrowDown';
    if (!box || (e.key !== dec && e.key !== inc)) return;
    e.preventDefault();
    const total = horizontal ? box.width : box.height;
    const next = clamp(latest.current + (e.key === inc ? 0.02 : -0.02), total);
    latest.current = next;
    setFraction(next);
    store(key, next);
  };

  return {
    fraction,
    dragging,
    handleProps: {
      role: 'separator' as const,
      'aria-orientation': (horizontal ? 'vertical' : 'horizontal') as 'vertical' | 'horizontal',
      'aria-valuenow': Math.round(fraction * 100),
      tabIndex: 0,
      onPointerDown,
      onPointerMove,
      onPointerUp: finish,
      onPointerCancel: finish,
      onKeyDown,
    },
  };
}
