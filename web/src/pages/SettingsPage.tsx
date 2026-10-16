import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  List,
  NumberInput,
  Select,
  Stack,
  Switch,
  Tabs,
  Text,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation } from "@tanstack/react-query";
import {
  IconAlertTriangle,
  IconClockPlay,
  IconFolderCog,
  IconHistory,
  IconPlayerPlay,
  IconRefresh,
  IconServer2,
  IconStack2,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import HistoryTab from "../components/settings/HistoryTab";
import ProjectsTab from "../components/settings/ProjectsTab";
import { useEnv } from "../state/env";
import HostsTab from "../components/settings/HostsTab";
import { useConfig } from "../hooks/useFleet";
import { useInvalidateSettings, useSettings } from "../hooks/useSettings";
import { SOUND_NAMES, play, requestDesktopPermission, unlock } from "../lib/sounds";

const PRESETS = [
  { label: "15s", value: 15 },
  { label: "30s", value: 30 },
  { label: "1m", value: 60 },
  { label: "2m", value: 120 },
  { label: "5m", value: 300 },
];

function RefreshTab() {
  const settings = useSettings();
  const invalidate = useInvalidateSettings();
  const [auto, setAuto] = useState(true);
  const [seconds, setSeconds] = useState<number | string>(30);
  const [stale, setStale] = useState<number | string>(90);
  const [connect, setConnect] = useState<number | string>(10);
  const [collector, setCollector] = useState<number | string>(25);
  const [thresholds, setThresholds] = useState<Record<string, number>>({});

  useEffect(() => {
    const data = settings.data;
    if (!data) return;
    setAuto(data.refresh.fast_s > 0);
    setSeconds(data.refresh.fast_s || 30);
    setStale(data.refresh.stale_after_s);
    setConnect(data.refresh.connect_timeout_s);
    setCollector(data.refresh.collector_timeout_s);
    setThresholds({ ...data.thresholds });
  }, [settings.data]);

  const saveRefresh = useMutation({
    mutationFn: () =>
      api.settings.setRefresh({
        fast_s: auto ? Number(seconds) : 0,
        stale_after_s: Number(stale),
        connect_timeout_s: Number(connect),
        collector_timeout_s: Number(collector),
      }),
    onSuccess: (result) => {
      invalidate();
      notifications.show({
        title: "Saved",
        message: result.refresh.fast_s
          ? `Every host is checked every ${result.refresh.fast_s} seconds, starting now.`
          : "Automatic checks are off; use the refresh button when you want one.",
        color: "teal",
      });
    },
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });

  const saveThresholds = useMutation({
    mutationFn: () => api.settings.setThresholds(thresholds),
    onSuccess: () => {
      invalidate();
      notifications.show({ title: "Saved", message: "New alert levels apply from the next check.", color: "teal" });
    },
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });

  const threshold = (key: string, label: string, suffix = "%") => (
    <NumberInput
      label={label}
      suffix={suffix}
      value={thresholds[key] ?? 0}
      onChange={(value) => setThresholds((current) => ({ ...current, [key]: Number(value) }))}
      min={1}
      max={key === "load_per_cpu_warn" ? 64 : 100}
      step={key === "load_per_cpu_warn" ? 0.1 : 1}
      decimalScale={key === "load_per_cpu_warn" ? 1 : 0}
      allowDecimal={key === "load_per_cpu_warn"}
    />
  );

  return (
    <Stack gap="md">
      <Card withBorder padding="md">
        <Text fw={700}>How often the fleet is checked</Text>
        <Text size="sm" c="dimmed" mt={4} maw={640}>
          A sweep asks every host for its status at once and takes about a second and a half once the connections are warm. Changes here
          take effect immediately — there is no need to restart anything.
        </Text>

        <Group mt="md" align="flex-end" gap="lg" wrap="wrap">
          <Switch
            checked={auto}
            onChange={(event) => setAuto(event.currentTarget.checked)}
            label="Check automatically"
            size="md"
          />
          <NumberInput
            label="Every"
            suffix=" seconds"
            value={seconds}
            onChange={setSeconds}
            min={5}
            max={3600}
            step={5}
            disabled={!auto}
            w={190}
            allowDecimal={false}
          />
          <Group gap={6}>
            {PRESETS.map((preset) => (
              <Button
                key={preset.value}
                size="xs"
                variant={auto && Number(seconds) === preset.value ? "filled" : "default"}
                onClick={() => {
                  setAuto(true);
                  setSeconds(preset.value);
                }}
              >
                {preset.label}
              </Button>
            ))}
          </Group>
        </Group>

        {!auto && (
          <Alert color="yellow" variant="light" mt="md" icon={<IconAlertTriangle size={16} />}>
            With automatic checks off, the fleet only updates when you press refresh. Alerts will not appear on their own.
          </Alert>
        )}

        <Group mt="lg" gap="md" align="flex-end" wrap="wrap">
          <NumberInput
            label="Call data stale after"
            description="Older than this is shown greyed out"
            suffix=" s"
            value={stale}
            onChange={setStale}
            min={10}
            max={3600}
            w={220}
            allowDecimal={false}
          />
          <NumberInput
            label="Connection timeout"
            description="How long to wait for a host to answer"
            suffix=" s"
            value={connect}
            onChange={setConnect}
            min={2}
            max={120}
            w={220}
            allowDecimal={false}
          />
          <NumberInput
            label="Status script timeout"
            description="Upper bound for one host's status"
            suffix=" s"
            value={collector}
            onChange={setCollector}
            min={5}
            max={300}
            w={220}
            allowDecimal={false}
          />
        </Group>

        <Group mt="md">
          <Button onClick={() => saveRefresh.mutate()} loading={saveRefresh.isPending}>
            Save
          </Button>
        </Group>
      </Card>

      <Card withBorder padding="md">
        <Text fw={700}>When to warn</Text>
        <Text size="sm" c="dimmed" mt={4}>
          Levels that turn a host yellow or red. They apply from the next check.
        </Text>
        <Group mt="md" gap="md" align="flex-end" wrap="wrap">
          {threshold("disk_warn_pct", "Disk warning")}
          {threshold("disk_crit_pct", "Disk critical")}
          {threshold("mem_warn_pct", "Memory warning")}
          {threshold("mem_crit_pct", "Memory critical")}
          {threshold("load_per_cpu_warn", "Load per CPU", "")}
        </Group>
        <Group mt="md">
          <Button onClick={() => saveThresholds.mutate()} loading={saveThresholds.isPending}>
            Save
          </Button>
        </Group>
      </Card>
      <SoundsCard />
    </Stack>
  );
}

function SoundsCard() {
  const config = useConfig();
  const invalidate = useInvalidateSettings();
  const sounds = config.data?.ui.sounds;
  const [desktopState, setDesktopState] = useState<string>(typeof Notification === "undefined" ? "unsupported" : Notification.permission);

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.settings.setSounds(body),
    onSuccess: () => invalidate(),
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });

  const row = (key: "crit" | "warn" | "done", label: string, hint: string) => (
    <Group key={key} align="flex-end" gap="sm" wrap="nowrap">
      <Select
        label={label}
        description={hint}
        data={SOUND_NAMES}
        value={sounds?.[key] ?? "none"}
        onChange={(value) => value && save.mutate({ [key]: value })}
        allowDeselect={false}
        w={300}
        disabled={!sounds?.enabled}
      />
      <Tooltip label="Play it">
        <ActionIcon
          variant="default"
          size="lg"
          aria-label={`play ${key}`}
          disabled={(sounds?.[key] ?? "none") === "none"}
          onClick={() => {
            unlock();
            play(sounds?.[key]);
          }}
        >
          <IconPlayerPlay size={16} />
        </ActionIcon>
      </Tooltip>
    </Group>
  );

  return (
    <Card withBorder padding="md">
      <Text fw={700}>Sounds</Text>
      <Text size="sm" c="dimmed" mt={4} maw={640}>
        A short sound when something happens while you are looking elsewhere. Browsers only allow sound after you have clicked
        somewhere on the page, so nothing plays until then. The choice is saved with your settings, not in this browser.
      </Text>
      <Stack gap="sm" mt="md">
        <Switch
          label="Play sounds"
          checked={sounds?.enabled ?? false}
          onChange={(event) => {
            unlock();
            save.mutate({ enabled: event.currentTarget.checked });
          }}
          size="md"
        />
        {row("crit", "Critical alert", "A host or container down, a crash")}
        {row("warn", "Warning", "Disk, memory, a stopped container, a failed probe")}
        {row("done", "Job finished", "A restart or a removal has completed")}
        <Group align="center" gap="sm" mt={4}>
          <Switch
            label="Desktop notification for critical alerts when this tab is in the background"
            checked={sounds?.desktop ?? false}
            disabled={!sounds?.enabled || desktopState === "unsupported" || desktopState === "denied"}
            onChange={async (event) => {
              const on = event.currentTarget.checked;
              if (on) {
                const result = await requestDesktopPermission();
                setDesktopState(result);
                if (result !== "granted") {
                  notifications.show({
                    title: "Notifications not allowed",
                    message: "The browser did not grant permission; the sound still plays.",
                    color: "yellow",
                  });
                  return;
                }
              }
              save.mutate({ desktop: on });
            }}
          />
          {desktopState === "denied" && (
            <Text size="xs" c="dimmed">
              blocked in the browser's site settings
            </Text>
          )}
        </Group>
      </Stack>
    </Card>
  );
}

