import {
  Alert,
  Badge,
  Box,
  Button,
  Card,
  Checkbox,
  Divider,
  Drawer,
  Group,
  Loader,
  Modal,
  ScrollArea,
  SegmentedControl,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { IconAlertTriangle, IconArrowBackUp, IconEye, IconHistory } from "@tabler/icons-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../../api/client";
import type { FileDiff, HistorySource, HistoryState, RestorePlanSummary, RestoreResult } from "../../api/types";
import { useHistory, useHistoryDetail, useInvalidateSettings } from "../../hooks/useSettings";
import { fmtAge, fmtDateTime } from "../../lib/format";
import { usePageLimit, useViewState } from "../../state/views";
import { EmptyState } from "../common";

//: States drawn before the table asks whether you want older ones.
const PAGE = 50;

const SOURCES: Record<HistorySource, { label: string; color: string; hint: string }> = {
  web: { label: "this page", color: "teal", hint: "Changed from the sshfleet web page" },
  watcher: { label: "outside edit", color: "yellow", hint: "Edited outside sshfleet — in an editor — and noticed by the server" },
  disk: { label: "outside edit", color: "yellow", hint: "Edited outside sshfleet, noticed when a command ran" },
  cli: { label: "command line", color: "blue", hint: "Changed by an sshfleet command" },
  start: { label: "server start", color: "gray", hint: "How the files were when the server started" },
  restore: { label: "restore", color: "grape", hint: "Put back from an earlier state" },
  import: { label: "import", color: "cyan", hint: "Brought in from a bundle" },
};

const FILTERS = [
  { value: "all", label: "Everything" },
  { value: "here", label: "Changes made here" },
  { value: "outside", label: "Outside edits" },
  { value: "restores", label: "Restores & imports" },
];

function matches(filter: string, source: HistorySource): boolean {
  if (filter === "here") return source === "web" || source === "cli";
  if (filter === "outside") return source === "watcher" || source === "disk";
  if (filter === "restores") return source === "restore" || source === "import";
  return true;
}

const fileName = (path: string) => path.split("/").pop() ?? path;

/** The file's name first, its folder after it and dimmed: a long folder is cut, never the name. */
function PathLabel({ path }: { path: string }) {
  const name = fileName(path);
  const folder = path.slice(0, path.length - name.length);
  return (
    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
      <Text size="sm" className="mono" fw={600} style={{ flexShrink: 0 }}>
        {name}
      </Text>
      <Tooltip label={path} openDelay={400} multiline maw={520}>
        <Text size="xs" c="dimmed" className="mono" truncate="start">
          {folder}
        </Text>
      </Tooltip>
    </Group>
  );
}
const ageOf = (at: string) => fmtAge((Date.now() - Date.parse(at)) / 1000);

/** What a state changed, as a few chips: the parts of config.yaml, the hosts that came and went,
 *  or the SSH files when no host did. */
function ChangeChips({ state }: { state: HistoryState }) {
  const changes = state.changes ?? {};
  if (changes.first) {
    return (
      <Badge size="xs" variant="outline" color="gray">
        first record
      </Badge>
    );
  }
  const config = changes.config ?? [];
  const added = changes.hosts_added ?? [];
  const removed = changes.hosts_removed ?? [];
  const ssh = (changes.files ?? []).filter((f) => f.role === "ssh");
  return (
    <Group gap={4} wrap="wrap">
      {config.slice(0, 4).map((part) => (
        <Badge key={part} size="xs" variant="outline" color="gray" className="mono" tt="none">
          {part}
        </Badge>
      ))}
      {config.length > 4 && (
        <Text size="xs" c="dimmed">
          +{config.length - 4}
        </Text>
      )}
      {added.length > 0 && (
        <Tooltip label={added.join(", ")} multiline maw={360}>
          <Badge size="xs" variant="light" color="teal">
            +{added.length} host{added.length === 1 ? "" : "s"}
          </Badge>
        </Tooltip>
      )}
      {removed.length > 0 && (
        <Tooltip label={removed.join(", ")} multiline maw={360}>
          <Badge size="xs" variant="light" color="red">
            −{removed.length} host{removed.length === 1 ? "" : "s"}
          </Badge>
        </Tooltip>
      )}
      {!added.length && !removed.length &&
        ssh.slice(0, 2).map((f) => (
          <Badge key={f.path} size="xs" variant="outline" color="blue" className="mono" tt="none">
            {fileName(f.path)}
          </Badge>
        ))}
      {!config.length && !added.length && !removed.length && !ssh.length && (
        <Text size="xs" c="dimmed">
          —
        </Text>
      )}
    </Group>
  );
}

/** A unified diff, coloured line by line. */
function DiffView({ text }: { text: string }) {
  const lines = text.split("\n");
  const shown = lines.slice(0, 3000);
  return (
    <ScrollArea.Autosize mah={420} type="auto" offsetScrollbars>
      <Box component="pre" className="mono" m={0} style={{ fontSize: 12, lineHeight: 1.5 }}>
        {shown.map((line, index) => {
          const header = line.startsWith("+++") || line.startsWith("---");
          const color = header
            ? "var(--mantine-color-dimmed)"
            : line.startsWith("+")
              ? "var(--mantine-color-teal-3)"
              : line.startsWith("-")
                ? "var(--mantine-color-red-3)"
                : line.startsWith("@@")
                  ? "var(--mantine-color-cyan-4)"
                  : undefined;
          const background =
            !header && line.startsWith("+")
              ? "rgba(32, 201, 151, 0.10)"
              : !header && line.startsWith("-")
                ? "rgba(250, 82, 82, 0.10)"
                : undefined;
          return (
            <div key={index} style={{ color, background, padding: "0 8px", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
              {line || " "}
            </div>
          );
        })}
      </Box>
      {lines.length > shown.length && (
        <Text size="xs" c="dimmed" px={8}>
          … {lines.length - shown.length} more lines
        </Text>
      )}
    </ScrollArea.Autosize>
  );
}

const CHANGE_LABEL: Record<FileDiff["change"], { label: string; color: string }> = {
  changed: { label: "changed", color: "yellow" },
  added: { label: "new", color: "teal" },
  removed: { label: "gone", color: "red" },
  same: { label: "same", color: "gray" },
  unavailable: { label: "not kept", color: "gray" },
};

function DiffList({ diffs, emptyText }: { diffs: FileDiff[]; emptyText: string }) {
  const visible = diffs.filter((d) => d.change !== "same");
  if (!visible.length) {
    return (
      <Text size="sm" c="dimmed">
        {emptyText}
      </Text>
    );
  }
  return (
    <Stack gap="sm">
      {visible.map((diff) => (
        <Card key={diff.path} withBorder padding={0}>
          <Group justify="space-between" px="sm" py={6} wrap="nowrap" style={{ borderBottom: diff.diff ? "1px solid var(--mantine-color-dark-4)" : undefined }}>
            <PathLabel path={diff.path} />
            <Badge size="xs" variant="light" color={CHANGE_LABEL[diff.change].color} style={{ flexShrink: 0 }}>
              {CHANGE_LABEL[diff.change].label}
            </Badge>
          </Group>
          {diff.note && (
            <Text size="xs" c="dimmed" px="sm" py={4}>
              {diff.note}
            </Text>
          )}
          {diff.diff && <DiffView text={diff.diff} />}
        </Card>
      ))}
    </Stack>
  );
}

function isPlan(value: unknown): value is RestorePlanSummary {
  return !!value && typeof value === "object" && "writes" in value;
}

/** The restore as one mutation, and the notification that offers to undo it. Undo restores the
 *  state recorded just before — the click is the confirmation, so its code is sent for you. */
function useRestore() {
  const invalidate = useInvalidateSettings();
  const client = useQueryClient();
  const announce = (result: RestoreResult, undoing: boolean) => {
    invalidate();
    void client.invalidateQueries({ queryKey: ["tunnels"] });
    const written = result.written.length;
    const before = result.before;
    notifications.show({
      title: undoing ? "Put back as it was" : written ? `Restored — ${written} file${written === 1 ? "" : "s"} written` : "Nothing to change",
      message: (
        <Stack gap={6}>
          <Text size="sm">
            {written ? result.written.map(fileName).join(", ") : "The files already held that state."}
            {result.left_in_place.length ? ` · left in place: ${result.left_in_place.map(fileName).join(", ")}` : ""}
          </Text>
          {!undoing && written > 0 && before && (
            <Button
              size="compact-xs"
              variant="light"
              leftSection={<IconArrowBackUp size={13} />}
              style={{ alignSelf: "flex-start" }}
              onClick={() => {
                api.history
                  .restore(before, before.slice(-6))
                  .then((undone) => announce(undone, true))
                  .catch((error: Error) => notifications.show({ title: "Could not undo", message: error.message, color: "red" }));
              }}
            >
              Undo
            </Button>
          )}
        </Stack>
      ),
      color: result.ok ? "teal" : "red",
      autoClose: undoing ? 6000 : 20_000,
    });
  };
  return useMutation({
    mutationFn: ({ id, code, files }: { id: string; code: string; files?: string[] }) => api.history.restore(id, code, files),
    onSuccess: (result) => announce(result, false),
    onError: (error) => notifications.show({ title: "Not restored", message: String((error as Error).message), color: "red", autoClose: 10_000 }),
  });
}

function StateDrawer({ state, latest, onClose, onRestore }: { state: HistoryState | null; latest: boolean; onClose: () => void; onRestore: () => void }) {
  const [against, setAgainst] = useState<"previous" | "current">("previous");
  useEffect(() => setAgainst("previous"), [state?.id]);
  const detail = useHistoryDetail(state?.id ?? null, against);
  const restore = detail.data?.restore;
  const source = state ? SOURCES[state.source] : null;

  return (
    <Drawer opened={!!state} onClose={onClose} position="right" size="xl" title={state ? <Text fw={700}>{state.reason}</Text> : null}>
      {state && source && (
        <Stack gap="md">
          <Group gap="xs">
            <Badge variant="light" color={source.color}>
              {source.label}
            </Badge>
            <Text size="sm" c="dimmed">
              {fmtDateTime(state.at)} · {ageOf(state.at)} ago
            </Text>
            <Text size="sm" c="dimmed" className="mono">
              {state.code}
            </Text>
          </Group>
          {!state.valid && (
            <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />} title="This state does not load">
              <Text size="sm">{state.error}</Text>
              <Text size="sm" mt={4}>
                It is kept so you can see what the broken edit was. It cannot be restored.
              </Text>
            </Alert>
          )}
          <SegmentedControl
            value={against}
            onChange={(value) => setAgainst(value as "previous" | "current")}
            data={[
              { value: "previous", label: "What it changed" },
              { value: "current", label: "What restoring it would change" },
            ]}
          />
          {detail.isLoading ? (
            <Group justify="center" py="lg">
              <Loader size="sm" />
            </Group>
          ) : detail.isError ? (
            <Alert color="red" variant="light">
              {String((detail.error as Error).message)}
            </Alert>
          ) : detail.data ? (
            <>
              {against === "current" && restore && !isPlan(restore) && (
                <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />}>
                  {restore.refused}
                </Alert>
              )}
              {against === "current" && isPlan(restore) && (
                <Stack gap={4}>
                  {restore.left_in_place.length > 0 && (
                    <Text size="sm" c="dimmed">
                      Left as they are (not part of that state): {restore.left_in_place.map(fileName).join(", ")}
                    </Text>
                  )}
                  {restore.unavailable.map((item) => (
                    <Text key={item.path} size="sm" c="dimmed">
                      {fileName(item.path)} stays as it is now — {item.why}.
                    </Text>
                  ))}
                  {restore.migrated && (
                    <Text size="sm" c="dimmed">
                      Its config.yaml is from an older version of sshfleet and is upgraded on the way back.
                    </Text>
                  )}
                </Stack>
              )}
              <DiffList
                diffs={detail.data.diffs}
                emptyText={against === "current" ? "The files already hold this state — restoring it would change nothing." : "Nothing differs from the state before it."}
              />
            </>
          ) : null}
          <Divider />
          <Group justify="space-between">
            <Text size="xs" c="dimmed" maw={420}>
              {latest
                ? "This is the newest state: the files as they are now."
                : "Restoring writes only the files that differ, keeps a copy of each, and records the state it replaces — so it can be undone."}
            </Text>
            <Button leftSection={<IconArrowBackUp size={15} />} disabled={latest || !state.valid} onClick={onRestore}>
              Restore this state…
            </Button>
          </Group>
        </Stack>
      )}
    </Drawer>
  );
}

function RestoreModal({ state, onClose }: { state: HistoryState | null; onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const [skip, setSkip] = useState<Set<string>>(new Set());
  const plan = useHistoryDetail(state?.id ?? null, "current");
  const restore = useRestore();
  useEffect(() => {
    setTyped("");
    setSkip(new Set());
  }, [state?.id]);
  const summary = plan.data?.restore;
  const writes = isPlan(summary) ? summary.writes : [];
  const chosen = writes.filter((path) => !skip.has(path));

  return (
    <Modal opened={!!state} onClose={onClose} title={state ? `Restore the state of ${fmtDateTime(state.at)}?` : ""} size="lg" centered>
      {state && (
        <Stack gap="sm">
          <Text size="sm">{state.reason}</Text>
          {plan.isLoading ? (
            <Loader size="sm" />
          ) : summary && !isPlan(summary) ? (
            <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />}>
              {summary.refused}
            </Alert>
          ) : writes.length === 0 ? (
            <Alert color="gray" variant="light">
              The files already hold this state. There is nothing to write.
            </Alert>
          ) : (
            <>
              <Text size="sm" c="dimmed">
                These files will be written. Each keeps a copy first, and the state they are in now is recorded, so this can be undone.
              </Text>
              <Stack gap={4}>
                {writes.map((path) => (
                  <Checkbox
                    key={path}
                    size="sm"
                    checked={!skip.has(path)}
                    onChange={() =>
                      setSkip((current) => {
                        const next = new Set(current);
                        if (next.has(path)) next.delete(path);
                        else next.add(path);
                        return next;
                      })
                    }
                    label={<PathLabel path={path} />}
                    styles={{ body: { minWidth: 0 }, labelWrapper: { minWidth: 0 } }}
                  />
                ))}
              </Stack>
              <Alert color="yellow" variant="light">
                Type{" "}
                <Text span className="mono" fw={700}>
                  {state.code}
                </Text>{" "}
                to confirm.
              </Alert>
              <TextInput value={typed} onChange={(e) => setTyped(e.currentTarget.value)} placeholder={state.code} className="mono" autoFocus />
            </>
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button
              leftSection={<IconArrowBackUp size={15} />}
              disabled={typed.trim() !== state.code || chosen.length === 0}
              loading={restore.isPending}
              onClick={() =>
                restore.mutate(
                  { id: state.id, code: state.code, files: chosen.length < writes.length ? chosen : undefined },
                  { onSuccess: onClose },
                )
              }
            >
              Restore {chosen.length < writes.length ? `${chosen.length} of ${writes.length} files` : ""}
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}

export default function HistoryTab() {
  const [filter, setFilter] = useViewState("settings.history.filter", "all");
  const [limit, setLimit] = usePageLimit("settings.history.limit", filter, PAGE);
  const history = useHistory(limit);
  const [viewing, setViewing] = useState<HistoryState | null>(null);
  const [restoring, setRestoring] = useState<HistoryState | null>(null);

  const states = history.data?.states ?? [];
  const latestId = states[0]?.id;
  const rows = useMemo(() => states.filter((s) => matches(filter, s.source)), [states, filter]);
  const total = history.data?.total ?? 0;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-start">
        <Text size="sm" c="dimmed" maw={680}>
          Every state your configuration has been in: <Text span className="mono">config.yaml</Text> and the SSH files it names. A new one is
          kept whenever they change — from this page, a command, or an editor. Any of them can be put back, and the state you leave is
          kept too, so going back can be undone.
        </Text>
        <SegmentedControl size="xs" value={filter} onChange={setFilter} data={FILTERS} />
      </Group>

      {history.data?.busy && (
        <Alert color="blue" variant="light" icon={<Loader size={14} />}>
          A restore or an import is writing the configuration right now.
        </Alert>
      )}

      <Card withBorder padding={0}>
        {history.isLoading ? (
          <Group justify="center" py={48}>
            <Loader />
          </Group>
        ) : rows.length === 0 ? (
          <EmptyState title={states.length ? "Nothing of this kind in the loaded states" : "No history yet"} hint="States appear here as the configuration changes." />
        ) : (
          <Table.ScrollContainer minWidth={860}>
            <Table highlightOnHover verticalSpacing={8}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th w={150}>When</Table.Th>
                  <Table.Th w={120}>By</Table.Th>
                  <Table.Th>What</Table.Th>
                  <Table.Th>Changed</Table.Th>
                  <Table.Th w={180} />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {rows.map((state) => {
                  const source = SOURCES[state.source] ?? SOURCES.cli;
                  const latest = state.id === latestId;
                  return (
                    <Table.Tr key={state.id} className="host-row" onClick={() => setViewing(state)}>
                      <Table.Td>
                        <Tooltip label={fmtDateTime(state.at)} openDelay={300}>
                          <Text size="sm" style={{ whiteSpace: "nowrap" }}>
                            {ageOf(state.at)} ago
                          </Text>
                        </Tooltip>
                      </Table.Td>
                      <Table.Td>
                        <Tooltip label={source.hint} openDelay={300}>
                          <Badge size="sm" variant="light" color={source.color}>
                            {source.label}
                          </Badge>
                        </Tooltip>
                      </Table.Td>
                      <Table.Td>
                        <Group gap={6} wrap="nowrap">
                          <Text size="sm">{state.reason}</Text>
                          {latest && (
                            <Badge size="xs" variant="outline" color="teal">
                              now
                            </Badge>
                          )}
                          {!state.valid && (
                            <Tooltip label={state.error} multiline maw={360}>
                              <Badge size="xs" color="red" variant="light">
                                does not load
                              </Badge>
                            </Tooltip>
                          )}
                        </Group>
                      </Table.Td>
                      <Table.Td>
                        <ChangeChips state={state} />
                      </Table.Td>
                      <Table.Td onClick={(event) => event.stopPropagation()}>
                        <Group gap={4} justify="flex-end" wrap="nowrap">
                          <Button size="compact-sm" variant="subtle" leftSection={<IconEye size={14} />} onClick={() => setViewing(state)}>
                            View
                          </Button>
                          <Tooltip label={latest ? "This is the state the files are in now" : !state.valid ? "This state does not load" : "Put this state back"}>
                            <Button
                              size="compact-sm"
                              variant="light"
                              leftSection={<IconArrowBackUp size={14} />}
                              disabled={latest || !state.valid}
                              onClick={() => setRestoring(state)}
                            >
                              Restore
                            </Button>
                          </Tooltip>
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
        {states.length > 0 && (
          <>
            <Divider />
            <Group justify="space-between" px="md" py="xs">
              <Text size="xs" c="dimmed">
                <IconHistory size={12} style={{ verticalAlign: -1 }} /> {states.length} of {total} states
                {filter !== "all" ? " — the filter applies to the loaded ones" : ""}
              </Text>
              {total > states.length && (
                <Button size="compact-sm" variant="subtle" loading={history.isFetching} onClick={() => setLimit((current) => current + PAGE)}>
                  Show {Math.min(total - states.length, PAGE)} older
                </Button>
              )}
            </Group>
          </>
        )}
      </Card>

      <StateDrawer
        state={viewing}
        latest={viewing?.id === latestId}
        onClose={() => setViewing(null)}
        onRestore={() => {
          setRestoring(viewing);
          setViewing(null);
        }}
      />
      <RestoreModal state={restoring} onClose={() => setRestoring(null)} />
    </Stack>
  );
}
