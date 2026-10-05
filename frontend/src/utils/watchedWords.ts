/**
 * Watched-word visuals shared by the Subtitle Editor and Final QC, so the two
 * editors can never drift apart. Pure constants (no React) so tests can pin them.
 */
export const WATCHED_WORD_BADGE = { size: 'xs', color: 'yellow', variant: 'light' } as const;
export const WATCHED_ROW_BACKGROUND = 'rgba(250, 176, 5, 0.08)';
export const watchedWordTitle = (word: string) => `Watched word: ${word}`;