function PathsTab() {
  const settings = useSettings();
  const navigate = useNavigate();
  const invalidate = useInvalidateSettings();
  const reload = useMutation({
    mutationFn: api.settings.reload,
    onSuccess: (summary) => {
      invalidate();
      const moved = [...(summary.added ?? []), ...(summary.removed ?? []), ...(summary.changed ?? [])];
      notifications.show({
        title: moved.length ? `Reloaded — ${moved.length} hosts changed` : "Reloaded — nothing changed",
        message: moved.slice(0, 8).join(", ") || `${summary.hosts} hosts in the inventory`,
        color: "teal",
      });
    },
    onError: (error) => notifications.show({ title: "Reload failed", message: String((error as Error).message), color: "red" }),
  });

  const paths = settings.data?.paths;
  const rows: [string, string | undefined][] = [
    ["Settings file", paths?.config_file],
    ["Generated ssh config", paths?.ssh_config],
    ["Backups of your ssh files", paths?.backups],
    ["Downloads", paths?.download_dir],
    ["Audit trail", paths?.audit_file],
  ];

  return (
    <Stack gap="md">
      <Card withBorder padding="md">
        <Text fw={700}>Watched files</Text>
        <Text size="sm" c="dimmed" mt={4} maw={680}>
          These are re-read a couple of seconds after you save them, so a host you add in an editor shows up without restarting
          sshfleet. Editing them by hand and editing them on the Hosts tab are the same thing.
        </Text>
        <List size="sm" mt="sm" spacing={4}>
          {(settings.data?.watching ?? []).map((path) => (
            <List.Item key={path}>
              <Text span className="mono" size="sm">
                {path}
              </Text>
            </List.Item>
          ))}
        </List>
        <Group mt="md" gap="sm">
          <Button variant="default" leftSection={<IconRefresh size={15} />} onClick={() => reload.mutate()} loading={reload.isPending}>
            Reload now
          </Button>
          {settings.data?.last_reload ? (
            <Text size="sm" c="dimmed">
              last reload {new Date(settings.data.last_reload * 1000).toLocaleTimeString()} · {settings.data.last_reload_reason}
            </Text>
          ) : null}
        </Group>
      </Card>

      <Card withBorder padding="md">
        <Text fw={700}>Where things live</Text>
        <Stack gap={6} mt="sm">
          {rows.map(([label, value]) => (
            <Group key={label} gap="sm" wrap="nowrap">
              <Text size="sm" c="dimmed" w={200}>
                {label}
              </Text>
              <Text size="sm" className="mono">
                {value ?? "—"}
              </Text>
            </Group>
          ))}
        </Stack>
        <Text size="sm" c="dimmed" mt="sm">
          Every earlier state of these files, together, is kept under{" "}
          <Anchor size="sm" onClick={() => navigate("/settings?tab=history")}>
            History
          </Anchor>
          , where any of them can be put back.
        </Text>
      </Card>

      {(settings.data?.warnings ?? []).length > 0 && (
        <Alert color="yellow" variant="light" title="Warnings" icon={<IconAlertTriangle size={16} />}>
          <List size="sm">
            {settings.data?.warnings.map((warning) => (
              <List.Item key={warning}>{warning}</List.Item>
            ))}
          </List>
        </Alert>
      )}
    </Stack>
  );
}

