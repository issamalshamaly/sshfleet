import { Badge, Button, Card, Divider, Group, Select, Stack, Table, Text, Tooltip } from "@mantine/core";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "../api/client";
import { EmptyState, EnvBadge } from "../components/common";
import { useConfig } from "../hooks/useFleet";
import { useEnv } from "../state/env";
import { actionColor, actionLabel } from "../lib/actions";
import { fmtDateTime } from "../lib/format";

const STATUS_COLOR: Record<string, string> = { ok: "teal", started: "blue", failed: "red", denied: "red", cancelled: "gray" };
const PAGE = 100;

export default function AuditPage() {
  const config = useConfig();
  const { inScope } = useEnv();
  // The newest PAGE entries to begin with: the trail grows without bound, and the last page of it
  // is the part anyone opens this screen to read.
  const [limit, setLimit] = useState(PAGE);
  const [action, setAction] = useState<string | null>(null);
  const audit = useQuery({
    queryKey: ["audit", limit],
    queryFn: () => api.audit(limit),
    refetchInterval: 20_000,
    placeholderData: keepPreviousData, // the table stays put while a longer page loads
  });

  // Scoped like every other view. An entry with no environment — a settings change, a sound
  // toggle — belongs to no environment at all, so narrowing to one must not hide it.
  const loaded = audit.data?.entries ?? [];
  const entries = loaded.filter((e) => !e.env || inScope(e.env));
  const total = audit.data?.total ?? 0;
  const actions = useMemo(() => Array.from(new Set(entries.map((e) => e.action))).sort(), [entries]);
  const rows = entries.filter((e) => !action || e.action === action);
  const more = total - loaded.length; // what is left on disk, not what survived the filter

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
        <Select
          size="sm"
          placeholder="All actions"
          clearable
          value={action}
          onChange={setAction}
          data={actions.map((value) => ({ value, label: actionLabel(value) }))}
          w={240}
        />
      </Group>
      <Card withBorder padding={0}>
        {rows.length === 0 ? (
          <EmptyState title="No actions recorded yet" />
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
        {loaded.length > 0 && (
          <>
            <Divider />
            <Group justify="space-between" px="md" py="xs">
              <Text size="xs" c="dimmed">
                {action ? `${rows.length} of ${entries.length} shown` : `${entries.length} of ${loaded.length} loaded`}
                {action && more > 0 ? " — filtering applies to loaded entries only" : ""}
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
