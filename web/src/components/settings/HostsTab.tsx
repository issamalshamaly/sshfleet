import {
  ActionIcon,
  Alert,
  Autocomplete,
  Badge,
  Button,
  Card,
  Code,
  Divider,
  Grid,
  Group,
  Modal,
  NumberInput,
  ScrollArea,
  Select,
  Stack,
  Switch,
  TagsInput,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  IconArrowRight,
  IconPencil,
  IconPlayerPause,
  IconPlugConnected,
  IconPlus,
  IconStarFilled,
  IconTrash,
} from "@tabler/icons-react";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../../api/client";
import type { HostSettings, Settings } from "../../api/types";
import { useConfig } from "../../hooks/useFleet";
import { useIsPhone } from "../../lib/responsive";
import { CardActions, DataCard, Field, InfoTip, ResponsiveModal, StickyActions } from "../responsive";
import { useInvalidateSettings, renderStanza, useSettingsHosts } from "../../hooks/useSettings";
import { pauseReason } from "../../lib/pause";
import { useEnv } from "../../state/env";
import { usePageLimit, useViewState } from "../../state/views";
import { EmptyState, EnvBadge, SearchInput } from "../common";

interface FormState {
  env: string;
  alias: string;
  hostname: string;
  user: string;
  port: string | number;
  proxyJump: string;
  identityFile: string;
  display: string;
  label: string;
  role: string;
  main: boolean;
  workloads: string;
  tags: string[];
  healthPort: string | number;
  healthPath: string;
  probeInterval: string; // "" inherits the service catalogue
  expectedCount: string | number; // "" = learn from what is seen
  connectTimeout: string | number; // "" = the fleet-wide budget
  ignored: string[];
  paused: boolean;
}

const EMPTY: FormState = {
  env: "",
  alias: "",
  hostname: "",
  user: "",
  port: "",
  proxyJump: "",
  identityFile: "",
  display: "",
  label: "",
  role: "service",
  main: false,
  workloads: "",
  tags: [],
  healthPort: "",
  healthPath: "",
  probeInterval: "",
  expectedCount: "",
  connectTimeout: "",
  ignored: [],
  paused: false,
};

// How often a host's health endpoint may be called. Some health endpoints write a block of log
// on every call, so a service can be throttled in the profile; this is how one host escapes
// that, either way.
const PROBE_CHOICES = [0, 300, 900, 3600];

function everyLabel(seconds: number): string {
  if (seconds <= 0) return "Every refresh";
  if (seconds < 3600) {
    const minutes = Math.round(seconds / 60);
    return `At most every ${minutes === 1 ? "minute" : `${minutes} minutes`}`;
  }
  const hours = seconds / 3600;
  return `At most every ${hours === 1 ? "hour" : `${hours} hours`}`;
}

