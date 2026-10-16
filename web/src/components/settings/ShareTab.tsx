import {
  Accordion,
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  List,
  Loader,
  MultiSelect,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { Dropzone } from "@mantine/dropzone";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconAlertTriangle, IconArrowBackUp, IconDownload, IconFileImport, IconKey, IconShieldLock } from "@tabler/icons-react";
import { useState } from "react";

import { ApiError, api } from "../../api/client";
import type { ImportChange, ImportOptions, ImportResult, Settings } from "../../api/types";
import { useInvalidateSettings } from "../../hooks/useSettings";
import { saveBlob } from "../../lib/download";
import { fmtDateTime } from "../../lib/format";
import { useViewState } from "../../state/views";
import { useNoHover } from "../../lib/responsive";
import { ResponsiveModal, ResponsiveSegmented } from "../responsive";
import { DiffView } from "./DiffView";

const MAX_BUNDLE = 5 * 1024 * 1024;

const ACTIONS: Record<ImportChange["action"], { label: string; color: string }> = {
  add: { label: "new", color: "teal" },
  update: { label: "updated", color: "yellow" },
  remove: { label: "removed", color: "red" },
  conflict: { label: "conflict", color: "red" },
  "kept-local": { label: "kept yours", color: "blue" },
  skipped: { label: "skipped", color: "gray" },
  same: { label: "same", color: "gray" },
};

const KINDS: Record<ImportChange["kind"], string> = {
  project: "Projects",
  environment: "Environments",
  stanza: "Hosts in the SSH files",
  host: "Host settings",
  tunnel: "Tunnels",
  setting: "Fleet-wide settings",
};

const fileName = (path: string) => path.split("/").pop() ?? path;

function ExportCard({ settings }: { settings: Settings | undefined }) {
  // Kept between visits like every page's choices (state/views).
  const [projects, setProjects] = useViewState<string[]>("settings.share.projects", []);
  const [environments, setEnvironments] = useViewState<string[]>("settings.share.environments", []);
  const [withLocal, setWithLocal] = useViewState("settings.share.withLocal", false);
  const [allowCommands, setAllowCommands] = useViewState("settings.share.allowCommands", false);
  // The environments offered are exactly those of the chosen projects, read from the projects
  // themselves; with every project, the whole fleet goes and there is nothing to narrow.
  const projectNames = (settings?.projects ?? []).map((p) => p.name);
  const chosenProjects = projects.filter((name) => projectNames.includes(name));
  const offered = (settings?.environments ?? []).filter((e) => chosenProjects.includes(e.project));
  const chosenEnvironments = environments.filter((name) => offered.some((e) => e.name === name));
  const environmentData =
    chosenProjects.length > 1
      ? chosenProjects
          .map((project) => ({ group: project, items: offered.filter((e) => e.project === project).map((e) => e.name) }))
          .filter((group) => group.items.length > 0)
      : offered.map((e) => e.name);
  const pickProjects = (next: string[]) => {
    setProjects(next);
    const stillOffered = new Set((settings?.environments ?? []).filter((e) => next.includes(e.project)).map((e) => e.name));
    setEnvironments((current) => current.filter((name) => stillOffered.has(name)));
  };
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const projectOf = (name: string) => offered.find((e) => e.name === name)?.project;
  // Chosen environments go with their own projects only: a project with none of them chosen stays out.
  const leftOut = chosenProjects.filter((project) => !chosenEnvironments.some((name) => projectOf(name) === project));
  const going = !chosenProjects.length
    ? `everything: ${plural(projectNames.length, "project")}, ${plural((settings?.environments ?? []).length, "environment")}`
    : chosenEnvironments.length
      ? `only ${chosenEnvironments.map((name) => `${name} (${projectOf(name)})`).join(", ")}${
          leftOut.length ? ` — nothing from ${leftOut.join(", ")}` : ""
        }`
      : `${chosenProjects.join(", ")}, with ${offered.length === 1 ? `its one environment, ${offered[0].name}` : `all ${plural(offered.length, "environment")}: ${offered.map((e) => e.name).join(", ")}`}`;

  const exported = useMutation({
    mutationFn: () => api.share.export({ projects: chosenProjects, environments: chosenEnvironments, withLocal, allowCommands }),
    onSuccess: ({ blob, name, warnings }) => {
      saveBlob(blob, name);
      notifications.show({
        title: `Saved ${name}`,
        message: warnings.length ? (
          <Stack gap={2}>
            <Text size="sm">Left out on the way:</Text>
            {warnings.slice(0, 6).map((w) => (
              <Text key={w} size="xs" c="dimmed">
                {w}
              </Text>
            ))}
          </Stack>
        ) : (
          "No key, key path or credential is in it."
        ),
        color: "teal",
        autoClose: warnings.length ? 15_000 : 6000,
      });
    },
    onError: (error) => notifications.show({ title: "Not exported", message: String((error as Error).message), color: "red", autoClose: 15_000 }),
  });

  return (
    <Card withBorder padding="md">
      <Group gap="xs">
        <IconDownload size={18} />
        <Text fw={700}>Export</Text>
      </Group>
      <Text size="sm" c="dimmed" mt={4} maw={720}>
        One file with the fleet: projects, environments, the hosts and what is watched on them, tunnels, thresholds, and each
        environment's SSH file. Keys, key paths and anything that looks like a password stay on this machine — and if a secret would
        still be in the file, nothing is exported and you are told where it is.
      </Text>
      <Group mt="md" align="flex-end" gap="sm" wrap="wrap">
        <MultiSelect
          label="Projects"
          placeholder={chosenProjects.length ? undefined : "all of them"}
          data={projectNames}
          value={chosenProjects}
          onChange={pickProjects}
          clearable
          w={{ base: "100%", xs: 260 }}
        />
        {chosenProjects.length > 0 && (
          <MultiSelect
            label="Environments"
            placeholder={chosenEnvironments.length ? undefined : "all of them"}
            data={environmentData}
            value={chosenEnvironments}
            onChange={setEnvironments}
            clearable
            w={{ base: "100%", xs: 300 }}
            nothingFoundMessage="No environment in this project"
          />
        )}
      </Group>
      <Text size="xs" c="dimmed" mt={6}>
        Exports {going}.
      </Text>
      <Stack gap="xs" mt="md">
        <Switch
          checked={withLocal}
          onChange={(e) => setWithLocal(e.currentTarget.checked)}
          label="Include this machine's paths and pauses"
          description="Key paths, where the SSH files and downloads are, which hosts you paused. For moving your own setup to another computer — leave it off when sharing."
        />
        <Switch
          checked={allowCommands}
          onChange={(e) => setAllowCommands(e.currentTarget.checked)}
          color="red"
          label="Carry SSH lines that run programs"
          description="ProxyCommand, Include, Match exec… These run on whoever imports the file, so they are refused unless you allow them."
        />
      </Stack>
      <Group mt="md" justify="space-between" wrap="wrap">
        <Text size="xs" c="dimmed">
          To keep it in git as a folder: <Code>sshfleet config export --out ops/sshfleet/</Code>
        </Text>
        <Button leftSection={<IconDownload size={16} />} onClick={() => exported.mutate()} loading={exported.isPending}>
          Download bundle
        </Button>
      </Group>
    </Card>
  );
}

