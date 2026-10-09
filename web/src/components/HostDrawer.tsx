import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Drawer,
  Group,
  Menu,
  Progress,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  Tooltip,
} from "@mantine/core";
import {
  IconActivity,
  IconChevronDown,
  IconEyeOff,
  IconFileText,
  IconFolders,
  IconPencil,
  IconPlayerPause,
  IconPlayerPlay,
  IconRotateClockwise,
  IconStar,
  IconStarFilled,
  IconTerminal2,
  IconTrash,
} from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useInvalidateSettings, usePauseHost } from "../hooks/useSettings";
import type { Container, Environment, Host } from "../api/types";
import { fmtAge, fmtBytes, fmtUptime, shortImage } from "../lib/format";
import { FRESH_LOGS, logsPath } from "../lib/navigation";
import { EmptyState, EnvBadge, ReachStatus, RoleBadge, SeverityBadge } from "./common";
import { ContainerActions } from "./ContainerActions";
import { DeleteContainerModal } from "./DeleteContainerModal";
import { RestartModal } from "./RestartModal";

interface Props {
  host: Host | null;
  environments: Environment[];
  thresholds: Record<string, number>;
  onClose: () => void;
  onOpenTerminal: (alias: string) => void;
}

function Metric({ label, value, pct, color, hint }: { label: string; value: string; pct: number | null; color: string; hint?: string }) {
  return (
    <div className="metric">
      <Text size="sm" c="dimmed">
        {label}
      </Text>
      {pct === null ? <div /> : <Progress value={Math.min(pct, 100)} color={color} size="md" radius="xl" />}
      <Tooltip label={hint} disabled={!hint}>
        <Text size="sm" className="mono" ta="right">
          {value}
        </Text>
      </Tooltip>
    </div>
  );
}

