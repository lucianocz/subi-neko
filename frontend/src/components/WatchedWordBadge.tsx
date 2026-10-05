import { Badge } from '@mantine/core';
import { WATCHED_WORD_BADGE, watchedWordTitle } from '../utils/watchedWords';

/** One watched-word chip — the single rendering used by every editor. */
export function WatchedWordBadge({ word }: { word: string }) {
  return (
    <Badge {...WATCHED_WORD_BADGE} title={watchedWordTitle(word)}>
      {word}
    </Badge>
  );
}