function ChangeGroups({ changes }: { changes: ImportChange[] }) {
  const visible = changes.filter((c) => c.action !== "same");
  const same = changes.length - visible.length;
  if (!visible.length) {
    return (
      <Text size="sm" c="dimmed">
        Everything in the bundle is already here{same ? ` (${same} item${same === 1 ? "" : "s"} checked)` : ""}.
      </Text>
    );
  }
  const kinds = Object.keys(KINDS) as ImportChange["kind"][];
  return (
    <Stack gap="sm">
      {kinds
        .filter((kind) => visible.some((c) => c.kind === kind))
        .map((kind) => (
          <div key={kind}>
            <Text size="xs" tt="uppercase" fw={700} c="dimmed" mb={4}>
              {KINDS[kind]}
            </Text>
            <Stack gap={4}>
              {visible
                .filter((c) => c.kind === kind)
                .map((change) => (
                  <Group key={`${change.kind}:${change.name}`} gap="xs" wrap="nowrap" align="flex-start">
                    <Badge size="xs" variant="light" color={ACTIONS[change.action].color} w={86} style={{ flexShrink: 0 }}>
                      {ACTIONS[change.action].label}
                    </Badge>
                    <div style={{ minWidth: 0 }}>
                      <Text size="sm" className="mono" truncate>
                        {change.name}
                      </Text>
                      {change.note && (
                        <Text size="xs" c="dimmed">
                          {change.note}
                        </Text>
                      )}
                    </div>
                  </Group>
                ))}
            </Stack>
          </div>
        ))}
      {same > 0 && (
        <Text size="xs" c="dimmed">
          {same} more already the same here.
        </Text>
      )}
    </Stack>
  );
}

