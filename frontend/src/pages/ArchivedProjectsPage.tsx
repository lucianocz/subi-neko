import { useDeferredValue, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ActionIcon,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Menu,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { ArchiveIcon, ArrowCounterClockwise, DotsThreeVertical, MagnifyingGlass, Trash } from '@phosphor-icons/react';
import { RemoveProjectModal } from '../components/Project/RemoveProjectModal';
import { useArchivedProjects, useUnarchiveProject } from '../hooks/useProjects';
import type { ArchivedProject } from '../types';
import { apiErrorMessage, filterArchivedProjects, secondaryTitle } from '../utils/projectArchive';

export function ArchivedProjectsPage() {
  const navigate = useNavigate();
  const { data: archived = [], isLoading, isError } = useArchivedProjects();
  const unarchive = useUnarchiveProject();
  const [search, setSearch] = useState('');
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [removing, setRemoving] = useState<ArchivedProject | null>(null);

  // Filtering all loaded rows is cheap; deferring keeps typing responsive for
  // large archives. No backend request is made while searching.
  const deferredSearch = useDeferredValue(search);
  const rows = useMemo(() => filterArchivedProjects(archived, deferredSearch), [archived, deferredSearch]);

  function handleUnarchive(project: ArchivedProject) {
    setPendingId(project.id);
    unarchive.mutate(project.id, {
      onSuccess: () => notifications.show({
        color: 'green',
        message: (
          <Group gap="xs" wrap="nowrap">
            <Text size="sm">“{project.name}” was restored.</Text>
            <Button
              size="compact-xs"
              variant="light"
              onClick={() => navigate(`/projects/${project.id}`)}
            >
              Open project
            </Button>
          </Group>
        ),
      }),
      onError: (err) => notifications.show({
        color: 'red',
        message: apiErrorMessage(err, 'Could not unarchive the project.'),
      }),
      onSettled: () => setPendingId(null),
    });
  }

  return (
    <Stack gap="md">
      <RemoveProjectModal project={removing} onClose={() => setRemoving(null)} />

      <Group justify="space-between" align="center">
        <Title order={3}>Archived projects</Title>
        <TextInput
          placeholder="Search title or folder…"
          aria-label="Search archived projects"
          leftSection={<MagnifyingGlass size={16} />}
          value={search}
          onChange={(e) => setSearch(e.currentTarget.value)}
          w={320}
        />
      </Group>

      {isLoading ? (
        <Center py="xl"><Loader size="sm" /></Center>
      ) : isError ? (
        <Text size="sm" c="red">Could not load archived projects.</Text>
      ) : archived.length === 0 ? (
        <Stack align="center" gap={4} py="xl">
          <ArchiveIcon size={32} color="var(--mantine-color-dimmed)" />
          <Text c="dimmed">No archived projects.</Text>
          <Text size="sm" c="dimmed">
            Finished projects can be archived from the ⋮ menu of the project page.
          </Text>
        </Stack>
      ) : rows.length === 0 ? (
        <Text size="sm" c="dimmed" py="md">No archived project matches “{search.trim()}”.</Text>
      ) : (
        <Table striped highlightOnHover style={{ tableLayout: 'fixed' }}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Project</Table.Th>
              <Table.Th style={{ width: 80 }}>Files</Table.Th>
              <Table.Th style={{ width: '28%' }}>Source folder</Table.Th>
              <Table.Th style={{ width: 170 }}>Archived</Table.Th>
              <Table.Th style={{ width: 48 }} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.map((project) => {
              const english = secondaryTitle(project);
              return (
                <Table.Tr key={project.id}>
                  <Table.Td>
                    <Text size="sm" fw={600} truncate title={project.name}>{project.name}</Text>
                    {english && <Text size="xs" c="dimmed" truncate title={english}>{english}</Text>}
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{project.file_count}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" ff="monospace" truncate title={project.source_directory}>
                      {project.source_directory}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" c="dimmed">{new Date(project.archived_at).toLocaleString()}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Box ta="right">
                      <Menu position="bottom-end" withinPortal>
                        <Menu.Target>
                          <ActionIcon
                            variant="subtle"
                            color="gray"
                            aria-label={`Actions for ${project.name}`}
                            loading={pendingId === project.id}
                          >
                            <DotsThreeVertical size={18} weight="bold" />
                          </ActionIcon>
                        </Menu.Target>
                        <Menu.Dropdown>
                          <Menu.Item
                            leftSection={<ArrowCounterClockwise size={14} />}
                            onClick={() => handleUnarchive(project)}
                          >
                            Unarchive
                          </Menu.Item>
                          <Menu.Divider />
                          <Menu.Item
                            color="red"
                            leftSection={<Trash size={14} />}
                            onClick={() => setRemoving(project)}
                          >
                            Delete
                          </Menu.Item>
                        </Menu.Dropdown>
                      </Menu>
                    </Box>
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