export default function SettingsPage() {
  const settings = useSettings();
  const { project, envNames } = useEnv();
  const [params, setParams] = useSearchParams();
  const TABS = ["projects", "hosts", "checks", "files", "history"];
  const requested = params.get("tab") ?? "";
  const tab = TABS.includes(requested) ? requested : "projects";

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-start">
        <div>
          <Text fw={700} size="lg">
            Settings
          </Text>
          <Text size="sm" c="dimmed">
            Projects, the servers inside them, and how often they are checked. Keys are recorded as paths and never opened.
          </Text>
        </div>
        <Tooltip label={project ? `Every host in ${project}` : "Every host sshfleet knows about"}>
          <Badge size="lg" variant="light" color="gray" leftSection={<IconServer2 size={13} />}>
            {(settings.data?.environments ?? [])
              .filter((e) => envNames.includes(e.name))
              .reduce((total, e) => total + e.hosts, 0)}{" "}
            hosts
          </Badge>
        </Tooltip>
      </Group>

      <Tabs value={tab} onChange={(value) => setParams(value && value !== "projects" ? { tab: value } : {})} keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="projects" leftSection={<IconStack2 size={15} />}>
            Projects
          </Tabs.Tab>
          <Tabs.Tab value="hosts" leftSection={<IconServer2 size={15} />}>
            Hosts
          </Tabs.Tab>
          <Tabs.Tab value="checks" leftSection={<IconClockPlay size={15} />}>
            Checks
          </Tabs.Tab>
          <Tabs.Tab value="files" leftSection={<IconFolderCog size={15} />}>
            Paths
          </Tabs.Tab>
          <Tabs.Tab value="history" leftSection={<IconHistory size={15} />}>
            History
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="projects" pt="md">
          <ProjectsTab settings={settings.data} />
        </Tabs.Panel>

        <Tabs.Panel value="hosts" pt="md">
          <HostsTab settings={settings.data} />
        </Tabs.Panel>
        <Tabs.Panel value="checks" pt="md">
          <RefreshTab />
        </Tabs.Panel>
        <Tabs.Panel value="files" pt="md">
          <PathsTab />
        </Tabs.Panel>
        <Tabs.Panel value="history" pt="md">
          <HistoryTab />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