function ImportModal({ file, onClose }: { file: { name: string; text: string } | null; onClose: () => void }) {
  const [options, setOptions] = useState<ImportOptions>({ mode: "merge", prefer: "local", withLocal: false, allowCommands: false });
  const [typed, setTyped] = useState("");
  const invalidate = useInvalidateSettings();
  const client = useQueryClient();
  const plan = useQuery({
    queryKey: ["import-plan", file?.name, file?.text.length, options],
    queryFn: () => api.share.plan(file!.text, options),
    enabled: !!file,
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });
  const apply = useMutation({
    mutationFn: () => api.share.apply(file!.text, options, plan.data!.plan.digest),
    onSuccess: (result: ImportResult) => {
      invalidate();
      void client.invalidateQueries({ queryKey: ["tunnels"] });
      const before = result.before;
      notifications.show({
        title: `Imported from ${result.bundle.created_on ?? "a bundle"}`,
        message: (
          <Stack gap={6}>
            <Text size="sm">{result.written.length ? result.written.map(fileName).join(", ") : "Nothing needed writing."}</Text>
            {before && (
              <Button
                size="compact-xs"
                variant="light"
                leftSection={<IconArrowBackUp size={13} />}
                style={{ alignSelf: "flex-start" }}
                onClick={() =>
                  api.history
                    .restore(before, before.slice(-6))
                    .then(() => {
                      invalidate();
                      notifications.show({ title: "Import undone", message: "The files are back as they were before it.", color: "teal" });
                    })
                    .catch((error: Error) => notifications.show({ title: "Could not undo", message: error.message, color: "red" }))
                }
              >
                Undo
              </Button>
            )}
          </Stack>
        ),
        color: result.ok ? "teal" : "red",
        autoClose: 20_000,
      });
      onClose();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        notifications.show({ title: "Something changed meanwhile", message: "The plan below is fresh — look at it again.", color: "yellow" });
        void plan.refetch();
        return;
      }
      notifications.show({ title: "Not imported", message: String((error as Error).message), color: "red", autoClose: 15_000 });
    },
  });

  const about = plan.data?.bundle;
  const summary = plan.data?.plan;
  const counts = (summary?.changes ?? []).reduce<Record<string, number>>((acc, c) => ({ ...acc, [c.action]: (acc[c.action] ?? 0) + 1 }), {});
  const replace = options.mode === "replace";
  const set = (patch: Partial<ImportOptions>) => setOptions((current) => ({ ...current, ...patch }));

  return (
    <ResponsiveModal opened={!!file} onClose={onClose} title={file ? `Import ${file.name}` : ""} size="xl">
      {file && (
        <Stack gap="md">
          {about && (
            <Text size="sm" c="dimmed">
              Made on <b>{about.created_on ?? "?"}</b>, {about.created_at ? fmtDateTime(about.created_at) : "?"} · sshfleet {about.sshfleet_version ?? "?"}
              {about.scope && (about.scope.projects.length || about.scope.environments.length)
                ? ` · only ${[...about.scope.projects, ...about.scope.environments].join(", ")}`
                : ""}
            </Text>
          )}
          <Group gap="lg" wrap="wrap" align="flex-end">
            <div>
              <Text size="xs" fw={600} mb={4}>
                How
              </Text>
              <ResponsiveSegmented
                phone="vertical"
                size="xs"
                value={options.mode}
                onChange={(value) => set({ mode: value as ImportOptions["mode"] })}
                data={[
                  { value: "merge", label: "Merge — add and update" },
                  { value: "replace", label: "Replace — the bundle's fleet" },
                ]}
              />
            </div>
            <div>
              <Text size="xs" fw={600} mb={4}>
                Where they differ
              </Text>
              <ResponsiveSegmented
                phone="vertical"
                size="xs"
                value={options.prefer}
                onChange={(value) => set({ prefer: value as ImportOptions["prefer"] })}
                data={[
                  { value: "local", label: "Keep mine" },
                  { value: "bundle", label: "Take the bundle's" },
                ]}
              />
            </div>
          </Group>
          <Group gap="lg">
            <Switch size="sm" label="Take its paths and pauses too" checked={options.withLocal} onChange={(e) => set({ withLocal: e.currentTarget.checked })} />
            <Switch
              size="sm"
              color="red"
              label="Accept SSH lines that run programs"
              checked={options.allowCommands}
              onChange={(e) => set({ allowCommands: e.currentTarget.checked })}
            />
          </Group>

          {plan.isLoading ? (
            <Group justify="center" py="lg">
              <Loader size="sm" />
            </Group>
          ) : plan.isError ? (
            <Alert color="red" variant="light" icon={<IconShieldLock size={16} />} title="This bundle is not imported">
              {String((plan.error as Error).message)}
            </Alert>
          ) : summary ? (
            <>
              <Group gap="xs">
                {(["add", "update", "remove", "conflict", "kept-local", "skipped"] as const)
                  .filter((action) => counts[action])
                  .map((action) => (
                    <Badge key={action} variant="light" color={ACTIONS[action].color}>
                      {counts[action]} {ACTIONS[action].label}
                    </Badge>
                  ))}
              </Group>
              {summary.needs_key.length > 0 && (
                <Alert color="yellow" variant="light" icon={<IconKey size={16} />}>
                  {summary.needs_key.join(", ")} {summary.needs_key.length === 1 ? "comes" : "come"} without a key on this machine. Set it in
                  Settings › Projects after importing; until then sshfleet uses your default keys.
                </Alert>
              )}
              {summary.needs_value.length > 0 && (
                <Alert color="yellow" variant="light" icon={<IconAlertTriangle size={16} />}>
                  These came as &lt;redacted&gt; and have no value here: {summary.needs_value.join(", ")}.
                </Alert>
              )}
              {summary.warnings.length > 0 && (
                <List size="sm" c="dimmed">
                  {summary.warnings.map((w) => (
                    <List.Item key={w}>{w}</List.Item>
                  ))}
                </List>
              )}
              <ChangeGroups changes={summary.changes} />
              {summary.files.length > 0 && (
                <Accordion variant="contained" chevronPosition="left">
                  {summary.files.map((f) => (
                    <Accordion.Item key={f.path} value={f.path}>
                      <Accordion.Control>
                        <Group gap="xs">
                          <Text size="sm" className="mono" fw={600}>
                            {fileName(f.path)}
                          </Text>
                          <Badge size="xs" variant="light" color={f.new ? "teal" : "yellow"}>
                            {f.new ? "new file" : "changed"}
                          </Badge>
                          <Text size="xs" c="dimmed" className="mono" truncate>
                            {f.path}
                          </Text>
                        </Group>
                      </Accordion.Control>
                      <Accordion.Panel>{f.diff ? <DiffView text={f.diff} /> : null}</Accordion.Panel>
                    </Accordion.Item>
                  ))}
                </Accordion>
              )}
              {replace && summary.changed && (
                <>
                  <Alert color="red" variant="light">
                    Replace unregisters every environment the bundle does not carry and drops host settings it does not have. No file is
                    deleted, and the state before is kept in History. Type <b>replace</b> to go on.
                  </Alert>
                  <TextInput value={typed} onChange={(e) => setTyped(e.currentTarget.value)} placeholder="replace" />
                </>
              )}
            </>
          ) : null}

          <Group justify="space-between">
            <Text size="xs" c="dimmed" maw={460}>
              Nothing is written until you import. The state before is kept in History, so an import can be undone.
            </Text>
            <Group gap="xs">
              <Button variant="default" onClick={onClose}>
                Cancel
              </Button>
              <Button
                leftSection={<IconFileImport size={16} />}
                disabled={!summary?.changed || (replace && typed.trim() !== "replace")}
                loading={apply.isPending}
                onClick={() => apply.mutate()}
              >
                Import
              </Button>
            </Group>
          </Group>
        </Stack>
      )}
    </ResponsiveModal>
  );
}

