import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Divider,
  Group,
  Loader,
  Menu,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation } from "@tanstack/react-query";
import {
  IconFileText,
  IconFolders,
  IconPlus,
  IconLock,
  IconLockOpen,
  IconPlayerPause,
  IconPlayerPlay,
  IconStar,
  IconStarFilled,
  IconTerminal2,
  IconWorld,
} from "@tabler/icons-react";
import { useEffect, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import type { Host, PausedBy } from "../api/types";
import { EmptyState, EnvBadge, PercentBar, ReachStatus, RoleBadge, SearchInput, SeverityBadge } from "../components/common";
import { HostDrawer } from "../components/HostDrawer";
import { api } from "../api/client";
import { useConfig, useFleet } from "../hooks/useFleet";
import { useInvalidateSettings, usePause } from "../hooks/useSettings";
import { fmtAge, fmtUptime } from "../lib/format";
import { FRESH_LOGS, logsPath } from "../lib/navigation";
import { pausedItself, pauseTargetLabel } from "../lib/pause";
import { useEnv } from "../state/env";
import { useTerminals } from "../state/terminals";
import { usePageLimit, useViewState } from "../state/views";

type SortKey = "alerts" | "name" | "load" | "mem" | "disk";

const SEVERITY_RANK = { crit: 0, warn: 1, info: 2 } as const;
//: How many rows the table draws before asking whether you want the rest.
const PAGE = 50;

function worstSeverity(host: Host): number {
  const alerts = host.snapshot?.alerts ?? [];
  if (!alerts.length) return 3;
  return Math.min(...alerts.map((a) => SEVERITY_RANK[a.severity]));
}

//: The jump host without the alias prefix it shares with the host: acme-prod-web via acme-prod-bastion → bastion.
function viaLabel(alias: string, via: string): string {
  let shared = 0;
  while (shared < alias.length && shared < via.length && alias[shared] === via[shared]) shared++;
  const cut = Math.max(via.lastIndexOf("-", shared - 1), via.lastIndexOf("_", shared - 1)) + 1;
  return shared > 0 && cut > 0 && cut < via.length ? via.slice(cut) : via;
}

function containersCell(host: Host) {
  const snapshot = host.snapshot;
  if (!snapshot || !snapshot.reachable) return <Text size="sm" c="dimmed">—</Text>;
  if (snapshot.runtime === "none") return <Text size="sm" c="dimmed">static</Text>;
  // Ignored containers are a job runner's churn, counted apart: they must not make a host that
  // is short a service look complete, nor turn a healthy host red when a batch of them exits.
  const tracked = snapshot.containers.filter((c) => !c.ignored);
  const churn = snapshot.containers.length - tracked.length;
  const running = tracked.filter((c) => c.state === "running").length;
  const stopped = tracked.length - running;
  const gone = snapshot.missing_containers.length + snapshot.vanished_containers.length;
  // Expected = a number the operator set, else everything that is or should be here.
  const expected = snapshot.expected_count ?? (gone ? tracked.length + gone : 0);
  const short = gone > 0 || (snapshot.expected_count !== null && tracked.length < snapshot.expected_count);
  return (
    <Tooltip
      label={[
        `${running} running`,
        stopped ? `${stopped} stopped` : null,
        churn ? `${churn} ignored (job containers)` : null,
        gone ? `${gone} gone: ${[...snapshot.missing_containers, ...snapshot.vanished_containers].join(", ")}` : null,
        snapshot.expected_count !== null ? `${snapshot.expected_count} expected (set by hand)` : null,
      ]
        .filter(Boolean)
        .join("\n")}
      multiline
      style={{ whiteSpace: "pre-line" }}
    >
      <Text size="sm" className="mono" c={short ? "red.4" : running ? "teal.4" : "dimmed"} fw={short ? 700 : 500} style={{ cursor: "default" }}>
        {expected ? `${running}/${expected}` : running}
        {stopped ? (
          <Text span size="xs" c="dimmed">
            {" "}
            ·{stopped}
          </Text>
        ) : null}
      </Text>
    </Tooltip>
  );
}

function healthCell(host: Host) {
  const probes = host.snapshot?.probes ?? [];
  if (!probes.length) return <Text size="sm" c="dimmed">—</Text>;
  const ok = probes.filter((p) => p.ok).length;
  const probe = probes[0];
  // A host whose endpoint is checked sparingly keeps showing its last answer. It is drawn as an
  // outline with its age, so an old reading is never mistaken for a fresh one.
  const age = probe.at ? (host.snapshot?.at ?? 0) - probe.at : 0;
  const carried = age > 90;
  const lines = probes.map((p) => `${p.port}${p.path} → ${p.code}`);
  if (carried) lines.push(`checked ${fmtAge(age)} ago`);
  return (
    // pre-line, or the newline between several probes collapses and the lines run together.
    <Tooltip label={lines.join("\n")} multiline style={{ whiteSpace: "pre-line" }}>
      <Badge color={ok === probes.length ? "teal" : "red"} variant={carried ? "outline" : "light"} size="sm" className="mono">
        {probes.length === 1 ? probe.code : `${ok}/${probes.length}`}
        {carried ? ` · ${fmtAge(age)}` : ""}
      </Badge>
    </Tooltip>
  );
}

function alertsCell(host: Host) {
  const alerts = host.snapshot?.alerts ?? [];
  if (!alerts.length) return null;
  const counts = new Map<string, { n: number; severity: "crit" | "warn" | "info" }>();
  for (const alert of alerts) {
    const entry = counts.get(alert.code);
    if (entry) {
      entry.n += 1;
      if (SEVERITY_RANK[alert.severity] < SEVERITY_RANK[entry.severity]) entry.severity = alert.severity;
    } else counts.set(alert.code, { n: 1, severity: alert.severity });
  }
  const sorted = [...counts.entries()].sort((a, b) => SEVERITY_RANK[a[1].severity] - SEVERITY_RANK[b[1].severity]);
  return (
    <Group gap={4} wrap="nowrap">
      {sorted.slice(0, 3).map(([code, { n, severity }]) => (
        <SeverityBadge key={code} severity={severity}>
          {code}
          {n > 1 ? ` ×${n}` : ""}
        </SeverityBadge>
      ))}
      {sorted.length > 3 && (
        <Text size="xs" c="dimmed">
          +{sorted.length - 3}
        </Text>
      )}
    </Group>
  );
}

export default function FleetPage() {
  const { env, project, inScope } = useEnv();
  const config = useConfig();
  const fleet = useFleet();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  // Kept between visits: the fleet filtered to one service is still filtered when you come back.
  const [query, setQuery] = useViewState("fleet.query", "");
  const [role, setRole] = useViewState("fleet.role", "all");
  const [alertsOnly, setAlertsOnly] = useViewState("fleet.alertsOnly", false);
  const [sort, setSort] = useViewState<SortKey>("fleet.sort", "alerts");
  // A fleet grows; the table does not have to grow with it in one go. Narrowing the list is a
  // new question, so it starts at the top again.
  const [limit, setLimit] = usePageLimit("fleet.limit", JSON.stringify([env, role, alertsOnly, query]), PAGE);
  const invalidate = useInvalidateSettings();
  // The star decides what the Logs page searches by default; it is saved on the host.
  const toggleMain = useMutation({
    mutationFn: (host: Host) => api.settings.updateHost(host.alias, { main: !host.main }),
    onSuccess: () => invalidate(),
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });
  const setPaused = usePause();
  const terminals = useTerminals();

  // A paused project or environment explains a page of paused hosts, so it is named above them
  // and can be resumed from here. A project's pause covers its environments' own.
  const pausedScopes = useMemo<PausedBy[]>(() => {
    if (!project) return [];
    if (config.data?.projects.find((p) => p.name === project)?.paused) return [{ kind: "project", name: project }];
    return (config.data?.environments ?? [])
      .filter((e) => e.project === project && e.paused && inScope(e.name))
      .map((e) => ({ kind: "environment", name: e.name }));
  }, [config.data, project, inScope]);

  const selectedAlias = params.get("host");
  const thresholds = config.data?.thresholds ?? {};

  const hosts = useMemo(() => {
    let list = (fleet.data?.hosts ?? []).filter((h) => inScope(h.env));
    if (role !== "all") list = list.filter((h) => h.role === role);
    if (alertsOnly) list = list.filter((h) => (h.snapshot?.alerts.length ?? 0) > 0);
    if (query.trim()) {
      const needle = query.trim().toLowerCase();
      list = list.filter(
        (h) =>
          h.alias.toLowerCase().includes(needle) ||
          h.label.toLowerCase().includes(needle) ||
          (h.hostname ?? "").includes(needle) ||
          (h.snapshot?.containers ?? []).some((c) => c.name.toLowerCase().includes(needle)),
      );
    }
    const byName = (a: Host, b: Host) => a.env.localeCompare(b.env) || a.alias.localeCompare(b.alias);
    const sorted = [...list];
    switch (sort) {
      case "alerts":
        sorted.sort((a, b) => worstSeverity(a) - worstSeverity(b) || byName(a, b));
        break;
      case "load":
        sorted.sort((a, b) => (b.snapshot?.load[0] ?? -1) - (a.snapshot?.load[0] ?? -1));
        break;
      case "mem":
        sorted.sort((a, b) => (b.snapshot?.mem.used_pct ?? -1) - (a.snapshot?.mem.used_pct ?? -1));
        break;
      case "disk":
        sorted.sort((a, b) => Math.max(...(b.snapshot?.disks.map((d) => d.pct) ?? [-1])) - Math.max(...(a.snapshot?.disks.map((d) => d.pct) ?? [-1])));
        break;
      default:
        sorted.sort(byName);
    }
    return sorted;
  }, [fleet.data, inScope, role, alertsOnly, query, sort]);

  const shown = hosts.slice(0, limit);
  const more = hosts.length - shown.length;

  // A paused host is counted apart: it is not down, and sshfleet holds no connection to it.
  const inView = useMemo(() => (fleet.data?.hosts ?? []).filter((h) => inScope(h.env)), [fleet.data, inScope]);
  const scope = useMemo(() => inView.filter((h) => !h.paused_by), [inView]);
  const pausedCount = inView.length - scope.length;
  const summary = useMemo(() => {
    const reachable = scope.filter((h) => h.snapshot?.reachable).length;
    const answered = scope.filter((h) => h.snapshot).length;
    // Same unit as the per-host cell: the workloads the fleet is held to, not a job runner's churn.
    const containers = scope.reduce((n, h) => n + (h.snapshot?.containers.filter((c) => c.state === "running" && !c.ignored).length ?? 0), 0);
    const missing = scope.reduce((n, h) => n + (h.snapshot?.missing_containers.length ?? 0), 0);
    const alerts = scope.flatMap((h) => h.snapshot?.alerts ?? []);
    return {
      reachable,
      answered,
      total: scope.length,
      connected: scope.filter((h) => h.connected).length,
      containers,
      missing,
      crit: alerts.filter((a) => a.severity === "crit").length,
      warn: alerts.filter((a) => a.severity === "warn").length,
    };
  }, [scope]);

  const selected = useMemo(() => fleet.data?.hosts.find((h) => h.alias === selectedAlias) ?? null, [fleet.data, selectedAlias]);

  useEffect(() => {
    if (selectedAlias && fleet.data && !selected) {
      params.delete("host");
      setParams(params, { replace: true });
    }
  }, [selectedAlias, selected, fleet.data, params, setParams]);

  const openHost = (alias: string) => {
    params.set("host", alias);
    setParams(params);
  };
  const closeHost = () => {
    params.delete("host");
    setParams(params);
  };

  return (
    <Stack gap="md" style={{ flex: 1 }}>
      {pausedScopes.length > 0 && (
        <Alert color="orange" variant="light" icon={<IconPlayerPause size={16} />} py="xs">
          <Group justify="space-between" gap="sm" wrap="wrap">
            <Text size="sm">
              {pausedScopes.length === 1
                ? `${pauseTargetLabel(pausedScopes[0])} is paused — sshfleet makes no connection to its hosts until it is resumed.`
                : `${pausedScopes.length} environments are paused (${pausedScopes.map((s) => s.name).join(", ")}) — sshfleet makes no connection to their hosts until they are resumed.`}
            </Text>
            <Group gap="xs">
              {pausedScopes.map((target) => (
                <Button
                  key={`${target.kind}:${target.name}`}
                  size="xs"
                  variant="light"
                  color="teal"
                  leftSection={<IconPlayerPlay size={14} />}
                  loading={
                    setPaused.isPending &&
                    setPaused.variables?.target.kind === target.kind &&
                    setPaused.variables?.target.name === target.name
                  }
                  onClick={() => setPaused.mutate({ target, paused: false })}
                >
                  Resume {target.name}
                </Button>
              ))}
            </Group>
          </Group>
        </Alert>
      )}
      <SimpleGrid cols={{ base: 2, md: 4 }} spacing="sm">
        <Card withBorder padding="sm">
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            Hosts reachable
          </Text>
          <Group gap={6} align="baseline">
            <Text size="xl" fw={700} className="mono" c={summary.reachable < summary.answered ? "yellow.4" : "teal.4"}>
              {summary.reachable}
            </Text>
            <Text size="sm" c="dimmed">
              / {summary.total}
              {pausedCount > 0 && ` · ${pausedCount} paused`}
            </Text>
            {fleet.isLoading && <Loader size={12} />}
          </Group>
        </Card>
        <Card withBorder padding="sm">
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            Containers running
          </Text>
          <Group gap={6} align="baseline">
            <Text size="xl" fw={700} className="mono">
              {summary.containers}
            </Text>
            {summary.missing > 0 && (
              <Text size="sm" c="red.4" fw={600}>
                {summary.missing} missing
              </Text>
            )}
          </Group>
        </Card>
        <Card withBorder padding="sm">
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            Alerts
          </Text>
          <Group gap="sm" align="baseline">
            <Text size="xl" fw={700} className="mono" c={summary.crit ? "red.4" : summary.warn ? "yellow.4" : "teal.4"}>
              {summary.crit + summary.warn}
            </Text>
            <Text size="sm" c="dimmed">
              {summary.crit} critical · {summary.warn} warning
            </Text>
          </Group>
        </Card>
        <Card withBorder padding="sm">
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            Connections
          </Text>
          <Group gap={6} align="baseline">
            {/* Counted over the hosts in view, like the cards beside it. */}
            <Text size="xl" fw={700} className="mono">
              {summary.connected}
            </Text>
            <Text size="sm" c="dimmed">
              / {summary.total} open
            </Text>
          </Group>
        </Card>
      </SimpleGrid>

      <Group justify="space-between" wrap="wrap" gap="sm">
        <Group gap="sm">
          <SearchInput placeholder="Filter by host, service or container…" value={query} onChange={setQuery} w={300} size="sm" />
          <SegmentedControl
            size="xs"
            value={role}
            onChange={setRole}
            data={[
              { label: "All", value: "all" },
              { label: "Services", value: "service" },
              { label: "Gateways", value: "gateway" },
              { label: "Frontends", value: "frontend" },
              { label: "Data", value: "data" },
              { label: "Ops", value: "ops" },
            ]}
          />
        </Group>
        <Group gap="md">
          <Switch size="sm" label="Alerts only" checked={alertsOnly} onChange={(e) => setAlertsOnly(e.currentTarget.checked)} />
          <Select
            size="sm"
            w={150}
            value={sort}
            onChange={(v) => setSort((v as SortKey) ?? "alerts")}
            data={[
              { value: "alerts", label: "Sort: alerts first" },
              { value: "name", label: "Sort: name" },
              { value: "load", label: "Sort: load" },
              { value: "mem", label: "Sort: memory" },
              { value: "disk", label: "Sort: disk" },
            ]}
            allowDeselect={false}
          />
        </Group>
      </Group>

      <Card withBorder padding={0} style={{ overflow: "hidden" }}>
        {fleet.isLoading ? (
          <Group justify="center" py={60}>
            <Loader />
          </Group>
        ) : hosts.length === 0 ? (
          <EmptyState title="No hosts match" hint="Adjust the filter, or register an environment with `sshfleet env add`." />
        ) : (
          <Table.ScrollContainer minWidth={1100}>
            <Table highlightOnHover stickyHeader verticalSpacing={8}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th w={34} />
                  <Table.Th w={250}>Host</Table.Th>
                  {!env && <Table.Th w={84}>Env</Table.Th>}
                  <Table.Th w={110}>Status</Table.Th>
                  <Table.Th miw={140}>Alerts</Table.Th>
                  <Table.Th ta="right" w={64}>Load</Table.Th>
                  <Table.Th w={110}>Memory</Table.Th>
                  <Table.Th w={110}>Disk</Table.Th>
                  <Table.Th ta="center" w={96}>Containers</Table.Th>
                  <Table.Th ta="center" w={80}>Health</Table.Th>
                  <Table.Th ta="right" w={84}>Uptime</Table.Th>
                  <Table.Th ta="right" w={56}>Age</Table.Th>
                  <Table.Th w={110} />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {shown.map((host) => {
                  const s = host.snapshot;
                  const up = s?.reachable ?? false;
                  const disk = s?.disks.length ? Math.max(...s.disks.map((d) => d.pct)) : null;
                  return (
                    <Table.Tr key={host.alias} className="host-row" data-selected={host.alias === selectedAlias} onClick={() => openHost(host.alias)}>
                      {/* The star is the only cell that does not open the drawer. */}
                      <Table.Td onClick={(event) => event.stopPropagation()}>
                        <Tooltip
                          label={
                            host.main
                              ? "In the default log search — click to leave it out"
                              : "Include this host's workload logs in the default search"
                          }
                          multiline
                          maw={260}
                          openDelay={300}
                        >
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            color={host.main ? "yellow" : "gray"}
                            aria-label={`default log search for ${host.alias}`}
                            onClick={() => toggleMain.mutate(host)}
                          >
                            {host.main ? <IconStarFilled size={14} /> : <IconStar size={14} />}
                          </ActionIcon>
                        </Tooltip>
                      </Table.Td>
                      <Table.Td>
                        <Group gap={8} wrap="nowrap">
                          <div>
                            <Text size="sm" fw={600} className="mono" style={{ whiteSpace: "nowrap" }}>
                              {host.label}
                            </Text>
                            <Group gap={6}>
                              <Text size="xs" c="dimmed">
                                {host.role}
                              </Text>
                              {host.via && (
                                <Text size="xs" c="dimmed">
                                  · via {viaLabel(host.alias, host.via)}
                                </Text>
                              )}
                            </Group>
                          </div>
                        </Group>
                      </Table.Td>
                      {!env && (
                        <Table.Td className="nowrap">
                          <EnvBadge env={host.env} environments={config.data?.environments} />
                        </Table.Td>
                      )}
                      <Table.Td>
                        <ReachStatus host={host} />
                      </Table.Td>
                      <Table.Td className="nowrap">{alertsCell(host)}</Table.Td>
                      <Table.Td ta="right">
                        <Text size="sm" className="mono" c={up && s && s.load[0] / Math.max(s.cpus, 1) >= (thresholds.load_per_cpu_warn ?? 1.5) ? "yellow.4" : undefined}>
                          {up && s ? s.load[0].toFixed(2) : "—"}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <PercentBar value={up && s ? s.mem.used_pct : null} warn={thresholds.mem_warn_pct ?? 85} crit={thresholds.mem_crit_pct ?? 95} width={60} />
                      </Table.Td>
                      <Table.Td>
                        <PercentBar value={up ? disk : null} warn={thresholds.disk_warn_pct ?? 80} crit={thresholds.disk_crit_pct ?? 90} width={60} />
                      </Table.Td>
                      <Table.Td ta="center">{containersCell(host)}</Table.Td>
                      <Table.Td ta="center">{healthCell(host)}</Table.Td>
                      <Table.Td ta="right" className="nowrap">
                        <Text size="sm" className="mono" c="dimmed">
                          {up ? fmtUptime(s?.uptime_s) : "—"}
                        </Text>
                      </Table.Td>
                      <Table.Td ta="right">
                        <Text size="xs" className="mono" c={host.stale ? "yellow.4" : "dimmed"}>
                          {fmtAge(host.age_s)}
                        </Text>
                      </Table.Td>
                      <Table.Td onClick={(e) => e.stopPropagation()}>
                        <Group gap={2} wrap="nowrap" justify="flex-end">
                          {(host.snapshot?.sites?.length ?? 0) > 0 && (
                            <Menu position="bottom-end" width={260} withinPortal>
                              <Menu.Target>
                                <Tooltip label={`${host.snapshot!.sites.length} site${host.snapshot!.sites.length > 1 ? "s" : ""} served here`}>
                                  <ActionIcon variant="subtle" color="gray">
                                    <IconWorld size={16} />
                                  </ActionIcon>
                                </Tooltip>
                              </Menu.Target>
                              <Menu.Dropdown>
                                <Menu.Label>Served here · opens in a new tab</Menu.Label>
                                {host.snapshot!.sites.map((site) => (
                                  <Menu.Item
                                    key={`${site.name}:${site.port}`}
                                    component="a"
                                    href={site.url}
                                    target="_blank"
                                    rel="noreferrer noopener"
                                    leftSection={site.ssl ? <IconLock size={13} /> : <IconLockOpen size={13} />}
                                  >
                                    <Text size="sm" className="mono" truncate>
                                      {site.name}
                                    </Text>
                                    <Text size="xs" c="dimmed">
                                      {site.source}
                                      {site.port !== (site.ssl ? 443 : 80) ? ` · port ${site.port}` : ""}
                                    </Text>
                                  </Menu.Item>
                                ))}
                              </Menu.Dropdown>
                            </Menu>
                          )}
                          {host.paused_by ? (
                            // Shell, logs and files would all need a connection it is not given.
                            pausedItself(host) && (
                              <Tooltip label="Resume monitoring">
                                <ActionIcon
                                  variant="subtle"
                                  color="teal"
                                  aria-label={`resume ${host.alias}`}
                                  loading={setPaused.isPending && setPaused.variables?.target.name === host.alias}
                                  onClick={() => setPaused.mutate({ target: { kind: "host", name: host.alias }, paused: false })}
                                >
                                  <IconPlayerPlay size={16} />
                                </ActionIcon>
                              </Tooltip>
                            )
                          ) : (
                            <>
                              <ShellButton alias={host.alias} />
                              <Tooltip label="Logs">
                                <ActionIcon variant="subtle" color="gray" onClick={() => navigate(logsPath({ env: host.env, host: host.alias }), FRESH_LOGS)}>
                                  <IconFileText size={16} />
                                </ActionIcon>
                              </Tooltip>
                              <Tooltip label="Files">
                                <ActionIcon variant="subtle" color="gray" onClick={() => navigate(`/files?host=${encodeURIComponent(host.alias)}`)}>
                                  <IconFolders size={16} />
                                </ActionIcon>
                              </Tooltip>
                            </>
                          )}
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
        {more > 0 && (
          <>
            <Divider />
            <Group justify="space-between" px="md" py="xs">
              <Text size="xs" c="dimmed">
                Showing {shown.length} of {hosts.length}
              </Text>
              <Group gap="xs">
                <Button size="compact-sm" variant="subtle" onClick={() => setLimit((current) => current + PAGE)}>
                  Show {Math.min(more, PAGE)} more
                </Button>
                <Button size="compact-sm" variant="subtle" c="dimmed" onClick={() => setLimit(hosts.length)}>
                  Show all {hosts.length}
                </Button>
              </Group>
            </Group>
          </>
        )}
      </Card>

      <Group gap="md" justify="space-between">
        <Text size="xs" c="dimmed">
          {shown.length} of {inView.length} hosts · roles: <RoleBadge role="service" /> <RoleBadge role="gateway" /> <RoleBadge role="frontend" /> <RoleBadge role="data" /> <RoleBadge role="ops" />
        </Text>
        <Text size="xs" c="dimmed">
          A host is refreshed every {config.data?.refresh.fast_s ?? 30}s. Rows update as each one answers.
        </Text>
      </Group>

      <HostDrawer host={selected} environments={config.data?.environments ?? []} thresholds={thresholds} onClose={closeHost} onOpenTerminal={terminals.openHost} />

    </Stack>
  );
}

/** The row's terminal button. With no shell open it opens one; with shells open it asks which —
 *  one of those, or another beside them — since both are what a second click can mean. */
function ShellButton({ alias }: { alias: string }) {
  const terminals = useTerminals();
  const open = terminals.hostSessions(alias);
  if (open.length === 0) {
    return (
      <Tooltip label="Terminal">
        <ActionIcon variant="subtle" color="gray" onClick={() => terminals.newHost(alias)}>
          <IconTerminal2 size={16} />
        </ActionIcon>
      </Tooltip>
    );
  }
  return (
    <Menu position="bottom-end" width={220} withinPortal>
      <Menu.Target>
        <Tooltip label={open.length === 1 ? "A shell is open here" : `${open.length} shells are open here`}>
          <ActionIcon variant="light" color="teal">
            <IconTerminal2 size={16} />
          </ActionIcon>
        </Tooltip>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Open here</Menu.Label>
        {open.map((session) => (
          <Menu.Item
            key={session.id}
            onClick={() => terminals.show(session.id)}
            leftSection={<IconTerminal2 size={14} color={terminals.ended(session.id) ? undefined : "var(--mantine-color-teal-4)"} />}
            rightSection={terminals.ended(session.id) ? <Text size="xs" c="dimmed">ended</Text> : undefined}
          >
            <Text size="sm" className="mono">
              {session.title}
            </Text>
          </Menu.Item>
        ))}
        <Menu.Divider />
        <Menu.Item leftSection={<IconPlus size={14} />} onClick={() => terminals.newHost(alias)}>
          New shell
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}