export function HostDrawer({ host, environments, thresholds, onClose, onOpenTerminal }: Props) {
  const navigate = useNavigate();
  const [restartTarget, setRestartTarget] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Container | null>(null);
  const invalidate = useInvalidateSettings();
  const setPaused = usePauseHost();
  const toggleMain = () =>
    host
      ? api.settings
          .updateHost(host.alias, { main: !host.main })
          .then(() => invalidate())
          .catch((error: Error) => notifications.show({ title: "Could not save", message: error.message, color: "red" }))
      : Promise.resolve();
  // "family" is for the containers a scheduler names: the saved rule is a glob, so the toast has
  // to say what was actually saved — it is wider than the one name that was on screen.
  const ignore = (alias: string, name: string, scope: "name" | "family" = "name") =>
    api
      .ignoreContainer(alias, name, scope)
      .then(({ pattern }) =>
        notifications.show({
          title: `${pattern} ignored`,
          message:
            scope === "family"
              ? "No container matching it raises an alert on this host any more."
              : "It is no longer expected on this host.",
          color: "gray",
        }),
      )
      .catch((error: Error) => notifications.show({ title: "Could not save", message: error.message, color: "red" }));
  const snapshot = host?.snapshot ?? null;
  const production = environments.find((e) => e.name === host?.env)?.production ?? false;

  const colorFor = (value: number, warn: number, crit: number) => (value >= crit ? "red" : value >= warn ? "yellow" : "teal");
  const loadPct = snapshot ? Math.min(100, Math.round((snapshot.load[0] / Math.max(snapshot.cpus, 1)) * 100)) : null;

  return (
    <>
      <Drawer
        opened={host !== null}
        onClose={onClose}
        position="right"
        // Wide enough for the container table's own columns — ports and status were being
        // truncated at 720 on a host with a dozen workloads.
        size="min(1040px, 92vw)"
        padding="lg"
        title={
          host && (
            <Group gap="sm" wrap="nowrap">
              <Text fw={700} size="lg" className="mono">
                {host.label}
              </Text>
              <EnvBadge env={host.env} environments={environments} />
              <RoleBadge role={host.role} />
              {host.runtime === "podman" && (
                <Badge variant="outline" color="orange" size="xs">
                  podman
                </Badge>
              )}
            </Group>
          )
        }
      >
        {host && (
          <Stack gap="md">
            <Group justify="space-between" align="flex-start">
              <Stack gap={2}>
                <Text size="sm" c="dimmed" className="mono">
                  {host.user ? `${host.user}@` : ""}
                  {host.hostname ?? ""}
                  {host.via ? ` · via ${host.via}` : host.is_bastion ? " · bastion" : ""}
                </Text>
                <ReachStatus host={host} />
              </Stack>
              {host.paused_by ? (
                // Nothing here may open a connection, so the buttons that would are not offered.
                host.paused_by === host.alias && (
                  <Button
                    size="xs"
                    color="teal"
                    leftSection={<IconPlayerPlay size={15} />}
                    loading={setPaused.isPending}
                    onClick={() => setPaused.mutate({ host, paused: false })}
                  >
                    Resume monitoring
                  </Button>
                )
              ) : (
              <Group gap="xs">
                <Button size="xs" variant="default" leftSection={<IconTerminal2 size={15} />} onClick={() => onOpenTerminal(host.alias)}>
                  Terminal
                </Button>
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconFileText size={15} />}
                  onClick={() => navigate(logsPath({ env: host.env, host: host.alias }), FRESH_LOGS)}
                >
                  Logs
                </Button>
                <Button size="xs" variant="default" leftSection={<IconFolders size={15} />} onClick={() => navigate(`/files?host=${encodeURIComponent(host.alias)}`)}>
                  Files
                </Button>
                <Tooltip label="Read this host's status again. Nothing on the server is changed." multiline maw={300}>
                  <Button
                    size="xs"
                    variant="default"
                    leftSection={<IconActivity size={15} />}
                    onClick={() => void api.refresh({ alias: host.alias })}
                  >
                    Check now
                  </Button>
                </Tooltip>
                <Tooltip
                  label="Stop monitoring this host: its connection is closed and sshfleet makes no new one — no checks, shells, logs or files — until you resume it."
                  multiline
                  maw={320}
                >
                  <Button
                    size="xs"
                    variant="default"
                    leftSection={<IconPlayerPause size={15} />}
                    loading={setPaused.isPending}
                    onClick={() => setPaused.mutate({ host, paused: true })}
                  >
                    Pause
                  </Button>
                </Tooltip>
              </Group>
              )}
            </Group>

            {host.paused_by && (
              <Alert color="orange" variant="light" icon={<IconPlayerPause size={16} />} title="Monitoring paused">
                {host.paused_by === host.alias
                  ? "sshfleet makes no connection to this host — no checks, shells, logs or file transfers — until you resume it."
                  : `This host is reached through ${host.paused_by}, whose monitoring is paused, so sshfleet does not connect to it either. Resume ${host.paused_by} to bring both back.`}
              </Alert>
            )}

            {snapshot && !snapshot.reachable && (
              <Text c="red.4" size="sm">
                {snapshot.error?.kind}: {snapshot.error?.message}
              </Text>
            )}

            <Tabs defaultValue="overview" keepMounted={false}>
              <Tabs.List>
                <Tabs.Tab value="overview">Overview</Tabs.Tab>
                <Tabs.Tab value="containers" rightSection={snapshot ? <Badge size="xs" variant="light" circle>{snapshot.containers.length}</Badge> : null}>
                  Containers
                </Tabs.Tab>
                <Tabs.Tab
                  value="alerts"
                  rightSection={snapshot?.alerts.length ? <Badge size="xs" color="red" circle>{snapshot.alerts.length}</Badge> : null}
                >
                  Alerts
                </Tabs.Tab>
              </Tabs.List>

              <Tabs.Panel value="overview" pt="md">
                {!snapshot || !snapshot.reachable ? (
                  <EmptyState title="No data yet" hint={host.paused_by ? "Monitoring is paused." : "This host has not answered a refresh."} />
                ) : (
                  <Stack gap="lg">
                    <Stack gap="sm">
                      <Metric
                        label="Load"
                        value={`${snapshot.load[0].toFixed(2)} / ${snapshot.cpus} cpu`}
                        pct={loadPct}
                        color={colorFor(snapshot.load[0] / Math.max(snapshot.cpus, 1), thresholds.load_per_cpu_warn ?? 1.5, 3)}
                        hint={`1m ${snapshot.load[0].toFixed(2)} · 5m ${snapshot.load[1].toFixed(2)} · 15m ${snapshot.load[2].toFixed(2)}`}
                      />
                      <Metric
                        label="Memory"
                        value={`${snapshot.mem.used_pct}%`}
                        pct={snapshot.mem.used_pct}
                        color={colorFor(snapshot.mem.used_pct, thresholds.mem_warn_pct ?? 85, thresholds.mem_crit_pct ?? 95)}
                        hint={`${fmtBytes(snapshot.mem.total - snapshot.mem.available)} of ${fmtBytes(snapshot.mem.total)}`}
                      />
                      {snapshot.disks.map((disk) => (
                        <Metric
                          key={disk.mount}
                          label={disk.mount}
                          value={`${disk.pct}%`}
                          pct={disk.pct}
                          color={colorFor(disk.pct, thresholds.disk_warn_pct ?? 80, thresholds.disk_crit_pct ?? 90)}
                          hint={`${fmtBytes(disk.used)} used · ${fmtBytes(disk.size - disk.used)} free`}
                        />
                      ))}
                    </Stack>

                    <SimpleGrid cols={2} spacing="md">
                      <Fact label="Uptime" value={fmtUptime(snapshot.uptime_s)} />
                      <Fact label="Runtime" value={snapshot.runtime} />
                      <Fact label="Kernel" value={snapshot.kernel || "—"} />
                      <Fact label="sudo" value={snapshot.sudo_ok ? "ok" : "not available"} bad={!snapshot.sudo_ok} />
                      <Fact label="Listening" value={snapshot.listening.join(", ") || "—"} />
                      <Fact
                        label="Health probes"
                        value={
                          snapshot.probes.length
                            ? snapshot.probes
                                .map((p) => {
                                  const age = p.at ? snapshot.at - p.at : 0;
                                  return `${p.port}${p.path} → ${p.code}${age > 90 ? ` (checked ${fmtAge(age)} ago)` : ""}`;
                                })
                                .join("  ")
                            : "—"
                        }
                        bad={snapshot.probes.some((p) => !p.ok)}
                      />
                      {snapshot.frontends.length > 0 && <Fact label="Web roots" value={snapshot.frontends.map((f) => f.name).join(", ")} />}
                      {snapshot.nginx && snapshot.nginx !== "absent" && <Fact label="nginx" value={snapshot.nginx} />}
                    </SimpleGrid>

                    {/* What sshfleet watches on this host, and where to change it. */}
                    <Card withBorder padding="sm" bg="dark.8">
                      <Group justify="space-between" wrap="nowrap" align="flex-start">
                        <div style={{ minWidth: 0 }}>
                          <Text size="xs" c="dimmed" tt="uppercase" fw={600} style={{ letterSpacing: 0.4 }}>
                            Watching
                          </Text>
                          <Group gap="lg" mt={6} wrap="wrap">
                            <Fact label="Role" value={host.role} />
                            <Fact label="Workloads" value={host.workloads ?? "all of them"} />
                            <Fact
                              label="Health endpoint"
                              value={host.health_port ? `${host.health_port}${host.health_path ?? "/"}` : "none set"}
                            />
                            <Fact label="Expected" value={host.expected_count === null ? "as seen" : String(host.expected_count)} />
                          </Group>
                        </div>
                        <Group gap={6} wrap="nowrap">
                          <Tooltip
                            label={
                              host.main
                                ? "In the default log search — click to leave it out"
                                : "Include this host's workload logs in the default search"
                            }
                            multiline
                            maw={250}
                          >
                            <ActionIcon
                              variant="subtle"
                              color={host.main ? "yellow" : "gray"}
                              aria-label="default log search"
                              onClick={() => void toggleMain()}
                            >
                              {host.main ? <IconStarFilled size={16} /> : <IconStar size={16} />}
                            </ActionIcon>
                          </Tooltip>
                          <Tooltip label="Change these on the Settings page">
                            <ActionIcon
                              variant="default"
                              aria-label="edit host"
                              onClick={() => navigate(`/settings?tab=hosts&host=${encodeURIComponent(host.alias)}`)}
                            >
                              <IconPencil size={15} />
                            </ActionIcon>
                          </Tooltip>
                        </Group>
                      </Group>
                    </Card>
                  </Stack>
                )}
              </Tabs.Panel>

              <Tabs.Panel value="containers" pt="md">
                {!snapshot || (snapshot.containers.length === 0 && snapshot.missing_containers.length === 0 && snapshot.vanished_containers.length === 0) ? (
                  <EmptyState title={snapshot?.runtime === "none" ? "No container runtime on this host" : "No containers"} />
                ) : (
                  <Table highlightOnHover verticalSpacing="sm">
                    <Table.Thead>
                      <Table.Tr>
                        <Table.Th>Container</Table.Th>
                        <Table.Th>State</Table.Th>
                        <Table.Th>Ports</Table.Th>
                        <Table.Th ta="right">Restarts</Table.Th>
                        <Table.Th ta="right">Log</Table.Th>
                        <Table.Th />
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {snapshot.containers.map((c) => (
                        // A stopped container is drawn faded so the running ones read first; its
                        // actions differ too: it can be started again, or removed for good.
                        <Table.Tr key={c.name} style={{ opacity: c.state === "running" ? 1 : 0.62 }}>
                          <Table.Td>
                            <Group gap={6} wrap="nowrap">
                              <Text size="sm" className="mono" fw={600}>
                                {c.name}
                              </Text>
                              {/* Says why this one is quiet: without it, a container exiting with
                                  no alert beside it looks like the tool missed it. */}
                              {c.ignored && (
                                <Tooltip label="Matches an ignore rule for this host: listed, never alerted on." multiline maw={280}>
                                  <Badge size="xs" variant="outline" color="gray" leftSection={<IconEyeOff size={10} />}>
                                    ignored
                                  </Badge>
                                </Tooltip>
                              )}
                            </Group>
                            <Text size="xs" c="dimmed" className="mono">
                              {shortImage(c.image)}
                            </Text>
                          </Table.Td>
                          <Table.Td>
                            <Badge
                              color={c.state === "running" ? "teal" : c.state === "restarting" || (c.exit_code ?? 0) !== 0 ? "red" : "gray"}
                              variant={c.state === "running" ? "light" : "outline"}
                              size="sm"
                            >
                              {c.state}
                              {c.state !== "running" && c.exit_code !== null && c.exit_code !== 0 ? ` · exit ${c.exit_code}` : ""}
                            </Badge>
                            <Text size="xs" c="dimmed">
                              {c.status}
                            </Text>
                          </Table.Td>
                          <Table.Td>
                            {/* Port maps can run to several lines; clipped here so the actions
                                stay on screen, the full list on hover. */}
                            <Tooltip label={c.ports || "no published ports"} multiline maw={360} disabled={!c.ports} openDelay={300}>
                              <Text size="xs" className="mono" c="dimmed" style={{ maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                {c.network_mode === "host" ? "host network" : c.ports || "—"}
                              </Text>
                            </Tooltip>
                          </Table.Td>
                          <Table.Td ta="right">
                            <Text size="sm" className="mono" c={c.restart_count ? "yellow.4" : "dimmed"}>
                              {c.restart_count || "—"}
                            </Text>
                          </Table.Td>
                          <Table.Td ta="right">
                            <Text size="sm" className="mono" c="dimmed">
                              {fmtBytes(c.log_bytes)}
                            </Text>
                          </Table.Td>
                          <Table.Td>
                            <Group gap={4} justify="flex-end" wrap="nowrap">
                              {/* What you came for nine times out of ten: a prompt inside this
                                  container. The menu is built on the host — see ContainerActions. */}
                              <ContainerActions alias={host.alias} container={c.name} state={c.state} />
                              <Tooltip label="Search this container's logs">
                                <ActionIcon
                                  variant="subtle"
                                  onClick={() => navigate(logsPath({ env: host.env, host: host.alias, container: c.name }), FRESH_LOGS)}
                                >
                                  <IconFileText size={16} />
                                </ActionIcon>
                              </Tooltip>
                              {/* The one control in the whole app that changes anything on a
                                  server. It says so in words: an icon alone is too easy to
                                  mistake for the read-only check above. */}
                              <Tooltip
                                label={
                                  production
                                    ? "Restart this container in place. Production: you will be asked to type its name first."
                                    : "Restart this container in place. You will see the exact command first."
                                }
                                multiline
                                maw={320}
                              >
                                <Button
                                  size="compact-xs"
                                  variant="light"
                                  color={production ? "red" : "teal"}
                                  leftSection={<IconRotateClockwise size={14} />}
                                  onClick={() => setRestartTarget(c.name)}
                                >
                                  {c.state === "running" ? "Restart" : "Start"}
                                </Button>
                              </Tooltip>
                              {c.state !== "running" && c.state !== "restarting" && c.state !== "paused" && (
                                <Tooltip label="Remove this stopped container. Its log is saved first; you type its name to confirm." multiline maw={300}>
                                  <Button
                                    size="compact-xs"
                                    variant="outline"
                                    color="red"
                                    leftSection={<IconTrash size={14} />}
                                    onClick={() => setDeleteTarget(c)}
                                  >
                                    Remove
                                  </Button>
                                </Tooltip>
                              )}
                            </Group>
                          </Table.Td>
                        </Table.Tr>
                      ))}
                      {snapshot.missing_containers.map((name) => (
                        <Table.Tr key={name}>
                          <Table.Td>
                            <Text size="sm" className="mono" fw={600} c="red.4">
                              {name}
                            </Text>
                            <Text size="xs" c="dimmed">
                              expected here; nothing restarts it automatically
                            </Text>
                          </Table.Td>
                          <Table.Td>
                            <Badge color="red" variant="filled" size="sm">
                              absent
                            </Badge>
                          </Table.Td>
                          <Table.Td colSpan={4} />
                        </Table.Tr>
                      ))}
                      {snapshot.vanished_containers.map((name) => (
                        <Table.Tr key={`gone:${name}`}>
                          <Table.Td>
                            <Text size="sm" className="mono" fw={600} c="red.4">
                              {name}
                            </Text>
                            <Text size="xs" c="dimmed">
                              was on this host and is gone — not stopped, removed
                            </Text>
                          </Table.Td>
                          <Table.Td>
                            <Badge color="red" variant="filled" size="sm">
                              gone
                            </Badge>
                          </Table.Td>
                          <Table.Td colSpan={3} />
                          <Table.Td>
                            <Group justify="flex-end">
                              <Menu position="bottom-end" withinPortal>
                                <Menu.Target>
                                  <Button
                                    size="compact-xs"
                                    variant="default"
                                    leftSection={<IconEyeOff size={14} />}
                                    rightSection={<IconChevronDown size={12} />}
                                  >
                                    Ignore
                                  </Button>
                                </Menu.Target>
                                <Menu.Dropdown>
                                  <Menu.Item onClick={() => void ignore(host.alias, name)}>
                                    This container
                                    <Text size="xs" c="dimmed">
                                      It left on purpose: stop expecting it here.
                                    </Text>
                                  </Menu.Item>
                                  {/* The one that matters for Airbyte and friends: they invent a
                                      name per run, so ignoring this one silences nothing. */}
                                  <Menu.Item onClick={() => void ignore(host.alias, name, "family")}>
                                    Every container named like it
                                    <Text size="xs" c="dimmed">
                                      For a scheduler that names a container per job run.
                                    </Text>
                                  </Menu.Item>
                                </Menu.Dropdown>
                              </Menu>
                            </Group>
                          </Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                )}
              </Tabs.Panel>

              <Tabs.Panel value="alerts" pt="md">
                {!snapshot?.alerts.length ? (
                  <EmptyState title="No alerts on this host" />
                ) : (
                  <Stack gap="xs">
                    {snapshot.alerts.map((alert, index) => (
                      <Group key={index} gap="sm" wrap="nowrap" align="flex-start">
                        <SeverityBadge severity={alert.severity}>{alert.code}</SeverityBadge>
                        <Text size="sm">{alert.message}</Text>
                      </Group>
                    ))}
                  </Stack>
                )}
              </Tabs.Panel>
            </Tabs>
          </Stack>
        )}
      </Drawer>
      {host && <RestartModal alias={host.alias} container={restartTarget} onClose={() => setRestartTarget(null)} />}
      {host && <DeleteContainerModal alias={host.alias} env={host.env} container={deleteTarget} onClose={() => setDeleteTarget(null)} />}
    </>
  );
}

function Fact({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <Stack gap={0}>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600} style={{ letterSpacing: 0.4 }}>
        {label}
      </Text>
      <Text size="sm" className="mono" c={bad ? "red.4" : undefined} style={{ wordBreak: "break-word" }}>
        {value}
      </Text>
    </Stack>
  );
}
