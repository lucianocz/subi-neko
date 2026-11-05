import { Group, Image, Loader, Paper, Stack, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { titleQuery } from '../../hooks/useTmdbTitle';
import type { SearchResult } from '../../hooks/useWebPublishing';
import { posterUrl } from '../../utils/tmdbShared';

/** One TMDB search hit (shared by the WEB Publish wizard and the import wizard). */
export function TmdbSearchCard({ result, busy, selected, onSelect }: {
  result: SearchResult; busy: boolean; selected: boolean; onSelect: () => void;
}) {
  const detail = useQuery(titleQuery(result.media_type, result.id));
  const names = detail.data?.romaji_titles ?? (result.title_romaji ? [result.title_romaji] : []);
  const seasons = detail.data?.number_of_seasons ?? result.number_of_seasons;
  return <Paper component="button" type="button" p="sm" withBorder disabled={busy} onClick={onSelect}
    aria-pressed={selected} style={{ width: '100%', textAlign: 'left', cursor: busy ? 'wait' : 'pointer',
      color: 'inherit', borderColor: selected ? 'var(--mantine-primary-color-filled)' : undefined }}>
    <Group wrap="nowrap">{result.poster_path && <Image src={posterUrl(result.poster_path)} w={48} h={72} fit="cover" alt="" />}
      <Stack gap={3} style={{ flex: 1, minWidth: 0 }}><Text fw={600} lineClamp={2}>{result.title}</Text>
        {names.length > 0 && <Text size="sm" c="dimmed" lineClamp={2}>{names.join(' / ')}</Text>}
        <Text size="xs" c="dimmed">{[result.year, result.media_type === 'tv' ? 'TV' : 'Movie',
          seasons != null ? `${seasons} season${seasons === 1 ? '' : 's'}` : null].filter(Boolean).join(' · ')}</Text>
      </Stack>{busy && selected && <Loader size="xs" />}</Group>
  </Paper>;
}
