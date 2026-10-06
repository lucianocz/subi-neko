import { useCallback, useEffect, useRef, useState } from 'react';
import { ActionIcon, Alert } from '@mantine/core';
import { ArrowsIn, ArrowsOut } from '@phosphor-icons/react';
import { readPlayerPrefs, writePlayerPrefs } from '../../utils/qcPlayerPrefs';
import './qc.css';

/** Height of the native control bar (clicks there are never "the picture"). */
const CONTROLS_STRIP_PX = 48;

const MEDIA_ERR_ABORTED = 1;
const MEDIA_ERR_NETWORK = 2;
const MEDIA_ERR_DECODE = 3;
const MEDIA_ERR_SRC_NOT_SUPPORTED = 4;

async function describeMediaError(video: HTMLVideoElement, src: string): Promise<string> {
  const code = video.error?.code;
  if (code === MEDIA_ERR_ABORTED) return 'Playback was aborted.';
  // The element can't tell "file missing" from "can't decode": ask the server.
  try {
    const res = await fetch(src, { method: 'HEAD' });
    if (res.status === 404) return 'The source media file was not found on the server.';
    if (!res.ok) return `The media endpoint answered HTTP ${res.status}.`;
  } catch {
    return 'The media endpoint could not be reached.';
  }
  if (code === MEDIA_ERR_SRC_NOT_SUPPORTED) return 'This browser cannot play the source container/codec.';
  if (code === MEDIA_ERR_DECODE) return 'The browser failed to decode the video (unsupported or corrupt stream).';
  if (code === MEDIA_ERR_NETWORK) return 'A network error interrupted loading the video.';
  return video.error?.message || 'The video could not be played.';
}

/**
 * Native `<video>` (no player framework) streaming the ORIGINAL source file.
 * The wrapper is the positioning context for the subtitle canvas JASSUB inserts
 * right after the element, and it — not the <video> — is what goes fullscreen:
 * a video-only fullscreen puts just that element in the top layer and hides the
 * canvas. The native fullscreen button is therefore removed (`controlsList`,
 * Chromium/Edge; other browsers may still show theirs) in favour of our own
 * toggle. `onFullscreenChange` fires after each transition so the renderer can
 * re-measure. The element is exposed through `onVideoElement` so the page can
 * drive seeking/keyboard and the renderer/clock can attach to it.
 */
export function QcVideoPlayer({
  src,
  onVideoElement,
  onFullscreenChange,
}: {
  src: string;
  onVideoElement: (el: HTMLVideoElement | null) => void;
  onFullscreenChange?: () => void;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const setVideoRef = useCallback((el: HTMLVideoElement | null) => {
    videoRef.current = el;
    onVideoElement(el);
  }, [onVideoElement]);

  // Volume/mute persist across files and sessions. Restoring only sets the
  // element's properties — never calls play(), so autoplay policy is untouched.
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const stored = readPlayerPrefs();
    if (stored) {
      video.volume = stored.volume;
      video.muted = stored.muted;
    }
    const onVolumeChange = () => writePlayerPrefs({ volume: video.volume, muted: video.muted });
    video.addEventListener('volumechange', onVolumeChange);
    return () => video.removeEventListener('volumechange', onVolumeChange);
  }, []);
  const [fullscreen, setFullscreen] = useState(false);
  const onChangeRef = useRef(onFullscreenChange);
  useEffect(() => { onChangeRef.current = onFullscreenChange; });

  useEffect(() => {
    let frame = 0;
    const onChange = () => {
      setFullscreen(document.fullscreenElement === wrapRef.current);
      // Let layout settle on the final size before the renderer measures it.
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => onChangeRef.current?.());
    };
    document.addEventListener('fullscreenchange', onChange);
    return () => {
      document.removeEventListener('fullscreenchange', onChange);
      cancelAnimationFrame(frame);
    };
  }, []);

  const toggleFullscreen = useCallback(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const op = document.fullscreenElement ? document.exitFullscreen() : wrap.requestFullscreen();
    op.catch((e) => console.warn('Fullscreen request failed', e));
  }, []);

  // Click on the picture = play/pause. Browsers differ in whether the native
  // controls already toggle on a click of the picture, so this never toggles
  // blindly: it only steps in when the press left playback state unchanged
  // (otherwise the native handler already did it -> no double toggle). Clicks
  // in the control bar strip and the 2nd click of a double-click are ignored.
  const pausedAtPress = useRef<boolean | null>(null);
  const onPicturePointerDown = useCallback((e: React.PointerEvent<HTMLVideoElement>) => {
    pausedAtPress.current = e.button === 0 ? e.currentTarget.paused : null;
  }, []);
  const onPictureClick = useCallback((e: React.MouseEvent<HTMLVideoElement>) => {
    const video = e.currentTarget;
    const before = pausedAtPress.current;
    pausedAtPress.current = null;
    if (before === null || e.detail > 1) return;
    const rect = video.getBoundingClientRect();
    if (e.clientY > rect.bottom - CONTROLS_STRIP_PX) return;
    if (video.paused !== before) return; // native controls already toggled
    if (video.paused) video.play().catch(() => { /* blocked or unsupported: the player shows why */ });
    else video.pause();
  }, []);

  const [errorState, setError] = useState<{ src: string; message: string } | null>(null);
  const error = errorState?.src === src ? errorState.message : null;

  const onError = useCallback((e: React.SyntheticEvent<HTMLVideoElement>) => {
    const video = e.currentTarget;
    describeMediaError(video, src).then((message) => setError({ src, message }));
  }, [src]);

  return (
    <div className="qc-video-wrap" ref={wrapRef}>
      <video
        ref={setVideoRef}
        className="qc-video"
        src={src}
        controls
        preload="metadata"
        playsInline
        controlsList="nofullscreen"
        disablePictureInPicture
        onPointerDown={onPicturePointerDown}
        onClick={onPictureClick}
        onError={onError}
        onLoadedData={() => setError(null)}
      />
      <ActionIcon
        className="qc-fullscreen-btn"
        variant="filled"
        color="dark"
        size="md"
        tabIndex={-1}
        aria-label={fullscreen ? 'Exit fullscreen' : 'Enter fullscreen'}
        title={fullscreen ? 'Exit fullscreen (Esc)' : 'Fullscreen'}
        // Keep focus off the button so Space still toggles playback.
        onMouseDown={(e) => e.preventDefault()}
        onClick={toggleFullscreen}
      >
        {fullscreen ? <ArrowsIn size={18} /> : <ArrowsOut size={18} />}
      </ActionIcon>
      {error && (
        <Alert color="red" title="Video unavailable" style={{ position: 'absolute', left: 8, right: 8, top: 8, zIndex: 2 }}>
          {error}
        </Alert>
      )}
    </div>
  );
}
