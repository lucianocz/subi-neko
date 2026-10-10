import { useMemo, useState } from 'react';
import {
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
  Textarea,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { ListChecks } from '@phosphor-icons/react';
import type { ReviewQueueItem } from '../types';
import {
  useBulkResolve,
  useResolveQueueItem,
  useReviewQueue,
  useSaveQueueTranslation,
} from '../hooks/useReviewQueue';
import './reviewQueue.css';

const SEVERITY_COLORS: Record<string, string> = {
  blocker: 'red',
  warning: 'yellow',
  info: 'blue',
};

function QueueRow({
  item,
  projectId,
}: {
  item: ReviewQueueItem;
  projectId: number;
}) {
  const resolve = useResolveQueueItem(projectId);
  const save = useSaveQueueTranslation(projectId);
  const [draft, setDraft] = useState(item.translated_text ?? '');
  const [syncedText, setSyncedText] = useState(item.translated_text);

  // Render-phase sync: refresh the draft when the server text changes.
  if (item.translated_text !== syncedText) {
    setSyncedText(item.translated_text);
    setDraft(item.translated_text ?? '');
  }

  async function handleResolve() {
    try {
      await resolve.mutateAsync({ fileId: item.file_id, issueId: item.id });
    } catch {
      notifications.show({ color: 'red', message: 'Could not resolve the issue.' });
    }
  }

  async function handleSaveAndResolve() {
    try {
      if (item.event_id !== null && draft !== (item.translated_text ?? '')) {
        await save.mutateAsync({
          fileId: item.file_id,
          eventId: item.event_id,
          translatedText: draft,
        });
      }
      await handleResolve();
    } catch {
      notifications.show({ color: 'red', message: 'Could not save the translation.' });
    }
  }

  return (
    <Stack gap={6} p="sm" className="rq-card">
      <Group gap="xs" wrap="nowrap">
        <Badge size="xs" variant="filled" color={SEVERITY_COLORS[item.severity] ?? 'gray'}>
          {item.severity}
        </Badge>
        <Badge size="xs" variant="light" color="gray">{item.qa_type.replace(/_/g, ' ')}</Badge>
        {item.translation_confidence !== null && (
          <Badge size="xs" variant="light" color="grape">
            conf {Math.round(item.translation_confidence * 100)}%
          </Badge>
        )}
        <Text size="xs" c="dimmed" truncate className="rq-meta" style={{ flex: 1 }}>
          {item.filename}{item.line_index !== null ? ` · line ${item.line_index + 1}` : ''}
          {item.speaker ? ` · ${item.speaker}` : ''}
        </Text>
      </Group>
      <Text size="xs" c="dimmed" className="rq-text">{item.message}</Text>
      {item.source_text !== null && (
        <Text size="sm" c="dimmed" className="rq-text">
          {item.source_text}
        </Text>
      )}
      {item.event_id !== null ? (
        <Textarea
          size="sm"
          autosize
          minRows={1}
          maxRows={4}
          classNames={{ input: 'rq-textarea' }}
          value={draft}
          onChange={(e) => setDraft(e.currentTarget.value)}
        />
      ) : null}
      <Group gap="xs" justify="flex-end">
        <Button
          size="compact-xs"
          variant="subtle"
          loading={resolve.isPending}
          onClick={() => void handleResolve()}
        >
          Resolve
        </Button>
        {item.event_id !== null && (
          <Button
            size="compact-xs"
            variant="light"
            color="green"
            loading={save.isPending}
            onClick={() => void handleSaveAndResolve()}
          >
            Save & resolve
          </Button>
        )}
      </Group>
    </Stack>
  );
}

interface ReviewQueueDialogProps {
  projectId: number;
  opened: boolean;
  onClose: () => void;
}

export function ReviewQueueDialog({ projectId, opened, onClose }: ReviewQueueDialogProps) {
  const { data, isLoading } = useReviewQueue(projectId, opened);
  const bulkResolve = useBulkResolve(projectId);

  const items = useMemo(() => data?.items ?? [], [data]);

  const warningCount = items.filter((i) => i.severity !== 'blocker').length;

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={
        <Group gap="xs">
          <ListChecks size={18} />
          <Text fw={700}>Review queue</Text>
          {data && <Badge size="sm" variant="light">{data.total} unresolved</Badge>}
        </Group>
      }
      size="60rem"
    >
      <Stack gap="sm">
        <Group justify="space-between">
          <Text size="xs" c="dimmed">
            Most severe and least confident first.
          </Text>
          {warningCount > 0 && (
            <Button
              size="compact-xs"
              variant="subtle"
              color="yellow"
              loading={bulkResolve.isPending}
              onClick={() => {
                if (window.confirm(`Resolve all ${warningCount} non-blocker issue(s) in this project?`)) {
                  void bulkResolve.mutateAsync({ severity: 'warning' })
                    .then(() => bulkResolve.mutateAsync({ severity: 'info' }));
                }
              }}
            >
              Resolve all warnings
            </Button>
          )}
        </Group>
        {isLoading ? (
          <Center py="xl"><Loader size="sm" /></Center>
        ) : items.length === 0 ? (
          <Center py="xl">
            <Text size="sm" c="dimmed">Nothing to review — all clear.</Text>
          </Center>
        ) : (
          <ScrollArea.Autosize mah="65vh" scrollbars="y">
            <Stack gap="xs">
              {items.map((item) => (
                <QueueRow key={item.id} item={item} projectId={projectId} />
              ))}
            </Stack>
          </ScrollArea.Autosize>
        )}
      </Stack>
    </Modal>
  );
}
