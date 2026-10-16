import {
  Alert,
  Autocomplete,
  Badge,
  Button,
  Card,
  Code,
  Divider,
  Grid,
  Group,
  List,
  Loader,
  ScrollArea,
  Select,
  Stack,
  TagsInput,
  Text,
  TextInput,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  IconAlertTriangle,
  IconCheck,
  IconFilePlus,
  IconKey,
  IconStack2,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { api } from "../../api/client";
import type { ConnectionTest, EnvironmentSettings, KeyInfo, ProjectSettings, Settings } from "../../api/types";
import { useInvalidateSettings } from "../../hooks/useSettings";
import { useIsPhone } from "../../lib/responsive";
import { envColor } from "../../theme";
import { InfoTip, ResponsiveModal, ResponsiveSegmented, StickyActions } from "../responsive";

/** The pieces that manage one environment: its add/edit form, the key badge and the
 *  connection-test result. They live with the project panel that hosts them. */
const COLORS = ["red", "orange", "yellow", "green", "teal", "cyan", "blue", "violet", "grape", "pink", "gray"];

export function KeyBadge({ info, path }: { info: KeyInfo | null; path?: string }) {
  if (!info) {
    return (
      <Text size="sm" c="dimmed">
        {path
          ? "not checked yet — its permissions are verified when you save"
          : "none — hosts use their own IdentityFile or your default key"}
      </Text>
    );
  }
  return (
    // A long key path wraps rather than pushing its badge off a narrow screen.
    <Group gap={8} wrap="wrap">
      <Text size="sm" className="mono" style={{ overflowWrap: "anywhere" }}>
        {info.path}
      </Text>
      {info.ok ? (
        <Badge color="teal" variant="light" size="sm">
          mode {info.mode}
        </Badge>
      ) : (
        <InfoTip label={info.problems.join("; ") + (info.fix ? ` — fix: ${info.fix}` : "")} multiline maw={340}>
          <Badge color="red" variant="light" size="sm" leftSection={<IconAlertTriangle size={11} />}>
            {info.exists ? `mode ${info.mode}` : "missing"}
          </Badge>
        </InfoTip>
      )}
    </Group>
  );
}

export function TestResult({ result }: { result: ConnectionTest }) {
  const rows = Object.entries(result.bastions ?? {});
  return (
    <Alert color={result.ok ? "teal" : "red"} variant="light" mt="sm" title={result.ok ? "Connected" : "Could not connect"}>
      <List size="sm" spacing={2}>
        {rows.map(([alias, outcome]) => (
          <List.Item key={alias} icon={outcome.ok ? <IconCheck size={14} color="var(--mantine-color-teal-5)" /> : <IconAlertTriangle size={14} color="var(--mantine-color-red-5)" />}>
            bastion <Text span className="mono">{alias}</Text> — {outcome.ok ? `ok in ${outcome.seconds}s` : outcome.error}
          </List.Item>
        ))}
        {result.host && (
          <List.Item icon={result.host.ok ? <IconCheck size={14} color="var(--mantine-color-teal-5)" /> : <IconAlertTriangle size={14} color="var(--mantine-color-red-5)" />}>
            host <Text span className="mono">{result.host.alias}</Text> — {result.host.ok ? `ok in ${result.host.seconds}s` : result.host.error}
          </List.Item>
        )}
      </List>
    </Alert>
  );
}

interface ModalProps {
  opened: boolean;
  onClose: () => void;
  editing: EnvironmentSettings | null;
  settings: Settings | undefined;
}

export function EnvironmentModal({
  opened,
  onClose,
  editing,
  projects,
  defaultProject,
}: ModalProps & { projects: ProjectSettings[]; defaultProject?: string }) {
  const invalidate = useInvalidateSettings();
  // "existing": point at a file that already lists the servers. "new": start with none and
  // let sshfleet create a private file under ~/.ssh/config.d that the Hosts tab fills in.
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [short, setShort] = useState("");
  const [color, setColor] = useState("cyan");
  const [keyPath, setKeyPath] = useState("");
  const [bastions, setBastions] = useState<string[]>([]);
  const [project, setProject] = useState<string | null>(null);

  const candidates = useQuery({ queryKey: ["settings", "ssh-configs"], queryFn: api.settings.sshConfigs, enabled: opened });
  const keys = useQuery({ queryKey: ["settings", "keys"], queryFn: api.settings.keys, enabled: opened });

  useEffect(() => {
    if (!opened) return;
    setMode("existing");
    setPath(editing?.ssh_config_path ?? "");
    setName(editing?.name ?? "");
    setShort(editing?.short ?? "");
    setColor(editing?.color ?? "cyan");
    setKeyPath(editing?.key?.path ?? "");
    setBastions(editing?.bastions_auto ? [] : (editing?.bastions ?? []));
    // With one project there is nothing to choose; with several the operator picks.
    // Opened from a project's own panel, so that is the project unless it is being moved.
    setProject(editing?.project ?? defaultProject ?? (projects.length === 1 ? projects[0].name : null));
  }, [opened, editing, projects, defaultProject]);

  // Parse the chosen file as soon as it looks like a path, so the operator sees what they get.
  // Starting empty, the file is named <project>_<short>, so those drive the preview instead.
  const preview = useQuery({
    queryKey: ["settings", "preview", mode, ...(mode === "new" ? [project, short.trim(), name.trim()] : [path])],
    queryFn: () =>
      mode === "new"
        ? api.settings.previewEnvironment("", { name: name.trim(), short: short.trim() || undefined, project })
        : api.settings.previewEnvironment(path),
    enabled: opened && (mode === "new" ? name.trim().length > 0 && !!project : path.trim().length > 3),
    // The file name follows each keystroke; keep showing the last one rather than flicker
    // (but never carry an existing file's preview over into "start empty").
    placeholderData: (previous, previousQuery) =>
      mode === "new" && previousQuery?.queryKey[2] === "new" ? previous : undefined,
    retry: false,
  });

  useEffect(() => {
    if (editing || !preview.data || mode === "new") return;
    setName((current) => current || preview.data.suggested.name);
    setShort((current) => current || preview.data.suggested.short);
    setColor((current) => (current === "cyan" ? preview.data.suggested.color : current));
    const suggestedKey = preview.data.key_candidates.find((k) => k?.path)?.path;
    if (suggestedKey) setKeyPath((current) => current || suggestedKey);
  }, [preview.data, editing]);

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        short: short.trim(),
        color,
        key_path: keyPath.trim(),
        bastions,
        project,
      };
      return editing
        ? api.settings.updateEnvironment(editing.name, { ...body, ssh_config_path: path.trim() })
        : api.settings.addEnvironment({ ...body, name: name.trim(), ssh_config_path: mode === "new" ? "" : path.trim() });
    },
    onSuccess: (result) => {
      invalidate();
      const created = "created_file" in result ? result.created_file : null;
      notifications.show({
        title: editing ? `${editing.name} updated` : `${name} registered`,
        message: created
          ? `Created ${created}. Add its servers from the Hosts tab.`
          : `${result.reload?.hosts ?? 0} hosts in the inventory`,
        color: "teal",
        autoClose: created ? 8000 : 4000,
      });
      onClose();
    },
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });

  const parseError = preview.isError ? String((preview.error as Error).message) : null;
  const phone = useIsPhone();
  const ready = name.trim().length > 0 && !!project && (mode === "new" || path.trim().length > 3) && !save.isPending;

  return (
    <ResponsiveModal opened={opened} onClose={onClose} title={editing ? `Edit ${editing.name}` : "Add an environment"} size="lg" centered>
      <ScrollArea.Autosize mah={phone ? undefined : "calc(100vh - 240px)"} type="auto" offsetScrollbars>
        <Stack gap="sm" pr={4}>
        {!editing && (
          // Two long labels side by side are cut to "Use an existing SS…" on a phone; stacked they fit.
          <ResponsiveSegmented
            phone="vertical"
            fullWidth
            size="sm"
            value={mode}
            onChange={(value) => setMode(value as "existing" | "new")}
            data={[
              { value: "existing", label: "Use an existing SSH config file" },
              { value: "new", label: "Start empty and add servers later" },
            ]}
          />
        )}
        {mode === "new" && !editing && (
          <Alert color="teal" variant="light" icon={<IconFilePlus size={16} />} title="A file of its own">
            {preview.data && !preview.data.new_file ? (
              <>
                <Text span className="mono">{preview.data.path}</Text> already exists, so sshfleet uses it as it is (
                {preview.data.summary.hosts} hosts).
              </>
            ) : (
              <>
                sshfleet creates a private, empty file{" "}
                {preview.data?.path ? (
                  <>
                    at <Text span className="mono">{preview.data.path}</Text>
                  </>
                ) : (
                  <>
                    under <Text span className="mono">~/.ssh/config.d/</Text> named{" "}
                    <Text span className="mono">&lt;project&gt;_&lt;short label&gt;</Text>
                  </>
                )}{" "}
                in plain OpenSSH syntax.
              </>
            )}{" "}
            Every server you add on the Hosts tab is written there, so it stays yours and works with plain <Code>ssh</Code>{" "}
            too.
          </Alert>
        )}
        {mode === "existing" && (
        <Autocomplete
          label="SSH config file"
          description="The file that already lists these servers. sshfleet reads it; it stays yours to edit."
          placeholder="~/.ssh/config.d/production"
          value={path}
          onChange={setPath}
          data={(candidates.data ?? []).map((c) => c.path)}
          renderOption={({ option }) => {
            const candidate = candidates.data?.find((c) => c.path === option.value);
            return (
              <Group justify="space-between" w="100%" wrap={phone ? "wrap" : "nowrap"}>
                <Text size="sm" className="mono" style={{ overflowWrap: "anywhere" }}>
                  {option.value}
                </Text>
                <Group gap={6}>
                  {candidate?.registered_to && (
                    <Badge size="xs" variant="light" color="gray">
                      {candidate.registered_to}
                    </Badge>
                  )}
                  <Text size="xs" c="dimmed">
                    {candidate?.hosts ?? 0} hosts
                  </Text>
                </Group>
              </Group>
            );
          }}
          disabled={save.isPending}
        />
        )}

        {preview.isFetching && mode === "existing" && (
          <Group gap={8}>
            <Loader size={14} />
            <Text size="sm" c="dimmed">
              reading the file…
            </Text>
          </Group>
        )}
        {parseError && (
          <Alert color="red" variant="light" title="Cannot use this file">
            {parseError}
          </Alert>
        )}
        {preview.data && mode === "existing" && (
          <Card withBorder padding="sm" bg="dark.7">
            <Group gap="lg" wrap="wrap">
              <Text size="sm">
                <Text span fw={700}>
                  {preview.data.summary.hosts}
                </Text>{" "}
                hosts
              </Text>
              {preview.data.prefix && (
                <Text size="sm" c="dimmed">
                  prefix <Code>{preview.data.prefix}*</Code>
                </Text>
              )}
              <Text size="sm" c="dimmed">
                users {Object.keys(preview.data.summary.users).join(", ") || "—"}
              </Text>
              <Text size="sm" c="dimmed">
                {preview.data.summary.without_identity_file} without an IdentityFile
              </Text>
            </Group>
            <Group gap={6} mt={8}>
              {preview.data.bastions.map((b) => (
                <Badge key={b} size="sm" variant="light" color="grape">
                  bastion {b}
                </Badge>
              ))}
            </Group>
          </Card>
        )}

        <Grid align="flex-end" gutter="sm">
          <Grid.Col span={{ base: 12, sm: 5 }}>
            <TextInput
              label="Name"
              description="How you refer to it"
              placeholder="production"
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
              disabled={!!editing}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 3 }}>
            <TextInput
              label="Short label"
              description={mode === "new" && !editing ? "For tables and the file name" : "For tables"}
              placeholder="prod"
              value={short}
              onChange={(e) => setShort(e.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 4 }}>
            <Select
              label="Colour"
              description="Its badge"
              data={COLORS}
              value={color}
              onChange={(value) => setColor(value ?? "cyan")}
              leftSection={<span style={{ width: 10, height: 10, borderRadius: 5, background: `var(--mantine-color-${envColor(color)}-6)` }} />}
            />
          </Grid.Col>
        </Grid>

        <Autocomplete
          label="Identity file"
          description="Recorded as a path and checked with stat. sshfleet never opens a key."
          placeholder="~/.ssh/production.pem"
          leftSection={<IconKey size={15} />}
          value={keyPath}
          onChange={setKeyPath}
          data={(keys.data ?? []).map((k) => k.path)}
        />
        {keyPath.trim() && (
          <KeyBadge
            path={keyPath.trim()}
            info={
              keys.data?.find((k) => k.path === keyPath.trim()) ??
              preview.data?.key_candidates.find((k) => k?.path === keyPath.trim()) ??
              null
            }
          />
        )}

        <TagsInput
          label="Bastions"
          description="Leave empty to detect them from ProxyJump."
          value={bastions}
          onChange={setBastions}
          placeholder="auto"
        />

        {/* Opened from inside a project, a new environment's project is not a choice — it is
            where you are. Editing one still offers it, because moving it is the only way. */}
        {(!defaultProject || editing) && (
          <Select
            label="Project"
            description="The project this environment belongs to. Grouping only — it changes nothing about how hosts are reached."
            leftSection={<IconStack2 size={15} />}
            placeholder="Choose a project"
            data={projects.map((p) => p.name)}
            value={project}
            onChange={setProject}
            error={project ? null : "An environment belongs to a project"}
            allowDeselect={false}
          />
        )}

        </Stack>
      </ScrollArea.Autosize>
      <Divider my="sm" />
      <StickyActions>
        <Button variant="default" onClick={onClose}>
          Cancel
        </Button>
        <Button onClick={() => save.mutate()} loading={save.isPending} disabled={!ready}>
          {editing ? "Save changes" : "Register environment"}
        </Button>
      </StickyActions>
    </ResponsiveModal>
  );
}