function HostModal({
  opened,
  onClose,
  editing,
  settings,
  hosts,
  envNames,
}: {
  opened: boolean;
  onClose: () => void;
  editing: HostSettings | null;
  settings: Settings | undefined;
  hosts: HostSettings[];
  /** Only the environments of the project in view: a host must not land in another one's file. */
  envNames: string[];
}) {
  const invalidate = useInvalidateSettings();
  const [form, setForm] = useState<FormState>(EMPTY);
  const keys = useQuery({ queryKey: ["settings", "keys"], queryFn: api.settings.keys, enabled: opened });

  useEffect(() => {
    if (!opened) return;
    const editPrefix = settings?.environments.find((e) => e.name === editing?.env)?.host_prefix ?? "";
    setForm(
      editing
        ? {
            env: editing.env,
            // The form holds only what follows the prefix; the prefix itself is not editable.
            alias: editPrefix && editing.alias.startsWith(editPrefix) ? editing.alias.slice(editPrefix.length) : editing.alias,
            hostname: editing.hostname ?? "",
            user: editing.user ?? "",
            port: editing.port ?? "",
            proxyJump: editing.proxy_jump ?? "",
            identityFile: "",
            display: editing.display ?? "",
            label: editing.label ?? "",
            role: editing.role ?? "service",
            main: editing.main,
            workloads: editing.workloads ?? "",
            tags: editing.tags ?? [],
            healthPort: editing.health_port ?? "",
            healthPath: editing.health_path ?? "",
            probeInterval: editing.probe_min_interval_s ? String(editing.probe_min_interval_s) : "",
            expectedCount: editing.expected_count ?? "",
            connectTimeout: editing.connect_timeout_s ?? "",
            ignored: editing.ignored_containers ?? [],
            paused: editing.paused,
          }
        : { ...EMPTY, env: envNames[0] ?? "" },
    );
  }, [opened, editing, settings, envNames]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((current) => ({ ...current, [key]: value }));

  const environment = settings?.environments.find((e) => e.name === form.env);
  // Aliases share one namespace across projects, so a new host is always <project>_<short>_<name>.
  // A host added before that rule is edited as it is, never renamed behind the operator's back.
  const prefix = environment?.host_prefix ?? "";
  const prefixed = !!prefix && (!editing || editing.alias.startsWith(prefix));
  const fullAlias = prefixed ? prefix + form.alias.trim() : form.alias.trim();
  const parents = useMemo(() => {
    const inEnv = hosts.filter((h) => h.env === form.env && h.alias !== editing?.alias);
    const bastionFirst = [...inEnv].sort((a, b) => Number(b.is_bastion) - Number(a.is_bastion) || a.alias.localeCompare(b.alias));
    return [
      { value: "", label: "Direct connection — no jump host" },
      ...bastionFirst.map((h) => ({ value: h.alias, label: h.is_bastion ? `${h.alias}  (bastion)` : h.alias })),
    ];
  }, [hosts, form.env, editing]);

  const save = useMutation({
    mutationFn: async () => {
      const port = form.port === "" ? null : Number(form.port);
      if (!editing) {
        return api.settings.addHost({
          env: form.env,
          alias: fullAlias,
          hostname: form.hostname.trim() || null,
          user: form.user.trim() || null,
          port,
          proxy_jump: form.proxyJump || null,
          identity_file: form.identityFile.trim() || null,
          display: form.display.trim() || null,
          label: form.label.trim() || null,
          role: form.role,
          main: form.main,
          workloads: form.workloads.trim() || null,
          tags: form.tags,
          health_port: form.healthPort === "" ? null : Number(form.healthPort),
          health_path: form.healthPath.trim() || null,
          probe_min_interval_s: form.probeInterval === "" ? null : Number(form.probeInterval),
          expected_count: form.expectedCount === "" ? null : Number(form.expectedCount),
          connect_timeout_s: form.connectTimeout === "" ? null : Number(form.connectTimeout),
          ignored_containers: form.ignored.length ? form.ignored : null,
        });
      }
      // Only what actually changed is sent, so untouched lines are never rewritten.
      const patch: Record<string, unknown> = {};
      if (fullAlias !== editing.alias) patch.alias = fullAlias;
      if (form.hostname.trim() !== (editing.hostname ?? "")) patch.hostname = form.hostname.trim();
      if (form.user.trim() !== (editing.user ?? "")) patch.user = form.user.trim();
      if (String(form.port ?? "") !== String(editing.port ?? "")) patch.port = form.port === "" ? null : Number(form.port);
      if (form.proxyJump !== (editing.proxy_jump ?? "")) patch.proxy_jump = form.proxyJump;
      if (form.identityFile.trim()) patch.identity_file = form.identityFile.trim();
      if (form.display.trim() !== (editing.display ?? "")) patch.display = form.display.trim();
      if (form.label.trim() !== (editing.label ?? "")) patch.label = form.label.trim();
      if (form.role !== editing.role) patch.role = form.role;
      if (form.main !== editing.main) patch.main = form.main;
      if (form.workloads.trim() !== (editing.workloads ?? "")) patch.workloads = form.workloads.trim();
      if (JSON.stringify(form.tags) !== JSON.stringify(editing.tags)) patch.tags = form.tags;
      // 0 and "" hand the field back to the service catalogue; -1 does the same for the interval.
      if (String(form.healthPort ?? "") !== String(editing.health_port ?? "")) {
        patch.health_port = form.healthPort === "" ? 0 : Number(form.healthPort);
      }
      if (form.healthPath.trim() !== (editing.health_path ?? "")) patch.health_path = form.healthPath.trim();
      const interval = editing.probe_min_interval_s ? String(editing.probe_min_interval_s) : "";
      if (form.probeInterval !== interval) patch.probe_min_interval_s = form.probeInterval === "" ? -1 : Number(form.probeInterval);
      if (String(form.connectTimeout ?? "") !== String(editing.connect_timeout_s ?? "")) {
        patch.connect_timeout_s = form.connectTimeout === "" ? 0 : Number(form.connectTimeout);
      }
      if (String(form.expectedCount ?? "") !== String(editing.expected_count ?? "")) {
        patch.expected_count = form.expectedCount === "" ? -1 : Number(form.expectedCount);
      }
      if (JSON.stringify(form.ignored) !== JSON.stringify(editing.ignored_containers ?? [])) patch.ignored_containers = form.ignored;
      if (form.paused !== editing.paused) patch.paused = form.paused;
      return api.settings.updateHost(editing.alias, patch);
    },
    onSuccess: (result) => {
      invalidate();
      notifications.show({
        title: editing ? `${result.alias} saved` : `${result.alias} added`,
        message: result.backup ? `A copy of the file before this change is in ${result.backup.split("/").slice(-1)[0]}` : "Settings updated",
        color: "teal",
      });
      onClose();
    },
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });

  const file = editing?.source ?? environment?.ssh_config_path ?? "";
  const phone = useIsPhone();
  const valid = form.alias.trim().length > 0 && form.env.length > 0;

  return (
    <ResponsiveModal opened={opened} onClose={onClose} title={editing ? `Edit ${editing.alias}` : "Add a host"} size="lg" centered>
      {/* The form scrolls inside the modal so the buttons stay reachable on a short screen. A
          phone's modal is the whole screen and scrolls itself, its buttons pinned at the bottom. */}
      <ScrollArea.Autosize mah={phone ? undefined : "calc(100vh - 240px)"} type="auto" offsetScrollbars>
        <Stack gap="sm" pr={4}>
        <Grid align="flex-end" gutter="sm">
          <Grid.Col span={{ base: 12, sm: 5 }}>
            <Select
              label="Environment"
              description="Where it is written"
              data={(settings?.environments ?? []).filter((e) => envNames.includes(e.name)).map((e) => ({ value: e.name, label: e.name }))}
              value={form.env}
              onChange={(value) => set("env", value ?? "")}
              disabled={!!editing}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 7 }}>
            <TextInput
              label="Alias"
              description={
                prefixed || !prefix ? "The name you type after ssh" : `New hosts here start with ${prefix}`
              }
              placeholder={prefixed ? "payments" : "prod-payments"}
              value={form.alias}
              // A whole alias pasted in loses the prefix it already carries, so it is not doubled.
              onChange={(e) => {
                const value = e.currentTarget.value;
                set("alias", prefixed && value.startsWith(prefix) ? value.slice(prefix.length) : value);
              }}
              classNames={{ input: "mono" }}
              leftSection={prefixed ? prefix : undefined}
              // Sized in ch of the same monospace font as the input, so the typed name starts
              // exactly where the prefix ends.
              leftSectionWidth={prefixed ? `calc(${prefix.length}ch + 12px)` : undefined}
              leftSectionPointerEvents="none"
              leftSectionProps={{
                className: "mono",
                style: { justifyContent: "flex-end", fontSize: "var(--mantine-font-size-sm)" },
              }}
            />
          </Grid.Col>

          <Grid.Col span={{ base: 12, sm: 6 }}>
            <TextInput
              label="Address"
              description="Private IP or DNS name"
              placeholder="10.0.12.34"
              value={form.hostname}
              onChange={(e) => set("hostname", e.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 8, sm: 3 }}>
            <TextInput
              label="User"
              description="Login name"
              placeholder="ubuntu"
              value={form.user}
              onChange={(e) => set("user", e.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 4, sm: 3 }}>
            <NumberInput
              label="Port"
              description="Default 22"
              placeholder="22"
              value={form.port}
              onChange={(value) => set("port", value)}
              min={1}
              max={65535}
              allowDecimal={false}
            />
          </Grid.Col>
        </Grid>

        <Select
          label="Connects through"
          description="The jump host this server sits behind. Choose direct for a server you can reach yourself."
          data={parents}
          value={form.proxyJump}
          onChange={(value) => set("proxyJump", value ?? "")}
          searchable
          leftSection={<IconArrowRight size={15} />}
        />

        <Autocomplete
          label="Identity file"
          description={
            environment?.key
              ? `Optional. Without one this host uses the ${form.env} key.`
              : "Optional. Without one this host uses your default SSH key."
          }
          placeholder="leave empty to use the environment key"
          value={form.identityFile}
          onChange={(value) => set("identityFile", value)}
          data={(keys.data ?? []).map((k) => k.path)}
        />

        <Divider label="sshfleet only — not written to the SSH file" labelPosition="left" my={2} />
        {editing && (
          <Switch
            // Orange, like any warning: it is a deliberate gap in what is being watched.
            color="orange"
            thumbIcon={form.paused ? <IconPlayerPause size={10} color="var(--mantine-color-orange-6)" /> : undefined}
            label={
              form.paused ? (
                <Text span c="orange.4" fw={600}>
                  Monitoring paused
                </Text>
              ) : (
                "Pause monitoring"
              )
            }
            description={
              editing.paused_by?.kind === "host" && editing.paused_by.name !== editing.alias
                ? `Already out of reach: it connects through ${editing.paused_by.name}, which is paused.`
                : editing.paused_by && editing.paused_by.kind !== "host"
                  ? `Already out of reach: ${editing.paused_by.kind} ${editing.paused_by.name} is paused. This switch is the host's own and stays as you leave it when that one is resumed.`
                  : "sshfleet closes its connection and makes no new one — no checks, shells, logs or files — until this is turned off."
            }
            checked={form.paused}
            onChange={(e) => set("paused", e.currentTarget.checked)}
          />
        )}
        <Grid align="flex-end" gutter="sm">
          <Grid.Col span={{ base: 12, sm: 6 }}>
            <TextInput
              label="Display name"
              description="Shown instead of the alias"
              placeholder="payments (rootless)"
              value={form.display}
              onChange={(e) => set("display", e.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 6, sm: 3 }}>
            <TextInput
              label="Short label"
              description="Tag in merged log output"
              placeholder={editing?.label ?? "api"}
              value={form.label}
              onChange={(e) => set("label", e.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 6, sm: 3 }}>
            <Select
              label="Role"
              description="Colour, and nginx logs"
              data={["service", "gateway", "frontend", "data", "ops"]}
              value={form.role}
              onChange={(value) => set("role", value ?? "service")}
              allowDeselect={false}
            />
          </Grid.Col>
        </Grid>
        <Group gap="lg" align="flex-start">
          <Switch
            label="In the default log search"
            description="Its running workloads are what the Logs page searches first"
            checked={form.main}
            onChange={(e) => set("main", e.currentTarget.checked)}
          />
          <TextInput
            label="Watch only these workloads"
            description="A glob over what the runtime reports; empty watches all of them"
            placeholder="api-*"
            value={form.workloads}
            onChange={(e) => set("workloads", e.currentTarget.value)}
            style={{ flex: 1 }}
          />
        </Group>
        <TagsInput label="Tags" value={form.tags} onChange={(value) => set("tags", value)} placeholder="optional" />

        <Grid align="flex-end" gutter="sm">
          <Grid.Col span={{ base: 12, sm: 3 }}>
            <NumberInput
              label="Health port"
              description="Empty uses the service"
              placeholder={editing?.health_port ? String(editing.health_port) : "none"}
              value={form.healthPort}
              onChange={(value) => set("healthPort", value)}
              min={1}
              max={65535}
              allowDecimal={false}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 4 }}>
            <TextInput
              label="Health path"
              description="Empty uses the service"
              placeholder={editing?.health_path ?? "/health/live"}
              value={form.healthPath}
              onChange={(e) => set("healthPath", e.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 5 }}>
            <Select
              label="Check this endpoint"
              description="Some endpoints log every call"
              data={[
                ...PROBE_CHOICES.map((value) => ({ value: String(value), label: everyLabel(value) })),
                ...(editing?.probe_min_interval_s && !PROBE_CHOICES.includes(editing.probe_min_interval_s)
                  ? [{ value: String(editing.probe_min_interval_s), label: everyLabel(editing.probe_min_interval_s) }]
                  : []),
              ]}
              value={form.probeInterval || "0"}
              onChange={(value) => set("probeInterval", value ?? "")}
              allowDeselect={false}
            />
          </Grid.Col>
        </Grid>
        <Text size="xs" c="dimmed" mt={-4}>
          Between checks the last answer is kept and shown with its age, so the column never goes blank.
        </Text>

        <Grid align="flex-end" gutter="sm">
          <Grid.Col span={{ base: 12, sm: 4 }}>
            <NumberInput
              label="Expected containers"
              description="Empty = learn from what is seen"
              placeholder="auto"
              value={form.expectedCount}
              onChange={(value) => set("expectedCount", value)}
              min={0}
              max={500}
              allowDecimal={false}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 4 }}>
            <NumberInput
              label="Connect timeout"
              description="Seconds. Empty = the fleet-wide one"
              placeholder="fleet default"
              value={form.connectTimeout}
              onChange={(value) => set("connectTimeout", value)}
              min={2}
              max={300}
              allowDecimal={false}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 4 }}>
            <TagsInput
              label="Ignored containers"
              description="Never alerted on. Wildcards allowed: destination-*"
              placeholder="old-worker, destination-*"
              value={form.ignored}
              onChange={(value) => set("ignored", value)}
            />
          </Grid.Col>
        </Grid>
        <Text size="xs" c="dimmed" mt={-4}>
          A container that was listed here once and has no trace now is reported as gone until it is back or ignored. An
          ignored name raises no alert at all and is left out of the count — that is how a job runner that starts and
          discards a container per task, such as Airbyte, is kept quiet: ignore <Text span className="mono">destination-*</Text>,
          not the one name you can see.
        </Text>

        <Card withBorder padding="sm" bg="dark.8">
          <Text size="xs" c="dimmed" mb={6}>
            {editing ? "The stanza will read" : "This will be appended to"} <Text span className="mono">{file}</Text>
          </Text>
          <Code block>
            {renderStanza({
              alias: fullAlias,
              hostname: form.hostname,
              user: form.user,
              port: form.port,
              proxyJump: form.proxyJump,
              identityFile: form.identityFile,
            })}
          </Code>
        </Card>

        </Stack>
      </ScrollArea.Autosize>
      <Divider my="sm" />
      <StickyActions>
        <Button variant="default" onClick={onClose}>
          Cancel
        </Button>
        <Button onClick={() => save.mutate()} loading={save.isPending} disabled={!valid}>
          {editing ? "Save changes" : "Add host"}
        </Button>
      </StickyActions>
    </ResponsiveModal>
  );
}

