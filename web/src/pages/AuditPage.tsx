import { Badge, Button, Card, Divider, Group, Menu, Select, Stack, Table, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { IconDownload } from "@tabler/icons-react";

import { api } from "../api/client";
import { saveBlob } from "../lib/download";
import { EmptyState, EnvBadge } from "../components/common";
import { useConfig } from "../hooks/useFleet";
import { useEnv } from "../state/env";
import { useViewState } from "../state/views";
import { actionColor, actionLabel } from "../lib/actions";
import { fmtDateTime } from "../lib/format";

const STATUS_COLOR: Record<string, string> = { ok: "teal", started: "blue", failed: "red", denied: "red", cancelled: "gray" };
const PAGE = 100;

const SINCE = [
  { value: "1d", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
];

export default function AuditPage() {
  const config = useConfig();
  const { envNames } = useEnv();
  // The newest PAGE entries to begin with: the trail grows without bound, and the last page of it
  // is the part anyone opens this screen to read.
  // Both kept between visits, like every page's filters (state/views).
  const [limit, setLimit] = useViewState("audit.limit", PAGE);
  const [action, setAction] = useViewState<string | null>("audit.action", null);
  const [since, setSince] = useViewState<string | null>("audit.since", null);
  // Scoped like every other view, and filtered by the server over the whole trail. An entry with
  // no environment — a settings change, a sound toggle — belongs to none, so it is always shown.
  const query = { action, since, envs: envNames };
  const audit = useQuery({
    queryKey: ["audit", limit, action, since, envNames],
    queryFn: () => api.audit({ ...query, limit }),
    refetchInterval: 20_000,
    placeholderData: keepPreviousData, // the table stays put while a longer page loads
  });
  const exported = useMutation({
    mutationFn: (format: "csv" | "jsonl") => api.auditExport(format, query),
    onSuccess: ({ blob, name }) => saveBlob(blob, name),
    onError: (error) => notifications.show({ title: "Not exported", message: String((error as Error).message), color: "red" }),
  });

  const rows = audit.data?.entries ?? [];
  const total = audit.data?.total ?? 0;
  const actions = audit.data?.actions ?? [];
  const more = total - rows.length;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <div>
          <Text fw={700} size="lg">
            Audit trail
          </Text>
          <Text size="sm" c="dimmed">
            Every restart, upload, download, tunnel change and opened shell, newest first, for the environments in view.
            Actions marked <Text span c="dimmed">—</Text> are fleet-wide and always shown. Stored locally in
            <Text span className="mono">
              {" "}
              ~/.local/state/sshfleet/audit.jsonl
            </Text>
            .
          </Text>
        </div>
        <Group gap="xs" align="flex-end">
          <Select size="sm" placeholder="Any time" clearable value={since} onChange={setSince} data={SINCE} w={160} />
          <Select
            size="sm"
            placeholder="All actions"
            clearable
            searchable
            value={action}
            onChange={setAction}
            data={actions.map((value) => ({ value, label: actionLabel(value) }))}
            w={240}
          />
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <Button size="sm" variant="default" leftSection={<IconDownload size={15} />} loading={exported.isPending}>
                Export
              </Button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>What is shown, every entry of it</Menu.Label>
              <Menu.Item onClick={() => exported.mutate("csv")}>CSV — for a spreadsheet</Menu.Item>
              <Menu.Item onClick={() => exported.mutate("jsonl")}>JSON Lines — for a script</Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </Group>
      </Group>
      <Card withBorder padding={0}>
        {rows.length === 0 ? (
          <EmptyState title={action || since ? "Nothing matches" : "No actions recorded yet"} />
        ) : (
          <Table.ScrollContainer minWidth={900}>
            <Table verticalSpacing="xs" highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>When</Table.Th>
                  <Table.Th>Action</Table.Th>
                  <Table.Th>Env</Table.Th>
                  <Table.Th>Host</Table.Th>
                  <Table.Th>Target</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th>Note</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {rows.map((entry, index) => (
                  <Table.Tr key={`${entry.ts}-${index}`}>
                    <Table.Td>
                      <Text size="xs" className="mono" c="dimmed" style={{ whiteSpace: "nowrap" }}>
                        {fmtDateTime(entry.ts)}
                      </Text>
                    </Table.Td>
                    <Table.Td className="nowrap">
                      <Tooltip label={entry.action} openDelay={400}>
                        <Badge size="sm" variant="light" color={actionColor(entry.action)} style={{ cursor: "default" }}>
                          {actionLabel(entry.action)}
                        </Badge>
                      </Tooltip>
                    </Table.Td>
                    <Table.Td>
                      {entry.env ? (
                        <EnvBadge env={entry.env} environments={config.data?.environments} size="xs" />
                      ) : (
                        <Tooltip label="A fleet-wide action: it belongs to no one environment" openDelay={400}>
                          <Text size="xs" c="dimmed" style={{ cursor: "default" }}>
                            —
                          </Text>
                        </Tooltip>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" className="mono">
                        {entry.host ?? ""}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Tooltip label={entry.command ?? ""} disabled={!entry.command} multiline maw={600}>
                        <Text size="sm" className="mono" style={{ maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {entry.target ?? ""}
                        </Text>
                      </Tooltip>
                    </Table.Td>
                    <Table.Td>
                      {entry.status && (
                        <Badge size="sm" variant="light" color={STATUS_COLOR[entry.status] ?? "gray"}>
                          {entry.status}
                        </Badge>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Text size="xs" c="dimmed">
                        {entry.note ?? (entry.duration_ms ? `${(entry.duration_ms / 1000).toFixed(1)}s` : "")}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
        {rows.length > 0 && (
          <>
            <Divider />
            <Group justify="space-between" px="md" py="xs">
              <Text size="xs" c="dimmed">
                {rows.length} of {total} {action || since ? "matching entries" : "entries"}
              </Text>
              {more > 0 && (
                <Button
                  size="compact-sm"
                  variant="subtle"
                  loading={audit.isFetching}
                  onClick={() => setLimit((current) => current + PAGE)}
                >
                  Load {Math.min(more, PAGE)} older
                </Button>
              )}
            </Group>
          </>
        )}
      </Card>
    </Stack>
  );
}
