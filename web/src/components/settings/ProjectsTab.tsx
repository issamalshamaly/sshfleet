import {
  Alert,
  Badge,
  Code,
  Button,
  Card,
  Divider,
  Drawer,
  Group,
  Modal,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  IconAlertTriangle,
  IconChevronRight,
  IconPencil,
  IconPlayerPause,
  IconPlayerPlay,
  IconPlugConnected,
  IconPlus,
  IconTrash,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { api } from "../../api/client";
import type { ConnectionTest, EnvironmentSettings, PausedBy, ProjectSettings, Settings } from "../../api/types";
import { useInvalidateSettings, usePause } from "../../hooks/useSettings";
import { envColor } from "../../theme";
import { EmptyState } from "../common";
import { EnvironmentModal, KeyBadge, TestResult } from "./environments";

const COLORS = ["red", "orange", "yellow", "green", "teal", "cyan", "blue", "violet", "grape", "pink", "gray"];

export function ProjectModal({
  opened,
  onClose,
  editing,
}: {
  opened: boolean;
  onClose: () => void;
  editing: ProjectSettings | null;
}) {
  const invalidate = useInvalidateSettings();
  const [name, setName] = useState("");
  const [color, setColor] = useState("gray");
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (!opened) return;
    setName(editing?.name ?? "");
    setColor(editing?.color ?? "gray");
    setDescription(editing?.description ?? "");
  }, [opened, editing]);

  const save = useMutation({
    mutationFn: () => {
      const body = { name: name.trim(), color, description: description.trim() };
      return editing ? api.settings.updateProject(editing.name, body) : api.settings.addProject(body);
    },
    onSuccess: () => {
      invalidate();
      notifications.show({
        title: editing ? `${name.trim()} saved` : `${name.trim()} created`,
        message: editing
          ? "Every environment in it followed the rename."
          : "Open it to add its environments.",
        color: "teal",
      });
      onClose();
    },
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });

  return (
    <Modal opened={opened} onClose={onClose} title={editing ? `Edit ${editing.name}` : "Add a project"} centered>
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          Every environment belongs to a project — typically one per system you run. Apart from pausing it, which stops sshfleet
          connecting to every host in it, it is grouping only: nothing about how a host is reached comes from here.
        </Text>
        <TextInput label="Name" placeholder="acme" value={name} onChange={(e) => setName(e.currentTarget.value)} data-autofocus />
        <Select
          label="Colour"
          data={COLORS}
          value={color}
          onChange={(value) => setColor(value ?? "gray")}
          leftSection={<span style={{ width: 10, height: 10, borderRadius: 5, background: `var(--mantine-color-${envColor(color)}-6)` }} />}
        />
        <TextInput
          label="Description"
          description="Optional."
          placeholder="Customer-facing platform"
          value={description}
          onChange={(e) => setDescription(e.currentTarget.value)}
        />
        <Group justify="flex-end" mt="xs">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} loading={save.isPending} disabled={!name.trim()}>
            {editing ? "Save changes" : "Create project"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** Everything one project is and holds, without leaving the page it was clicked on: its
 *  environments are added, tested, edited and removed here, because this is the only place they
 *  are ever looked at as a set. */
function ProjectDrawer({
  project,
  environments,
  onClose,
  onEdit,
  onRemove,
  onAddEnvironment,
  onEditEnvironment,
}: {
  project: ProjectSettings | null;
  environments: EnvironmentSettings[];
  onClose: () => void;
  onEdit: () => void;
  onRemove: () => void;
  onAddEnvironment: () => void;
  onEditEnvironment: (environment: EnvironmentSettings) => void;
}) {
  const invalidate = useInvalidateSettings();
  const [tests, setTests] = useState<Record<string, ConnectionTest>>({});
  const [confirmRemove, setConfirmRemove] = useState<EnvironmentSettings | null>(null);
  // Pausing closes many connections at once — shells and tunnels included — so it is confirmed;
  // resuming only lets sshfleet look again, so it is not.
  const [confirmPause, setConfirmPause] = useState<{ target: PausedBy; hosts: number } | null>(null);
  const setPaused = usePause();
  const resuming = (target: PausedBy) =>
    setPaused.isPending && setPaused.variables?.target.kind === target.kind && setPaused.variables?.target.name === target.name;
  const members = environments.filter((e) => e.project === project?.name);

  const test = useMutation({
    mutationFn: (name: string) => api.settings.testEnvironment(name),
    onSuccess: (result, name) => setTests((current) => ({ ...current, [name]: result })),
    onError: (error) => notifications.show({ title: "Test failed", message: String((error as Error).message), color: "red" }),
  });

  const removeEnvironment = useMutation({
    mutationFn: (name: string) => api.settings.removeEnvironment(name),
    onSuccess: (_result, name) => {
      invalidate();
      setConfirmRemove(null);
      notifications.show({ title: `${name} removed`, message: "The SSH config file itself was not touched.", color: "gray" });
    },
    onError: (error) => notifications.show({ title: "Could not remove", message: String((error as Error).message), color: "red" }),
  });

  return (
    <Drawer
      opened={!!project}
      onClose={onClose}
      position="right"
      size={560}
      title={
        <Group gap={10}>
          <span
            style={{ width: 11, height: 11, borderRadius: 6, background: `var(--mantine-color-${envColor(project?.color ?? "gray")}-6)` }}
          />
          <Text fw={700} size="lg">
            {project?.name}
          </Text>
        </Group>
      }
    >
      {project && (
        <Stack gap="md">
          {project.description && (
            <Text size="sm" c="dimmed">
              {project.description}
            </Text>
          )}

          <Group gap="sm" justify="space-between">
            <Text size="sm" c="dimmed">
              {members.length} {members.length === 1 ? "environment" : "environments"} · {project.hosts} hosts
            </Text>
            <Group gap={4}>
              {project.paused ? (
                <Button
                  variant="light"
                  color="teal"
                  size="compact-xs"
                  leftSection={<IconPlayerPlay size={12} />}
                  loading={resuming({ kind: "project", name: project.name })}
                  onClick={() => setPaused.mutate({ target: { kind: "project", name: project.name }, paused: false })}
                >
                  Resume project
                </Button>
              ) : (
                <Button
                  variant="subtle"
                  color="orange"
                  size="compact-xs"
                  leftSection={<IconPlayerPause size={12} />}
                  disabled={project.hosts === 0}
                  onClick={() => setConfirmPause({ target: { kind: "project", name: project.name }, hosts: project.hosts })}
                >
                  Pause project
                </Button>
              )}
              <Button variant="subtle" size="compact-xs" leftSection={<IconPencil size={12} />} onClick={onEdit}>
                Edit project
              </Button>
            </Group>
          </Group>

          {project.paused && (
            <Alert color="orange" variant="light" icon={<IconPlayerPause size={16} />} title="Monitoring paused">
              sshfleet makes no connection to any host in this project — no checks, shells, logs, files or tunnels — until it is
              resumed. Each environment keeps its own pause switch for when it is.
            </Alert>
          )}

          <Divider />

          <Group justify="space-between">
            <Text fw={600} size="sm">
              Environments
            </Text>
            <Button size="compact-sm" leftSection={<IconPlus size={14} />} onClick={onAddEnvironment}>
              Add environment
            </Button>
          </Group>

          {members.length === 0 ? (
            <EmptyState
              title="No environments yet"
              hint="An environment is one SSH config file plus the key it uses. Register a file you already have and every host in it appears, or start empty and add servers on the Hosts tab."
            />
          ) : (
            <Stack gap="sm">
              {members.map((environment) => (
                <Card key={environment.name} withBorder padding="sm">
                  <Group justify="space-between" wrap="nowrap" align="flex-start">
                    <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
                      <span
                        style={{ width: 9, height: 9, borderRadius: 5, flexShrink: 0, background: `var(--mantine-color-${envColor(environment.color)}-6)` }}
                      />
                      <Text fw={600} size="sm" tt="capitalize">
                        {environment.name}
                      </Text>
                      <Badge size="xs" variant="light" color={envColor(environment.color)}>
                        {environment.short}
                      </Badge>
                      {environment.production && (
                        <Badge size="xs" color="red" variant="filled">
                          production
                        </Badge>
                      )}
                      {(environment.paused || project.paused) && (
                        <Badge size="xs" color="orange" variant="light" leftSection={<IconPlayerPause size={10} />}>
                          {environment.paused ? "paused" : "paused with the project"}
                        </Badge>
                      )}
                    </Group>
                    <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>
                      {environment.hosts} hosts
                    </Text>
                  </Group>

                  <Stack gap={4} mt="xs">
                    <Group gap={8} wrap="nowrap" align="flex-start">
                      <Text size="xs" c="dimmed" w={72} style={{ flexShrink: 0 }}>
                        Config file
                      </Text>
                      <Text size="xs" className="mono" style={{ wordBreak: "break-all" }}>
                        {environment.ssh_config_path}
                      </Text>
                      {!environment.ssh_config_exists && (
                        <Badge color="red" size="xs" variant="light">
                          missing
                        </Badge>
                      )}
                    </Group>
                    <Group gap={8} wrap="nowrap" align="flex-start">
                      <Text size="xs" c="dimmed" w={72} style={{ flexShrink: 0 }}>
                        Identity
                      </Text>
                      <KeyBadge info={environment.key} />
                    </Group>
                    <Group gap={8} wrap="nowrap" align="flex-start">
                      <Text size="xs" c="dimmed" w={72} style={{ flexShrink: 0 }}>
                        Bastions
                      </Text>
                      <Group gap={4}>
                        {environment.bastions.length === 0 ? (
                          <Text size="xs" c="dimmed">
                            none
                          </Text>
                        ) : (
                          environment.bastions.map((b) => (
                            <Badge key={b} size="xs" variant="light" color="grape">
                              {b}
                            </Badge>
                          ))
                        )}
                        {environment.bastions_auto && (
                          <Text size="xs" c="dimmed">
                            detected from ProxyJump
                          </Text>
                        )}
                      </Group>
                    </Group>
                  </Stack>

                  {tests[environment.name] && <TestResult result={tests[environment.name]} />}

                  <Group gap={4} mt="xs" justify="flex-end">
                    {environment.paused ? (
                      <Button
                        size="compact-xs"
                        variant="light"
                        color="teal"
                        leftSection={<IconPlayerPlay size={13} />}
                        loading={resuming({ kind: "environment", name: environment.name })}
                        onClick={() => setPaused.mutate({ target: { kind: "environment", name: environment.name }, paused: false })}
                      >
                        Resume
                      </Button>
                    ) : (
                      <Tooltip
                        label="Stop monitoring every host in this environment: their connections are closed and sshfleet makes no new one until you resume it."
                        multiline
                        maw={300}
                      >
                        <Button
                          size="compact-xs"
                          variant="subtle"
                          color="orange"
                          leftSection={<IconPlayerPause size={13} />}
                          disabled={environment.hosts === 0}
                          onClick={() => setConfirmPause({ target: { kind: "environment", name: environment.name }, hosts: environment.hosts })}
                        >
                          Pause
                        </Button>
                      </Tooltip>
                    )}
                    {/* A test opens connections, which is exactly what a pause forbids. */}
                    <Tooltip label="Paused — resume it to test the connection" disabled={!environment.paused && !project.paused}>
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        color="gray"
                        leftSection={<IconPlugConnected size={13} />}
                        loading={test.isPending && test.variables === environment.name}
                        disabled={environment.paused || project.paused}
                        onClick={() => test.mutate(environment.name)}
                      >
                        Test
                      </Button>
                    </Tooltip>
                    <Button size="compact-xs" variant="subtle" color="gray" leftSection={<IconPencil size={13} />} onClick={() => onEditEnvironment(environment)}>
                      Edit
                    </Button>
                    <Button size="compact-xs" variant="subtle" color="red" leftSection={<IconTrash size={13} />} onClick={() => setConfirmRemove(environment)}>
                      Remove
                    </Button>
                  </Group>
                </Card>
              ))}
            </Stack>
          )}

          <Divider />
          <Group justify="flex-end">
            <Tooltip
              label="Remove its environments first — an environment always belongs to a project"
              disabled={members.length === 0}
              multiline
              maw={260}
            >
              <Button color="red" variant="light" size="sm" leftSection={<IconTrash size={15} />} disabled={members.length > 0} onClick={onRemove}>
                Remove project
              </Button>
            </Tooltip>
          </Group>
        </Stack>
      )}

      <Modal
        opened={!!confirmRemove}
        onClose={() => setConfirmRemove(null)}
        title={`Remove ${confirmRemove?.name}?`}
        centered
        zIndex={400}
      >
        <Stack gap="sm">
          <Text size="sm">
            sshfleet forgets this environment and its {confirmRemove?.hosts} hosts disappear from the fleet view.
          </Text>
          <Alert color="gray" variant="light">
            The SSH config file <Text span className="mono">{confirmRemove?.ssh_config_path}</Text> is not touched, so nothing changes
            for plain <Code>ssh</Code>.
          </Alert>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmRemove(null)}>
              Cancel
            </Button>
            <Button
              color="red"
              loading={removeEnvironment.isPending}
              onClick={() => confirmRemove && removeEnvironment.mutate(confirmRemove.name)}
            >
              Remove
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={!!confirmPause}
        onClose={() => setConfirmPause(null)}
        title={confirmPause ? `Pause ${confirmPause.target.kind} ${confirmPause.target.name}?` : ""}
        centered
        zIndex={400}
      >
        <Stack gap="sm">
          <Text size="sm">
            sshfleet closes every connection it holds to the {confirmPause?.hosts} {confirmPause?.hosts === 1 ? "host" : "hosts"} in it —
            open shells, log streams and tunnels through them end — and makes no new one until you resume it. Their alerts are dropped,
            and they are counted apart on the fleet page.
          </Text>
          <Alert color="gray" variant="light">
            Nothing changes on the servers: what runs there keeps running. Only sshfleet stops looking.
          </Alert>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmPause(null)}>
              Cancel
            </Button>
            <Button
              color="orange"
              leftSection={<IconPlayerPause size={15} />}
              loading={setPaused.isPending}
              onClick={() =>
                confirmPause &&
                setPaused.mutate({ target: confirmPause.target, paused: true }, { onSuccess: () => setConfirmPause(null) })
              }
            >
              Pause
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Drawer>
  );
}


