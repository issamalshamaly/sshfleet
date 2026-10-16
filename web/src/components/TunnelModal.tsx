import { Alert, Button, Divider, Grid, Group, NumberInput, Select, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import type { Host, Tunnel } from "../api/types";
import { ResponsiveModal, StickyActions } from "./responsive";

/** What people actually tunnel to here, so the common cases are one click. */
const COMMON = [
  { label: "Kibana", port: 5601, local: 15601, url: true },
  { label: "Elasticsearch", port: 9200, local: 19200, url: false },
  { label: "Grafana", port: 3000, local: 13000, url: true },
  { label: "Seq", port: 8001, local: 18001, url: true },
  { label: "Postgres", port: 5432, local: 15432, url: false },
];

/** Said rather than clamped: a typo like 133006 must not quietly become 65535 and get saved. */
function portError(value: number | string): string | null {
  if (value === "") return null;
  const port = Number(value);
  return port >= 1 && port <= 65535 ? null : "A port is 1 to 65535";
}

interface Props {
  opened: boolean;
  onClose: () => void;
  editing: Tunnel | null;
  hosts: Host[];
  environments: { name: string }[];
  /** The environment being looked at, if one is; a new tunnel starts there. */
  defaultEnv: string | null;
}

export default function TunnelModal({ opened, onClose, editing, hosts, environments, defaultEnv }: Props) {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [env, setEnv] = useState("");
  const [via, setVia] = useState("");
  const [remoteHost, setRemoteHost] = useState("127.0.0.1");
  const [remotePort, setRemotePort] = useState<number | string>(5601);
  const [local, setLocal] = useState<number | string>(15601);
  const [url, setUrl] = useState("");
  // A name you typed is yours; a name a preset suggested is replaced by the next preset.
  const [nameTouched, setNameTouched] = useState(false);

  useEffect(() => {
    if (!opened) return;
    setNameTouched(false);
    if (editing) {
      const [host, port] = editing.remote.split(":");
      setName(editing.name);
      setEnv(editing.env);
      setVia(editing.via);
      setRemoteHost(host);
      setRemotePort(Number(port));
      setLocal(editing.local);
      setUrl(editing.url ?? "");
    } else {
      setName("");
      setEnv(defaultEnv ?? environments[0]?.name ?? "");
      setVia("");
      setRemoteHost("127.0.0.1");
      setRemotePort(5601);
      setLocal(15601);
      setUrl("");
    }
  }, [opened, editing, environments, defaultEnv]);

  const viaOptions = useMemo(() => {
    const inEnv = hosts.filter((h) => h.env === env);
    const sorted = [...inEnv].sort((a, b) => Number(b.is_bastion) - Number(a.is_bastion) || a.alias.localeCompare(b.alias));
    return sorted.map((h) => ({ value: h.alias, label: h.is_bastion ? `${h.alias}  (bastion)` : h.alias }));
  }, [hosts, env]);

  const save = useMutation({
    mutationFn: () => {
      const body = {
        env,
        via,
        remote_host: remoteHost.trim(),
        remote_port: Number(remotePort),
        local: Number(local),
        open_url: url.trim() || null,
      };
      return editing ? api.settings.updateTunnel(editing.name, body) : api.settings.addTunnel({ ...body, name: name.trim() });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["tunnels"] });
      void client.invalidateQueries({ queryKey: ["settings"] });
      notifications.show({ title: editing ? `${editing.name} saved` : `${name} added`, message: "", color: "teal" });
      onClose();
    },
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });

  const applyCommon = (preset: (typeof COMMON)[number]) => {
    setRemotePort(preset.port);
    setLocal(preset.local);
    if (!editing && !nameTouched) setName(`${preset.label.toLowerCase()}-${env || "env"}`);
    setUrl(preset.url ? `http://127.0.0.1:${preset.local}` : "");
  };

  const valid =
    name.trim().length > 0 &&
    env.length > 0 &&
    via.length > 0 &&
    local !== "" &&
    remotePort !== "" &&
    !portError(local) &&
    !portError(remotePort);

  return (
    <ResponsiveModal opened={opened} onClose={onClose} title={editing ? `Edit ${editing.name}` : "Add a tunnel"} size="lg" centered>
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          A tunnel forwards a local port to something only the servers can reach. It rides on the connection sshfleet already holds
          open, so opening one costs nothing extra.
        </Text>

        <Group gap={6}>
          <Text size="xs" c="dimmed" mr={4}>
            Common:
          </Text>
          {COMMON.map((preset) => (
            <Button key={preset.label} size="compact-xs" variant="default" onClick={() => applyCommon(preset)}>
              {preset.label}
            </Button>
          ))}
        </Group>

        <Grid align="flex-end" gutter="sm">
          <Grid.Col span={{ base: 12, sm: 7 }}>
            <TextInput
              label="Name"
              description="What you will call it"
              placeholder="kibana-prod"
              value={name}
              onChange={(e) => {
                setName(e.currentTarget.value);
                setNameTouched(true);
              }}
              disabled={!!editing}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 5 }}>
            <Select
              label="Environment"
              description="Where it lives"
              data={environments.map((e) => ({ value: e.name, label: e.name }))}
              value={env}
              onChange={(value) => {
                setEnv(value ?? "");
                setVia("");
              }}
            />
          </Grid.Col>
        </Grid>

        <Select
          label="Through"
          description="Usually the bastion. The forward starts from this server."
          data={viaOptions}
          value={via}
          onChange={(value) => setVia(value ?? "")}
          searchable
          nothingFoundMessage="No host in this environment"
        />

        <Grid align="flex-end" gutter="sm">
          <Grid.Col span={{ base: 12, sm: 6 }}>
            <TextInput
              label="Remote address"
              description="As seen from that server"
              value={remoteHost}
              onChange={(e) => setRemoteHost(e.currentTarget.value)}
              placeholder="127.0.0.1"
            />
          </Grid.Col>
          <Grid.Col span={{ base: 6, sm: 3 }}>
            <NumberInput
              label="Remote port"
              description="Over there"
              value={remotePort}
              onChange={setRemotePort}
              min={1}
              max={65535}
              clampBehavior="none"
              allowDecimal={false}
              allowNegative={false}
              error={portError(remotePort)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 6, sm: 3 }}>
            <NumberInput
              label="Local port"
              description="On this machine"
              value={local}
              onChange={setLocal}
              min={1}
              max={65535}
              clampBehavior="none"
              allowDecimal={false}
              allowNegative={false}
              error={portError(local)}
            />
          </Grid.Col>
        </Grid>

        <TextInput
          label="Open in browser"
          description="Optional. Shown as a link once the tunnel is open."
          value={url}
          onChange={(e) => setUrl(e.currentTarget.value)}
          placeholder={`http://127.0.0.1:${local || 15601}`}
        />

        <Alert color="gray" variant="light">
          <Text size="sm" className="mono">
            127.0.0.1:{local || "…"} → {remoteHost || "…"}:{remotePort || "…"} via {via || "…"}
          </Text>
        </Alert>

        <Divider my={2} />
        <StickyActions>
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} loading={save.isPending} disabled={!valid}>
            {editing ? "Save changes" : "Add tunnel"}
          </Button>
        </StickyActions>
      </Stack>
    </ResponsiveModal>
  );
}