function ImportCard() {
  const [file, setFile] = useState<{ name: string; text: string } | null>(null);
  const noHover = useNoHover();
  return (
    <Card withBorder padding="md">
      <Group gap="xs">
        <IconFileImport size={18} />
        <Text fw={700}>Import</Text>
      </Group>
      <Text size="sm" c="dimmed" mt={4} maw={720}>
        Bring in a bundle — your own from another machine, or a teammate's. You see everything it would change first. By default it
        adds and updates, never removes, keeps your version wherever the two differ, and leaves your keys, paths and pauses alone.
      </Text>
      <Dropzone
        mt="md"
        multiple={false}
        maxSize={MAX_BUNDLE}
        accept={{ "application/json": [".json"] }}
        onDrop={async (files) => {
          const picked = files[0];
          if (picked) setFile({ name: picked.name, text: await picked.text() });
        }}
        onReject={() => notifications.show({ title: "Not a bundle", message: "A bundle is one .json file of at most 5 MB.", color: "red" })}
        styles={{ root: { padding: 18 } }}
      >
        <Group justify="center" gap="sm">
          <IconFileImport size={22} style={{ opacity: 0.6 }} />
          <div>
            <Text size="sm" fw={600}>
              {noHover ? "Tap to choose a bundle" : "Drop a bundle here, or click to choose one"}
            </Text>
            <Text size="xs" c="dimmed">
              A folder kept in git imports with <Code>sshfleet config import ops/sshfleet/</Code>
            </Text>
          </div>
        </Group>
      </Dropzone>
      <ImportModal file={file} onClose={() => setFile(null)} />
    </Card>
  );
}

export default function ShareTab({ settings }: { settings: Settings | undefined }) {
  return (
    <Stack gap="md">
      <ExportCard settings={settings} />
      <ImportCard />
    </Stack>
  );
}
