import { Center, Group, Loader, Modal, ScrollArea, Table, Text } from '@mantine/core';
import { ChartLine } from '@phosphor-icons/react';
import { useProjectMetrics } from '../hooks/useProjectMetrics';
import type { FileMetrics } from '../hooks/useProjectMetrics';

function pct(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`;
}

function cost(value: number | null): string {
  return value === null ? '—' : `$${value.toFixed(3)}`;
}

function editDistanceColor(value: number | null): string | undefined {
  if (value === null) return undefined;
  if (value < 0.02) return 'green';
  if (value < 0.1) return 'yellow';
  return 'red';
}

function MetricsRow({ m }: { m: FileMetrics }) {
  return (
    <Table.Tr>
      <Table.Td>
        <Text size="sm" truncate title={m.filename}>
          {m.episode_number !== null ? `Ep ${m.episode_number}` : m.filename}
        </Text>
      </Table.Td>
      <Table.Td><Text size="sm" c="dimmed">{m.events_total}</Text></Table.Td>
      <Table.Td>
        <Text size="sm" fw={600} c={editDistanceColor(m.edit_distance_norm)}>
          {pct(m.edit_distance_norm)}
        </Text>
      </Table.Td>
      <Table.Td><Text size="sm" c="dimmed">{m.events_user_edited}</Text></Table.Td>
      <Table.Td>
        <Text size="sm" c="dimmed">
          {m.polish_edit_count}
          {m.polish_churn_norm !== null && ` (${pct(m.polish_churn_norm)})`}
        </Text>
      </Table.Td>
      <Table.Td>
        <Group gap={6} wrap="nowrap">
          {m.qa_blockers > 0 && <Text size="xs" c="red">{m.qa_blockers}</Text>}
          {m.qa_warnings > 0 && <Text size="xs" c="yellow">{m.qa_warnings}</Text>}
          {m.qa_info > 0 && <Text size="xs" c="blue">{m.qa_info}</Text>}
          {m.qa_blockers + m.qa_warnings + m.qa_info === 0 && <Text size="xs" c="dimmed">0</Text>}
        </Group>
      </Table.Td>
      <Table.Td><Text size="sm" c="dimmed">{pct(m.mean_confidence)}</Text></Table.Td>
      <Table.Td><Text size="sm" c="dimmed">{cost(m.llm_cost_usd)}</Text></Table.Td>
      <Table.Td>
        <Text size="xs" c="dimmed">
          {((m.prompt_tokens + m.completion_tokens) / 1000).toFixed(0)}k
        </Text>
      </Table.Td>
    </Table.Tr>
  );
}

interface MetricsDialogProps {
  projectId: number;
  opened: boolean;
  onClose: () => void;
}

export function MetricsDialog({ projectId, opened, onClose }: MetricsDialogProps) {
  const { data: metrics, isLoading } = useProjectMetrics(projectId, opened);

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={<Group gap="xs"><ChartLine size={18} /><Text fw={700}>Quality metrics</Text></Group>}
      size="62rem"
    >
      <Text size="xs" c="dimmed" mb="sm">
        Computed when a file completes. <b>Human edits</b> is the mean edit distance between the
        AI output and what shipped — falling across episodes means the glossary, translation
        memory, and style bible are working. <b>Polish edits</b> shows how many lines the polish
        pass rewrote (and how heavily, as mean edit distance). QA counts are lifetime
        (including resolved).
      </Text>
      {isLoading ? (
        <Center py="xl"><Loader size="sm" /></Center>
      ) : !metrics || metrics.length === 0 ? (
        <Center py="xl">
          <Text size="sm" c="dimmed">No metrics yet — they appear as soon as a file has translated lines and update as it progresses.</Text>
        </Center>
      ) : (
        <ScrollArea.Autosize mah="65vh">
          <Table verticalSpacing={6} withRowBorders={false}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Episode</Table.Th>
                <Table.Th style={{ width: 60 }}>Lines</Table.Th>
                <Table.Th style={{ width: 110 }}>Human edits</Table.Th>
                <Table.Th style={{ width: 70 }}>Edited</Table.Th>
                <Table.Th style={{ width: 90 }}>Polish edits</Table.Th>
                <Table.Th style={{ width: 90 }}>QA (b/w/i)</Table.Th>
                <Table.Th style={{ width: 100 }}>Confidence</Table.Th>
                <Table.Th style={{ width: 80 }}>Cost</Table.Th>
                <Table.Th style={{ width: 70 }}>Tokens</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {metrics.map((m) => <MetricsRow key={m.file_id} m={m} />)}
            </Table.Tbody>
          </Table>
        </ScrollArea.Autosize>
      )}
    </Modal>
  );
}