//: Rows drawn before the table asks whether you want the rest.
const PAGE = 50;

export default function HostsTab({ settings }: { settings: Settings | undefined }) {
  const hosts = useSettingsHosts();
  const config = useConfig();
  const invalidate = useInvalidateSettings();
  const { env: sidebarEnv, envNames, project } = useEnv();
  const [filter, setFilter] = useViewState("settings.hosts.filter", "");
  const env = sidebarEnv ?? "";
  const [modal, setModal] = useState<{ open: boolean; editing: HostSettings | null }>({ open: false, editing: null });
  const [removing, setRemoving] = useState<HostSettings | null>(null);
  const [confirmText, setConfirmText] = useState("");
  // The fleet host panel links here with ?host=<alias>, so one editor serves both pages.
  const [params, setParams] = useSearchParams();
  const requested = params.get("host");
  useEffect(() => {
    if (!requested || !hosts.data) return;
    const wanted = hosts.data.find((h) => h.alias === requested);
    if (wanted) setModal({ open: true, editing: wanted });
    const next = new URLSearchParams(params);
    next.delete("host");
    setParams(next, { replace: true });
  }, [requested, hosts.data, setParams]);

  const remove = useMutation({
    mutationFn: (host: HostSettings) => api.settings.removeHost(host.alias, confirmText),
    onSuccess: (result, host) => {
      invalidate();
      setRemoving(null);
      setConfirmText("");
      notifications.show({
        title: `${host.alias} removed`,
        message: result.backup ? `A copy of the file before the change is in ${result.backup.split("/").slice(-1)[0]}` : "The file was updated",
        color: "gray",
      });
    },
    onError: (error) => notifications.show({ title: "Could not remove", message: String((error as Error).message), color: "red" }),
  });

  const test = useMutation({
    mutationFn: (alias: string) => api.settings.testHost(alias),
    onSuccess: (result) =>
      notifications.show({
        title: result.ok ? `${result.alias} answered` : `${result.alias} did not answer`,
        message: result.ok ? `connected in ${result.seconds}s` : result.error,
        color: result.ok ? "teal" : "red",
      }),
  });

  const all = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (hosts.data ?? []).filter((host) => {
      if (env ? host.env !== env : !envNames.includes(host.env)) return false;
      if (!needle) return true;
      return [host.alias, host.hostname, host.user, host.proxy_jump, host.label, host.display]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle));
    });
  }, [hosts.data, filter, env, envNames]);

  // A fleet of any size makes this table taller than the screen many times over. A new filter
  // is a new question, answered from the top.
  const [limit, setLimit] = usePageLimit("settings.hosts.limit", JSON.stringify([filter, env, project]), PAGE);
  const rows = all.slice(0, limit);
  const more = all.length - rows.length;
  // Eight columns do not fit a phone: a host is a card there, its buttons spelled out.
  const phone = useIsPhone();

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-start">
        <Text size="sm" c="dimmed" maw={620}>
          These are the stanzas in your SSH config files. Editing one here rewrites only its own lines and keeps a copy of the file
          first; everything else in the file stays exactly as you wrote it.
        </Text>
        <Button leftSection={<IconPlus size={16} />} onClick={() => setModal({ open: true, editing: null })} disabled={!settings?.environments.length}>
          Add host
        </Button>
      </Group>

      <Group gap="sm">
        <SearchInput placeholder="Filter by alias, address, user or service…" value={filter} onChange={setFilter} w={{ base: "100%", sm: 340 }} />
        {/* No environment picker here: the sidebar is the one in the app, and two controls for
            the same thing disagreed with each other. The Env column says which is which. */}
        <Text size="sm" c="dimmed">
          {all.length} hosts in {env || project || "view"}
        </Text>
      </Group>

      <Card withBorder={!phone} padding={0} bg={phone ? "transparent" : undefined}>
        {all.length === 0 ? (
          <EmptyState title="No hosts match" hint="Register an environment, or clear the filter." />
        ) : phone ? (
          <Stack gap="sm">
            {rows.map((host) => (
              <DataCard key={host.alias}>
                <Group justify="space-between" wrap="nowrap" align="flex-start" gap="xs">
                  <div style={{ minWidth: 0 }}>
                    <Group gap={6} wrap="nowrap">
                      <Text size="sm" fw={600} className="mono" truncate>
                        {host.alias}
                      </Text>
                      {host.main && <IconStarFilled size={12} color="var(--mantine-color-yellow-5)" style={{ flexShrink: 0 }} />}
                    </Group>
                    {host.display && (
                      <Text size="xs" c="dimmed">
                        {host.display}
                      </Text>
                    )}
                  </div>
                  <EnvBadge env={host.env} environments={config.data?.environments} />
                </Group>
                {host.paused_by && (
                  <Text size="xs" c="orange.4" mt={4}>
                    {pauseReason(host)}
                  </Text>
                )}
                <Group grow mt={8} gap="sm" align="flex-start">
                  <Field label="Address">
                    <Text span inherit className="mono">
                      {host.user ? `${host.user}@` : ""}
                      {host.hostname ?? "—"}
                      {host.port ? `:${host.port}` : ""}
                    </Text>
                  </Field>
                  <Field label="Connects through">
                    <Text span inherit className="mono">
                      {host.proxy_jump ?? (host.is_bastion ? "bastion, directly" : "directly")}
                    </Text>
                  </Field>
                </Group>
                <Text size="xs" c="dimmed" mt={6} style={{ overflowWrap: "anywhere" }}>
                  {host.role}
                  {host.main ? " · in the default log search" : ""} · {host.source ?? "unknown file"}
                </Text>
                {!host.editable && (
                  <Text size="xs" c="dimmed" mt={4}>
                    Its file is not writable, so it cannot be removed from here.
                  </Text>
                )}
                <CardActions>
                  <Button
                    variant="default"
                    leftSection={<IconPlugConnected size={15} />}
                    loading={test.isPending && test.variables === host.alias}
                    onClick={() => test.mutate(host.alias)}
                  >
                    Test
                  </Button>
                  <Button variant="default" leftSection={<IconPencil size={15} />} onClick={() => setModal({ open: true, editing: host })}>
                    Edit
                  </Button>
                  <Button variant="outline" color="red" leftSection={<IconTrash size={15} />} disabled={!host.editable} onClick={() => setRemoving(host)}>
                    Remove
                  </Button>
                </CardActions>
              </DataCard>
            ))}
          </Stack>
        ) : (
          <Table.ScrollContainer minWidth={900}>
            <Table highlightOnHover verticalSpacing={8}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Alias</Table.Th>
                  <Table.Th w={80}>Env</Table.Th>
                  <Table.Th>Address</Table.Th>
                  <Table.Th w={110}>User</Table.Th>
                  <Table.Th>Connects through</Table.Th>
                  <Table.Th w={140}>Role</Table.Th>
                  <Table.Th w={150}>File</Table.Th>
                  <Table.Th w={110} />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {rows.map((host) => (
                  <Table.Tr key={host.alias}>
                    <Table.Td>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm" fw={600} className="mono">
                          {host.alias}
                        </Text>
                        {host.paused_by && (
                          <InfoTip label={pauseReason(host)} multiline maw={300}>
                            <Badge size="xs" variant="light" color="orange">
                              paused
                            </Badge>
                          </InfoTip>
                        )}
                      </Group>
                      {host.display && (
                        <Text size="xs" c="dimmed">
                          {host.display}
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td className="nowrap">
                      <EnvBadge env={host.env} environments={config.data?.environments} />
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" className="mono">
                        {host.hostname ?? "—"}
                        {host.port ? `:${host.port}` : ""}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" className="mono">
                        {host.user ?? "—"}
                      </Text>
                    </Table.Td>
                    <Table.Td className="nowrap">
                      {host.proxy_jump ? (
                        <Group gap={4} wrap="nowrap">
                          <IconArrowRight size={13} style={{ opacity: 0.5 }} />
                          <Text size="sm" className="mono">
                            {host.proxy_jump}
                          </Text>
                        </Group>
                      ) : (
                        <Badge size="xs" variant="light" color={host.is_bastion ? "grape" : "gray"}>
                          {host.is_bastion ? "bastion" : "direct"}
                        </Badge>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm">{host.role}</Text>
                        {host.main && (
                          <InfoTip label="In the default log search">
                            <IconStarFilled size={12} color="var(--mantine-color-yellow-5)" />
                          </InfoTip>
                        )}
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      <InfoTip label={host.source ?? "unknown"}>
                        <Text size="xs" c="dimmed" truncate>
                          {host.source?.split("/").slice(-1)[0] ?? "—"}
                        </Text>
                      </InfoTip>
                    </Table.Td>
                    <Table.Td>
                      <Group gap={4} justify="flex-end" wrap="nowrap">
                        <Tooltip label="Try connecting">
                          <ActionIcon
                            variant="subtle"
                            color="gray"
                            aria-label={`try connecting to ${host.alias}`}
                            onClick={() => test.mutate(host.alias)}
                            loading={test.isPending && test.variables === host.alias}
                          >
                            <IconPlugConnected size={15} />
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label="Edit">
                          <ActionIcon variant="subtle" color="gray" aria-label={`edit ${host.alias}`} onClick={() => setModal({ open: true, editing: host })}>
                            <IconPencil size={15} />
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label={host.editable ? "Remove from the ssh config" : "This file is not writable"}>
                          <ActionIcon variant="subtle" color="red" aria-label={`remove ${host.alias}`} disabled={!host.editable} onClick={() => setRemoving(host)}>
                            <IconTrash size={15} />
                          </ActionIcon>
                        </Tooltip>
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
        {more > 0 && (
          <>
            <Divider />
            <Group justify="space-between" px="md" py="xs">
              <Text size="xs" c="dimmed">
                Showing {rows.length} of {all.length}
              </Text>
              <Group gap="xs">
                <Button size="compact-sm" variant="subtle" onClick={() => setLimit((current) => current + PAGE)}>
                  Show {Math.min(more, PAGE)} more
                </Button>
                <Button size="compact-sm" variant="subtle" c="dimmed" onClick={() => setLimit(all.length)}>
                  Show all {all.length}
                </Button>
              </Group>
            </Group>
          </>
        )}
      </Card>

      <HostModal
        opened={modal.open}
        editing={modal.editing}
        settings={settings}
        hosts={hosts.data ?? []}
        envNames={envNames}
        onClose={() => setModal({ open: false, editing: null })}
      />

      <Modal
        opened={!!removing}
        onClose={() => {
          setRemoving(null);
          setConfirmText("");
        }}
        title={`Remove ${removing?.alias}?`}
        centered
      >
        <Stack gap="sm">
          <Text size="sm">
            This deletes the stanza from <Text span className="mono">{removing?.source}</Text>. The server itself is untouched, and a copy
            of the file is kept.
          </Text>
          <Alert color="yellow" variant="light">
            Type <Text span className="mono" fw={700}>{removing?.alias}</Text> to confirm.
          </Alert>
          <TextInput value={confirmText} onChange={(e) => setConfirmText(e.currentTarget.value)} placeholder={removing?.alias} autoFocus />
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setRemoving(null);
                setConfirmText("");
              }}
            >
              Cancel
            </Button>
            <Button color="red" disabled={confirmText !== removing?.alias} loading={remove.isPending} onClick={() => removing && remove.mutate(removing)}>
              Remove host
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
