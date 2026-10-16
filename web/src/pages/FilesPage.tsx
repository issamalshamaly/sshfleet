import {
  ActionIcon,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Card,
  Code,
  Divider,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Select,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { Dropzone } from "@mantine/dropzone";
import { notifications } from "@mantine/notifications";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer, type VirtualItem } from "@tanstack/react-virtual";
import { IconDownload, IconEye, IconFile, IconFolder, IconLock, IconRefresh, IconUpload } from "@tabler/icons-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { FileEntry } from "../api/types";
import { EmptyState, EnvBadge } from "../components/common";
import { useConfig, useFleet } from "../hooks/useFleet";
import { fmtBytes, fmtDateTime } from "../lib/format";
import { useEnv } from "../state/env";
import { useViewState, useViewStore } from "../state/views";

/** A directory can hold thousands of files, so rows are virtualised: only what is on screen exists.
 *  The parent link rides in the same list so it scrolls and measures like any other row. */
type Row = { kind: "up" } | { kind: "entry"; entry: FileEntry };

export default function FilesPage() {
  const { inScope } = useEnv();
  const config = useConfig();
  const fleet = useFleet();
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();

  const hosts = useMemo(() => (fleet.data?.hosts ?? []).filter((h) => inScope(h.env)), [fleet.data, inScope]);
  // Nothing is selected on a first arrival: landing on this page must not connect to a server by
  // itself, least of all to whichever host happens to sort first. A return is different — the
  // sidebar brings back the address you left, host and directory, because you chose them.
  const alias = params.get("host");
  const host = hosts.find((h) => h.alias === alias) ?? fleet.data?.hosts.find((h) => h.alias === alias) ?? null;
  const [path, setPath] = useState(params.get("path") ?? "");
  const [pathInput, setPathInput] = useState(path);
  const [hidden, setHidden] = useViewState("files.hidden", false);
  const [preview, setPreview] = useState<{ name: string; text: string } | null>(null);
  const [pendingUpload, setPendingUpload] = useState<File[] | null>(null);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    setPathInput(path);
  }, [path]);

  const listing = useQuery({
    queryKey: ["files", alias, path, hidden],
    queryFn: () => api.files.ls(alias!, path, hidden),
    enabled: !!alias,
    retry: false,
  });

  // The server resolves an empty path to the home directory. The address follows it, so the
  // directory you were in is the one a return lands in.
  useEffect(() => {
    if (!listing.data || listing.data.path === path) return;
    setPath(listing.data.path);
    params.set("path", listing.data.path);
    setParams(params, { replace: true });
  }, [listing.data, path, params, setParams]);

  const entries = listing.data?.entries;
  const rows = useMemo<Row[]>(() => {
    const list: Row[] = (entries ?? []).map((entry) => ({ kind: "entry", entry }) as Row);
    return path !== "/" && path !== "" ? [{ kind: "up" }, ...list] : list;
  }, [entries, path]);

  // How far down the listing you were, kept for that one directory: a long /var/log is read
  // from where you left it, and any other directory still opens at its top. The row heights
  // measured so far go with the offset — folders and files are not the same height, so the
  // offset alone, laid over estimated rows, lands a few rows off.
  const views = useViewStore();
  const place = JSON.stringify([alias, path, hidden]);
  const [kept] = useState(() => {
    const held = views.get<{ place: string; offset: number; measured: VirtualItem[] }>("files.scroll");
    return held?.place === place ? held : { place, offset: 0, measured: [] };
  });
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 35,
    overscan: 12,
    initialOffset: kept.offset,
    initialMeasurementsCache: kept.measured,
    measureElement: (el) => el.getBoundingClientRect().height,
  });
  const items = virtualizer.getVirtualItems();
  const padTop = items.length ? items[0].start : 0;
  const padBottom = items.length ? virtualizer.getTotalSize() - items[items.length - 1].end : 0;

  // Where the list should be once it is drawn: where you left it, on a return; the top, for a
  // directory just opened — not where the last one was. Either has to wait for rows: the
  // virtualizer draws none until it has measured its box, the browser clamps an offset set on a
  // table that is only its header to 0, and a directory that had to load arrives in a new
  // element, which the virtualizer scrolls straight back to the offset it remembers.
  const target = useRef<number | null>(kept.offset);
  const shownPlace = useRef(place);
  useLayoutEffect(() => {
    if (shownPlace.current !== place) {
      shownPlace.current = place;
      target.current = 0;
    }
    const element = scrollRef.current;
    if (target.current === null || !element || !items.length) return;
    element.scrollTop = target.current;
    target.current = null;
  }, [place, items.length, entries]);
  // Written once, on the way out: scrolling does not need to pay for it.
  useEffect(
    () => () => views.set("files.scroll", { place: shownPlace.current, offset: virtualizer.scrollOffset ?? 0, measured: virtualizer.takeSnapshot() }),
    [views, virtualizer],
  );

  const selectHost = (next: string | null) => {
    if (!next) return;
    params.set("host", next);
    params.delete("path");
    setParams(params);
    setPath("");
  };
  const go = (next: string) => {
    setPath(next);
    params.set("path", next);
    setParams(params, { replace: true });
  };

  const crumbs = useMemo(() => {
    const parts = path.split("/").filter(Boolean);
    return parts.map((part, index) => ({ label: part, target: "/" + parts.slice(0, index + 1).join("/") }));
  }, [path]);

  const openPreview = async (entry: FileEntry) => {
    if (!alias) return;
    try {
      const text = await api.files.preview(alias, `${path}/${entry.name}`);
      setPreview({ name: entry.name, text });
    } catch (error) {
      notifications.show({ title: "preview", message: String((error as Error).message), color: "red" });
    }
  };

  const doUpload = async () => {
    if (!alias || !pendingUpload) return;
    setUploading(true);
    try {
      for (const file of pendingUpload) {
        const result = await api.files.upload(alias, path, file);
        notifications.show({ title: "uploaded", message: `${file.name} → ${result.destination} (${result.method})`, color: "teal" });
      }
      void client.invalidateQueries({ queryKey: ["files", alias] });
    } catch (error) {
      notifications.show({ title: "upload failed", message: String((error as Error).message), color: "red" });
    } finally {
      setUploading(false);
      setPendingUpload(null);
    }
  };

  const production = config.data?.environments.find((e) => e.name === host?.env)?.production ?? false;

  return (
    <Stack gap="md" style={{ flex: 1 }}>
      <Group align="flex-end" gap="sm" wrap="wrap">
        <Select
          label="Host"
          searchable
          w={300}
          data={hosts.map((h) => ({ value: h.alias, label: h.alias }))}
          value={alias}
          onChange={selectHost}
          placeholder="Choose a server…"
          allowDeselect={false}
          nothingFoundMessage="No host"
        />
        <TextInput
          label="Path"
          w={420}
          className="mono"
          value={pathInput}
          onChange={(e) => setPathInput(e.currentTarget.value)}
          onKeyDown={(e) => e.key === "Enter" && go(pathInput.trim() || "/")}
          placeholder="/var/log"
        />
        <Switch label="Hidden files" checked={hidden} onChange={(e) => setHidden(e.currentTarget.checked)} mb={8} />
        <Tooltip label="Reload">
          <ActionIcon variant="default" size="lg" mb={2} onClick={() => void listing.refetch()}>
            <IconRefresh size={16} />
          </ActionIcon>
        </Tooltip>
        {host && (
          <Group gap="xs" mb={8} ml="auto">
            <EnvBadge env={host.env} environments={config.data?.environments} />
            <Text size="sm" c="dimmed" className="mono">
              {host.user}@{host.hostname}
            </Text>
            {listing.data?.used_sudo && (
              <Badge size="xs" variant="outline" color="orange">
                read with sudo
              </Badge>
            )}
          </Group>
        )}
      </Group>

      {alias && (
      <Breadcrumbs separatorMargin={4}>
        <Anchor size="sm" onClick={() => go("/")} className="mono">
          /
        </Anchor>
        {crumbs.map((crumb) => (
          <Anchor key={crumb.target} size="sm" onClick={() => go(crumb.target)} className="mono">
            {crumb.label}
          </Anchor>
        ))}
      </Breadcrumbs>
      )}

      <Card withBorder padding={0}>
        {!alias ? (
          <EmptyState title="Choose a server" hint="Pick a host above to browse its files. Nothing is read until you do." />
        ) : listing.isLoading ? (
          <Group justify="center" py={48}>
            <Loader />
          </Group>
        ) : listing.isError ? (
          <EmptyState title="Could not list this directory" hint={String((listing.error as Error).message)} />
        ) : !rows.length ? (
          <EmptyState title="Empty directory" />
        ) : (
          <>
            <div ref={scrollRef} style={{ overflowY: "auto", maxHeight: "calc(100vh - 400px)", minHeight: 220 }}>
              <Table highlightOnHover verticalSpacing={6} stickyHeader style={{ tableLayout: "fixed" }}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Name</Table.Th>
                    <Table.Th w={100} ta="right">
                      Size
                    </Table.Th>
                    <Table.Th w={170}>Modified</Table.Th>
                    <Table.Th w={110}>Mode</Table.Th>
                    <Table.Th w={150}>Owner</Table.Th>
                    <Table.Th w={90} />
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {padTop > 0 && (
                    <Table.Tr style={{ height: padTop }}>
                      <Table.Td colSpan={6} p={0} style={{ border: "none" }} />
                    </Table.Tr>
                  )}
                  {items.map((item) => {
                    const row = rows[item.index];
                    if (row.kind === "up") {
                      return (
                        <Table.Tr
                          key="up"
                          data-index={item.index}
                          ref={virtualizer.measureElement}
                          className="host-row"
                          onClick={() => go(path.replace(/\/[^/]+\/?$/, "") || "/")}
                        >
                          <Table.Td colSpan={6}>
                            <Group gap={8}>
                              <IconFolder size={16} style={{ opacity: 0.7 }} />
                              <Text size="sm" className="mono" c="dimmed">
                                ..
                              </Text>
                            </Group>
                          </Table.Td>
                        </Table.Tr>
                      );
                    }
                    const entry = row.entry;
                    const isDir = entry.kind === "d";
                    const denied = !!entry.denied;
                    return (
                      <Table.Tr
                        key={entry.name}
                        data-index={item.index}
                        ref={virtualizer.measureElement}
                        className={isDir ? "host-row" : undefined}
                        onClick={isDir ? () => go(`${path.replace(/\/$/, "")}/${entry.name}`) : undefined}
                        style={denied ? { opacity: 0.6 } : undefined}
                      >
                        <Table.Td>
                          <Group gap={8} wrap="nowrap">
                            {denied ? <IconLock size={16} color="var(--mantine-color-red-5)" /> : isDir ? <IconFolder size={16} color="var(--mantine-color-blue-4)" /> : <IconFile size={16} style={{ opacity: 0.6 }} />}
                            <Text size="sm" className="mono" fw={isDir ? 600 : 400} truncate>
                              {entry.name}
                              {entry.kind === "l" && entry.link ? (
                                <Text span c="dimmed">
                                  {" "}
                                  → {entry.link}
                                </Text>
                              ) : null}
                            </Text>
                            {denied && (
                              <Tooltip label={entry.denied}>
                                <Badge size="xs" color="red" variant="light">
                                  protected
                                </Badge>
                              </Tooltip>
                            )}
                          </Group>
                        </Table.Td>
                        <Table.Td ta="right">
                          <Text size="sm" className="mono" c="dimmed">
                            {isDir ? "" : fmtBytes(entry.size)}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" c="dimmed">
                            {fmtDateTime(entry.mtime * 1000)}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <Text size="xs" className="mono" c="dimmed">
                            {entry.mode}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <Text size="xs" className="mono" c="dimmed" truncate>
                            {entry.owner}:{entry.group}
                          </Text>
                        </Table.Td>
                        <Table.Td onClick={(e) => e.stopPropagation()}>
                          {!isDir && !denied && (
                            <Group gap={2} justify="flex-end" wrap="nowrap">
                              {entry.size <= 1_000_000 && (
                                <Tooltip label="Preview">
                                  <ActionIcon variant="subtle" color="gray" onClick={() => void openPreview(entry)}>
                                    <IconEye size={16} />
                                  </ActionIcon>
                                </Tooltip>
                              )}
                              <Tooltip label="Download">
                                <ActionIcon variant="subtle" color="gray" component="a" href={api.files.downloadUrl(alias!, `${path}/${entry.name}`)}>
                                  <IconDownload size={16} />
                                </ActionIcon>
                              </Tooltip>
                            </Group>
                          )}
                        </Table.Td>
                      </Table.Tr>
                    );
                  })}
                  {padBottom > 0 && (
                    <Table.Tr style={{ height: padBottom }}>
                      <Table.Td colSpan={6} p={0} style={{ border: "none" }} />
                    </Table.Tr>
                  )}
                </Table.Tbody>
              </Table>
            </div>
            <Divider />
            <Text size="xs" c="dimmed" px="md" py={6}>
              {entries?.length ?? 0} {entries?.length === 1 ? "entry" : "entries"}
              {hidden ? "" : " · hidden files not shown"}
            </Text>
          </>
        )}
      </Card>

      {alias && (
        <Dropzone onDrop={(files) => setPendingUpload(files)} multiple maxSize={2 * 1024 ** 3} styles={{ root: { padding: 18 } }}>
          <Group justify="center" gap="sm">
            <IconUpload size={22} style={{ opacity: 0.6 }} />
            <div>
              <Text size="sm" fw={600}>
                Drop files here to upload to <Text span className="mono">{path}</Text>
              </Text>
              <Text size="xs" c="dimmed">
                You will be asked to confirm before anything is written to the server.
              </Text>
            </div>
          </Group>
        </Dropzone>
      )}

      <Modal opened={pendingUpload !== null} onClose={() => setPendingUpload(null)} title="Confirm upload" size="md">
        {pendingUpload && host && (
          <Stack gap="sm">
            <Group gap="xs">
              <Text size="sm" c="dimmed">
                Environment
              </Text>
              <Badge color={production ? "red" : "yellow"} variant={production ? "filled" : "light"} tt="uppercase">
                {host.env}
              </Badge>
            </Group>
            <Text size="sm">
              Upload {pendingUpload.length} file{pendingUpload.length > 1 ? "s" : ""} to <Text span className="mono">{alias}:{path}</Text>
            </Text>
            <Stack gap={2}>
              {pendingUpload.map((f) => (
                <Text key={f.name} size="xs" className="mono" c="dimmed">
                  {f.name} · {fmtBytes(f.size)}
                </Text>
              ))}
            </Stack>
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setPendingUpload(null)} disabled={uploading}>
                Cancel
              </Button>
              <Button color={production ? "red" : "teal"} onClick={doUpload} loading={uploading} leftSection={<IconUpload size={16} />}>
                Upload
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>

      <Modal opened={preview !== null} onClose={() => setPreview(null)} title={preview?.name} size="xl">
        <ScrollArea h="70vh">
          <Code block style={{ fontSize: 12 }}>
            {preview?.text}
          </Code>
        </ScrollArea>
      </Modal>
    </Stack>
  );
}