export default function ProjectsTab({ settings }: { settings: Settings | undefined }) {
  const invalidate = useInvalidateSettings();
  const [modal, setModal] = useState<{ open: boolean; editing: ProjectSettings | null }>({ open: false, editing: null });
  const [opened, setOpened] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<ProjectSettings | null>(null);
  const [envModal, setEnvModal] = useState<{ open: boolean; editing: EnvironmentSettings | null }>({ open: false, editing: null });

  const projects = settings?.projects ?? [];
  const environments = settings?.environments ?? [];
  const selected = projects.find((p) => p.name === opened) ?? null;

  const remove = useMutation({
    mutationFn: (name: string) => api.settings.removeProject(name, name),
    onSuccess: (_result, name) => {
      invalidate();
      setConfirmRemove(null);
      setOpened(null);
      notifications.show({ title: `${name} removed`, message: "It had no environments.", color: "gray" });
    },
    onError: (error) => notifications.show({ title: "Could not remove", message: String((error as Error).message), color: "red" }),
  });

  // Plain `ssh <alias>` only sees files under ~/.ssh/config.d through an Include line in the
  // user's own config. sshfleet never needs it, so it is offered, never added on its own.
  const include = useQuery({ queryKey: ["settings", "ssh-include"], queryFn: api.settings.sshInclude, enabled: environments.length > 0 });
  const addInclude = useMutation({
    mutationFn: api.settings.addSshInclude,
    onSuccess: (result) => {
      invalidate();
      notifications.show({
        title: "Include line added",
        message: result.backup ? `A copy of your previous ${result.config} is in ${result.backup.split("/").slice(-1)[0]}` : `${result.config} written`,
        color: "teal",
      });
    },
    onError: (error) => notifications.show({ title: "Could not edit ~/.ssh/config", message: String((error as Error).message), color: "red" }),
  });
  const includeMissing = include.data ? !include.data.includes_config_d : false;

  return (
    <Stack gap="md">
      {includeMissing && include.data && (
        <Alert color="yellow" variant="light" icon={<IconAlertTriangle size={16} />} title="Plain ssh does not read these files yet">
          <Text size="sm">
            sshfleet reaches every host through its own generated config, so nothing here is broken. But{" "}
            <Code>ssh &lt;alias&gt;</Code> in a terminal only sees files under <Text span className="mono">config.d</Text> when{" "}
            <Text span className="mono">{include.data.config}</Text> includes them. This line at the top of that file fixes it:
          </Text>
          <Code block mt={6}>
            {include.data.line}
          </Code>
          <Group mt="sm" gap="sm">
            <Button size="xs" variant="light" color="yellow" loading={addInclude.isPending} onClick={() => addInclude.mutate()}>
              Add it for me
            </Button>
            <Text size="xs" c="dimmed">
              A copy of the file is kept under the backups folder first. Nothing else in it changes.
            </Text>
          </Group>
        </Alert>
      )}
      <Group justify="space-between" align="flex-start">
        <Text size="sm" c="dimmed" maw={640}>
          A project is the outermost thing sshfleet knows about — typically one per system you run, with its staging and production
          inside it. Every environment belongs to exactly one. A project or an environment can be paused as a whole; otherwise it is
          grouping only: nothing about how a host is reached comes from it.
        </Text>
        <Button leftSection={<IconPlus size={16} />} onClick={() => setModal({ open: true, editing: null })}>
          Add project
        </Button>
      </Group>

      {projects.length === 0 ? (
        <Card withBorder>
          <EmptyState
            title="No projects yet"
            hint="A project comes first. Create one, then register the SSH config files of the environments that belong to it."
          />
        </Card>
      ) : (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md">
          {projects.map((project) => (
            <UnstyledButton key={project.name} onClick={() => setOpened(project.name)}>
              <Card withBorder padding="md" h="100%" className="clickable-card">
                <Group justify="space-between" wrap="nowrap" align="flex-start">
                  <Group gap={9} wrap="nowrap" style={{ minWidth: 0 }}>
                    <span
                      style={{ width: 11, height: 11, borderRadius: 6, flexShrink: 0, background: `var(--mantine-color-${envColor(project.color)}-6)` }}
                    />
                    <Text fw={700} truncate>
                      {project.name}
                    </Text>
                    {project.paused && (
                      <Badge size="xs" color="orange" variant="light" leftSection={<IconPlayerPause size={10} />} style={{ flexShrink: 0 }}>
                        paused
                      </Badge>
                    )}
                  </Group>
                  <IconChevronRight size={16} style={{ opacity: 0.5, flexShrink: 0 }} />
                </Group>

                <Group gap={6} mt="sm" wrap="wrap">
                  {project.environments.length === 0 ? (
                    <Text size="xs" c="dimmed">
                      no environments yet
                    </Text>
                  ) : (
                    project.environments.map((name) => {
                      const environment = environments.find((e) => e.name === name);
                      return (
                        <Badge
                          key={name}
                          size="sm"
                          variant="light"
                          color={envColor(environment?.color ?? "gray")}
                          leftSection={environment?.paused ? <IconPlayerPause size={10} /> : undefined}
                        >
                          {name}
                        </Badge>
                      );
                    })
                  )}
                </Group>

                <Text size="xs" c="dimmed" mt="sm">
                  {project.hosts} {project.hosts === 1 ? "host" : "hosts"}
                </Text>

                {project.description && (
                  <Text size="xs" c="dimmed" mt={6} lineClamp={2}>
                    {project.description}
                  </Text>
                )}
              </Card>
            </UnstyledButton>
          ))}
        </SimpleGrid>
      )}

      <ProjectDrawer
        project={selected}
        environments={environments}
        onClose={() => setOpened(null)}
        onEdit={() => selected && setModal({ open: true, editing: selected })}
        onRemove={() => selected && setConfirmRemove(selected)}
        onAddEnvironment={() => setEnvModal({ open: true, editing: null })}
        onEditEnvironment={(environment) => setEnvModal({ open: true, editing: environment })}
      />

      <EnvironmentModal
        opened={envModal.open}
        editing={envModal.editing}
        settings={settings}
        projects={projects}
        defaultProject={opened ?? undefined}
        onClose={() => setEnvModal({ open: false, editing: null })}
      />

      <ProjectModal opened={modal.open} editing={modal.editing} onClose={() => setModal({ open: false, editing: null })} />

      <Modal opened={!!confirmRemove} onClose={() => setConfirmRemove(null)} title={`Remove ${confirmRemove?.name}?`} centered>
        <Stack gap="sm">
          <Text size="sm">This project has no environments. Nothing else changes.</Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmRemove(null)}>
              Cancel
            </Button>
            <Button color="red" loading={remove.isPending} onClick={() => confirmRemove && remove.mutate(confirmRemove.name)}>
              Remove
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
