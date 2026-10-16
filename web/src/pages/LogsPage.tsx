import {
  ActionIcon,
  Badge,
  Button,
  Card,
  Checkbox,
  Divider,
  Drawer,
  Group,
  Loader,
  NumberInput,
  ScrollArea,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Text,
  Tooltip,
} from "@mantine/core";
import { DateTimePicker } from "@mantine/dates";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  IconArchive,
  IconDownload,
  IconPlayerPause,
  IconPlayerPlay,
  IconSearch,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { LogSelection, LogSourceInfo } from "../api/types";
import { EmptyState, SearchInput } from "../components/common";
import { useConfig } from "../hooks/useFleet";
import { fmtBytes, fmtDateTime, fmtLogTime } from "../lib/format";
import { useEnv } from "../state/env";
import { seedFromParams, useLogsEngine, useLogsStore, type LogsSnapshot } from "../state/logs";
import { envColor } from "../theme";

const PALETTE = ["cyan", "grape", "teal", "yellow", "blue", "pink", "lime", "orange", "violet", "indigo", "green", "red"];
const LINE_CAPS = [
  { label: "2k", value: 2000 },
  { label: "20k", value: 20000 },
  { label: "50k", value: 50000 },
  { label: "200k", value: 200000 },
];
const PRESETS = [
  { value: "5m", label: "Last 5 minutes" },
  { value: "15m", label: "Last 15 minutes" },
  { value: "1h", label: "Last hour" },
  { value: "6h", label: "Last 6 hours" },
  { value: "24h", label: "Last 24 hours" },
  { value: "custom", label: "Custom range…" },
];

function labelColor(label: string, labels: string[]): string {
  const index = labels.indexOf(label);
  return PALETTE[(index === -1 ? 0 : index) % PALETTE.length];
}

function buildHighlighter(pattern: string, mode: "regex" | "fixed", ignoreCase: boolean): RegExp | null {
  if (!pattern.trim()) return null;
  try {
    const source = mode === "fixed" ? pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : pattern;
    return new RegExp(`(${source})`, ignoreCase ? "gi" : "g");
  } catch {
    return null;
  }
}

function Highlighted({ text, regex }: { text: string; regex: RegExp | null }) {
  if (!regex) return <>{text}</>;
  const parts = text.split(regex);
  if (parts.length === 1) return <>{text}</>;
  return (
    <>
      {parts.map((part, index) => (index % 2 === 1 ? <mark key={index}>{part}</mark> : <span key={index}>{part}</span>))}
    </>
  );
}

/** `fresh` comes from the route: true when a Logs icon elsewhere in the app opened this page,
 *  which starts a new search instead of resuming the last one. See lib/navigation. */
export default function LogsPage({ fresh = false }: { fresh?: boolean }) {
  const { env: globalEnv, envNames, setEnv: setGlobalEnv } = useEnv();
  const config = useConfig();
  const [params] = useSearchParams();
  // Taken once, on the way in: the page either resumes what it was left with or starts from
  // the defaults plus whatever the link asked for. Nothing below re-reads this.
  const store = useLogsStore();
  const [initial] = useState<LogsSnapshot>(() => (fresh ? seedFromParams(params) : store.get()));
  // A search spans one environment, and only the ones inside the project being looked at.
  const environments = (config.data?.environments ?? []).filter((e) => envNames.includes(e.name));

  // ------------------------------------------------------------- selection
  // The sidebar is the one environment control in the app. A link that names an environment —
  // the fleet's "logs" button does — moves the sidebar to it rather than disagreeing with it.
  const linked = params.get("env");
  useEffect(() => {
    if (linked && linked !== globalEnv && envNames.includes(linked)) setGlobalEnv(linked);
  }, [linked, globalEnv, envNames, setGlobalEnv]);

  // A search reads one environment at a time, so when the sidebar is on all of them this page
  // still has to pick which. That is the only case where it asks.
  const [pick, setPick] = useState<string | null>(initial.pick);
  const env = globalEnv ?? (pick && envNames.includes(pick) ? pick : (environments[0]?.name ?? null));
  const resetSelection = () => {
    setPicked(new Set());
    setExcluded(new Set());
  };

  const sources = useQuery({
    queryKey: ["log-sources", env],
    queryFn: () => api.logs.sources(env!),
    enabled: !!env,
    staleTime: 30_000,
  });

  const [mode, setMode] = useState<"main" | "custom">(initial.mode);
  const [picked, setPicked] = useState<Set<string>>(() => new Set(initial.picked));
  const [hostFilter, setHostFilter] = useState(initial.hostFilter);
  // The starred hosts are a subtractive list: every one is in unless you click it out. Keyed by
  // host alias, not by source id, so it survives the sources list being refetched.
  const [excluded, setExcluded] = useState<Set<string>>(() => new Set(initial.excluded));

  const mainSources = useMemo(() => sources.data?.main ?? [], [sources.data]);
  const keptMain = useMemo(() => mainSources.filter((s) => !excluded.has(s.host ?? s.id)), [mainSources, excluded]);

  const selection: LogSelection = useMemo(() => {
    if (mode === "main") {
      // With nothing excluded we still send "main" so the server owns the list; only once a
      // host is clicked out do we spell the remaining ones out by alias.
      if (!excluded.size) return { main: true };
      return { starred_hosts: keptMain.map((s) => s.host).filter((name): name is string => !!name) };
    }
    const out: LogSelection = { hosts: [], containers: [], files: [] };
    for (const key of picked) {
      const [kind, ...rest] = key.split(":");
      if (kind === "h") out.hosts!.push(rest.join(":"));
      else if (kind === "c") {
        const [host, ...name] = rest;
        out.containers!.push({ host, name: name.join(":") });
      } else if (kind === "f") {
        // The source id, not the path: a journal has no path, and an in-container file read
        // from its path alone would be looked for on the host instead.
        const [host, ...id] = rest;
        out.files!.push({ host, id: id.join(":") });
      }
    }
    return out;
  }, [mode, picked, excluded, keptMain]);

  // ----------------------------------------------------------------- query
  const [pattern, setPattern] = useState(initial.pattern);
  const [matchMode, setMatchMode] = useState<"regex" | "fixed">(initial.matchMode);
  const [caseSensitive, setCaseSensitive] = useState(initial.caseSensitive);
  const [before, setBefore] = useState<number>(initial.before);
  // useState keeps its first argument, and on first render the config query has not resolved,
  // so the configured default would never be seen. Apply it once, and never again: after that
  // the field belongs to whoever is typing in it.
  // A resumed page already carries whatever was in the field, so the default is not re-applied.
  const beforeFromConfig = useRef(initial.restored);
  useEffect(() => {
    const configured = config.data?.logs.default_before;
    if (configured === undefined || beforeFromConfig.current) return;
    beforeFromConfig.current = true;
    setBefore(configured);
  }, [config.data?.logs.default_before]);
  const [after, setAfter] = useState<number>(initial.after);
  const [preset, setPreset] = useState(initial.preset);
  // A busy workload can write hundreds of thousands of lines in an hour. These caps are per
  // source and keep the newest data, so a wide window stays fast instead of dragging the
  // whole hour across the wire.
  const [maxLines, setMaxLines] = useState<number>(initial.maxLines);
  const [maxMb, setMaxMb] = useState<number>(initial.maxMb);
  const [since, setSince] = useState<Date | null>(initial.since);
  const [until, setUntil] = useState<Date | null>(initial.until);
  // Always UTC on the wire, whatever the display zone: the server reads an offset, not a guess.
  const window = preset === "custom" ? (since ? `${since.toISOString()}..${until ? until.toISOString() : "now"}` : null) : preset;

  // --------------------------------------------------------------- results
  // The search and what it has found live in the provider, not here: they go on while you are
  // on another page and are still there when you come back. This page only shows them.
  const engine = useLogsEngine();
  const { lines, sourceStates, summary, running, stop, reset } = engine;
  const [wrap, setWrap] = useState(initial.wrap);
  const [matchesOnly, setMatchesOnly] = useState(initial.matchesOnly);
  const [useUtc, setUseUtc] = useState(initial.useUtc);
  const [bundlesOpen, setBundlesOpen] = useState(false);

  // A new question: whatever the last search found is not the answer to it. Before the first
  // paint, so the old results are never seen under the new form.
  useLayoutEffect(() => {
    if (fresh) reset();
  }, [fresh, reset]);

  const tz = useUtc ? "UTC" : config.data?.logs.display_tz ?? "UTC";
  const labels = useMemo(() => Array.from(new Set(Object.values(sourceStates).map((s) => s.label))).sort(), [sourceStates]);
  const highlighter = useMemo(() => buildHighlighter(pattern, matchMode, !caseSensitive), [pattern, matchMode, caseSensitive]);
  // "Matches only" drops the -B/-A context, not the match. A pino entry is a header plus its
  // indented body, and grep marks whichever of those lines the pattern was on — usually the
  // body. Filtering to `match` alone would therefore hide the header, and filtering to headers
  // alone would hide every match that landed inside an object. So the unit is the entry: keep
  // every line of an entry that contains a match anywhere in it.
  const visible = useMemo(() => {
    if (!matchesOnly) return lines;
    const hit = new Set<string>();
    for (const line of lines) if (line.match) hit.add(`${line.source}\u0000${line.entry}`);
    if (!hit.size) return [];
    return lines.filter((line) => hit.has(`${line.source}\u0000${line.entry}`));
  }, [lines, matchesOnly]);

  const run = (download = false) => {
    if (!env || window === null) return;
    void engine.search({
      env,
      selection,
      pattern,
      mode: matchMode,
      ignore_case: !caseSensitive,
      before,
      after,
      window,
      tz: config.data?.logs.display_tz,
      tail_lines: maxLines,
      max_bytes: maxMb * 1024 * 1024,
      download,
    });
  };

  const startFollow = () => {
    if (!env) return;
    void engine.follow({ env, selection, pattern, since: "1m" });
  };

  // Everything worth resuming, kept in a ref so the value at the moment the page goes away is
  // the value that is saved. One write, on the way out — see state/logs for why. The scroll
  // position is tracked beside it: scrolling does not render, so it cannot go through state.
  const scrollTop = useRef(initial.scrollTop);
  const latest = useRef<LogsSnapshot>(initial);
  latest.current = {
    pick,
    mode,
    picked,
    excluded,
    hostFilter,
    pattern,
    matchMode,
    caseSensitive,
    before,
    after,
    preset,
    maxLines,
    maxMb,
    since,
    until,
    wrap,
    matchesOnly,
    useUtc,
    scrollTop: scrollTop.current,
    restored: true,
  };

  useEffect(
    () => () => {
      // Leaving does not stop the search: it belongs to the provider and goes on without
      // this page, so what it finds in the meantime is here on the way back.
      store.save({ ...latest.current, scrollTop: scrollTop.current });
    },
    [store],
  );

  // ---------------------------------------------------------- virtual list
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 20,
    overscan: 30,
    // So the first render already draws the lines you were reading, not the top of the list.
    initialOffset: initial.scrollTop,
    measureElement: wrap ? (el) => el.getBoundingClientRect().height : undefined,
  });
  // Back to where you were, before the first paint. With wrapping on the heights were measured
  // and are measured again now, so the place is close rather than exact.
  useLayoutEffect(() => {
    if (initial.scrollTop && scrollRef.current) scrollRef.current.scrollTop = initial.scrollTop;
  }, [initial.scrollTop]);
  useEffect(() => {
    if (running === "follow" && stickToBottom.current && visible.length) {
      virtualizer.scrollToIndex(visible.length - 1, { align: "end" });
    }
  }, [visible.length, running, virtualizer]);
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    scrollTop.current = el.scrollTop;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  const totalMatches = Object.values(sourceStates).reduce((n, s) => n + s.matches, 0);
  const anyRunning = Object.values(sourceStates).some((s) => s.state === "running");
  const selectedCount = mode === "main" ? keptMain.length : picked.size;

  const toggle = (key: string) =>
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const toggleExcluded = (alias: string) =>
    setExcluded((current) => {
      const next = new Set(current);
      if (next.has(alias)) next.delete(alias);
      else next.add(alias);
      return next;
    });

  // Why the actions are off, in the words the operator needs rather than a dead grey button.
  // Follow tails from now and never reads the window, so an unfinished custom range stops a
  // search without stopping it too.
  const hasSources = Boolean(env) && selectedCount > 0;
  const ready = hasSources && window !== null;
  const pickReason = !env
    ? "Choose an environment first"
    : mode === "main"
      ? "Every host is excluded — click one to put it back"
      : "Pick at least one source above";
  const reason = hasSources && window === null ? "A custom range needs a start time" : pickReason;

  const filteredHosts = (sources.data?.hosts ?? []).filter((h) => !hostFilter || h.alias.toLowerCase().includes(hostFilter.toLowerCase()));

  return (
    // Full height, nothing taller: the query column and the results scroll separately, so
    // neither the sources you picked nor the match count ever scrolls out of reach.
    <Group align="stretch" gap="md" wrap="nowrap" style={{ flex: 1, minHeight: 0, height: "100%" }}>
      {/* ------------------------------------------------------- left column */}
      <Stack gap="md" w={370} style={{ flexShrink: 0, minHeight: 0 }}>
        {/* Takes the height it needs and no more, so on a tall screen the buttons sit right
            under the query instead of miles below it; on a short one it shrinks and scrolls. */}
        <Stack gap="md" style={{ flex: "0 1 auto", minHeight: 0, overflowY: "auto", paddingRight: 6, paddingBottom: 6 }}>
        <Card withBorder padding="md" style={{ flexShrink: 0 }}>
          <Stack gap="sm">
            <Group justify="space-between">
              <Text fw={600}>Sources</Text>
              <Badge variant="light" color="gray">
                {selectedCount} selected
              </Badge>
            </Group>
            {globalEnv ? (
              <Group gap={6} wrap="nowrap">
                <Text size="xs" c="dimmed">
                  Environment
                </Text>
                <Badge size="sm" variant="light" color={envColor(environments.find((e) => e.name === globalEnv)?.color ?? "gray")}>
                  {globalEnv}
                </Badge>
                <Text size="xs" c="dimmed">
                  · chosen in the sidebar
                </Text>
              </Group>
            ) : (
              <Select
                size="sm"
                label="Environment"
                description="A search reads one at a time"
                data={environments.map((e) => ({ value: e.name, label: e.name }))}
                value={env}
                onChange={(v) => {
                  setPick(v);
                  resetSelection();
                }}
                allowDeselect={false}
              />
            )}
            <SegmentedControl
              size="xs"
              fullWidth
              value={mode}
              onChange={(v) => setMode(v as "main" | "custom")}
              data={[
                { value: "main", label: "Starred hosts" },
                { value: "custom", label: "Pick hosts & containers" },
              ]}
            />
            {mode === "main" ? (
              <Stack gap={6}>
                <Group gap={4}>
                  {mainSources.map((s) => {
                    const key = s.host ?? s.id;
                    const off = excluded.has(key);
                    return (
                      <Tooltip
                        key={s.id}
                        label={off ? `${key} is excluded — click to search it again` : `Click to leave ${key} out of this search`}
                        openDelay={400}
                        position="top"
                      >
                        <Badge
                          component="button"
                          type="button"
                          size="sm"
                          variant={off ? "outline" : "light"}
                          color={off ? "gray" : labelColor(s.label, mainSources.map((m) => m.label))}
                          className="mono"
                          onClick={() => toggleExcluded(key)}
                          style={{ cursor: "pointer", opacity: off ? 0.5 : 1, textDecoration: off ? "line-through" : undefined }}
                        >
                          {s.label}
                        </Badge>
                      </Tooltip>
                    );
                  })}
                  {sources.isLoading && <Loader size={14} />}
                </Group>
                {excluded.size > 0 ? (
                  <Group gap={8} align="center">
                    <Text size="xs" c="dimmed">
                      {excluded.size} excluded
                    </Text>
                    <Button size="compact-xs" variant="subtle" onClick={() => setExcluded(new Set())}>
                      Include all
                    </Button>
                  </Group>
                ) : (
                  <Text size="xs" c="dimmed">
                    Click a host to leave it out of the search.
                  </Text>
                )}
              </Stack>
            ) : (
              <>
                <SearchInput size="xs" placeholder="Filter hosts…" value={hostFilter} onChange={setHostFilter} />
                <ScrollArea h="min(360px, 34vh)" type="auto" scrollbars="y" offsetScrollbars>
                  <Stack gap={6} pb={6}>
                    {filteredHosts.map((h) => {
                      // Ticking the host means "every running container on it". Its containers
                      // are then shown ticked and locked, and the files and journal below stay
                      // plainly unticked, so what will be searched is never a guess.
                      const wholeHost = picked.has(`h:${h.alias}`);
                      return (
                      <div key={h.alias}>
                        <Tooltip
                          label="Every running container on this host. The files and journal below are not included unless you tick them."
                          multiline
                          maw={320}
                          openDelay={400}
                          position="right"
                        >
                        <Checkbox
                          size="xs"
                          className="source-check"
                          label={
                            <Text size="sm" className="mono" fw={600}>
                              {h.alias}
                            </Text>
                          }
                          checked={wholeHost}
                          onChange={() => toggle(`h:${h.alias}`)}
                        />
                        </Tooltip>
                        <Stack gap={2} pl={22} mt={2}>
                          {h.containers.map((c) => (
                            <Tooltip key={c.name} label={`${c.name} · ${c.state}`} openDelay={500} position="right">
                              <Checkbox
                                size="xs"
                                className="source-check"
                                label={
                                  <Text size="xs" className="mono" c={c.state === "running" ? undefined : "dimmed"}>
                                    {c.name}
                                  </Text>
                                }
                                checked={wholeHost || picked.has(`c:${h.alias}:${c.name}`)}
                                disabled={wholeHost}
                                onChange={() => toggle(`c:${h.alias}:${c.name}`)}
                              />
                            </Tooltip>
                          ))}
                          {h.files.map((f: LogSourceInfo) => (
                            <Tooltip key={f.id} label={f.path ?? f.id} openDelay={500} position="right" multiline maw={420}>
                              <Checkbox
                                size="xs"
                                className="source-check"
                                label={
                                  <Text size="xs" className="mono" c="dimmed">
                                    {f.kind === "journal" ? `journal: ${f.path ?? f.id.split(":").slice(-1)[0]}` : f.path ?? f.id}
                                  </Text>
                                }
                                checked={picked.has(`f:${h.alias}:${f.id}`)}
                                onChange={() => toggle(`f:${h.alias}:${f.id}`)}
                              />
                            </Tooltip>
                          ))}
                        </Stack>
                      </div>
                      );
                    })}
                    {sources.isLoading && <Loader size={16} />}
                  </Stack>
                </ScrollArea>
              </>
            )}
          </Stack>
        </Card>

        <Card withBorder padding="md" style={{ flexShrink: 0 }}>
          <Stack gap="sm">
            <Text fw={600}>Query</Text>
            {/* The cross only: a stray Escape must not throw away a regex that took a while to write. */}
            <SearchInput
              label="Pattern"
              placeholder={matchMode === "regex" ? "error|exception|timeout" : "exact text"}
              value={pattern}
              onChange={setPattern}
              clearOnEscape={false}
              leftSection={null}
              onKeyDown={(e) => e.key === "Enter" && void run()}
              className="mono"
              description="Leave empty to see everything in the window."
            />
            <Group gap="sm" grow>
              <SegmentedControl
                size="xs"
                value={matchMode}
                onChange={(v) => setMatchMode(v as "regex" | "fixed")}
                data={[
                  { value: "regex", label: "Regex" },
                  { value: "fixed", label: "Exact" },
                ]}
              />
              <Checkbox size="xs" label="Case sensitive" checked={caseSensitive} onChange={(e) => setCaseSensitive(e.currentTarget.checked)} />
            </Group>
            <Group gap="sm" grow>
              <NumberInput size="sm" label="Lines before" value={before} onChange={(v) => setBefore(Number(v) || 0)} min={0} max={500} />
              <NumberInput size="sm" label="Lines after" value={after} onChange={(v) => setAfter(Number(v) || 0)} min={0} max={500} />
            </Group>
            <Select size="sm" label="Time window" data={PRESETS} value={preset} onChange={(v) => setPreset(v ?? "1h")} allowDeselect={false} />
            <Group gap="sm" grow align="flex-start">
              <NumberInput
                size="sm"
                label="Max lines"
                description="per source"
                value={maxLines}
                onChange={(v) => setMaxLines(Math.max(100, Number(v) || 0))}
                min={100}
                max={1000000}
                step={1000}
                thousandSeparator=","
              />
              <NumberInput
                size="sm"
                label="Max size"
                description="MB per source"
                value={maxMb}
                onChange={(v) => setMaxMb(Math.max(1, Number(v) || 0))}
                min={1}
                max={200}
              />
            </Group>
            <Group gap={6}>
              {LINE_CAPS.map((cap) => (
                <Button
                  key={cap.value}
                  size="compact-xs"
                  variant={maxLines === cap.value ? "filled" : "default"}
                  onClick={() => setMaxLines(cap.value)}
                >
                  {cap.label}
                </Button>
              ))}
              <Tooltip label="A busy workload can write far more than this in an hour. The newest lines are kept and the source is marked truncated." multiline maw={320}>
                <Text size="xs" c="dimmed" style={{ cursor: "help" }}>
                  newest first
                </Text>
              </Tooltip>
            </Group>
            {preset === "custom" && (
              <Group gap="sm" grow>
                <DateTimePicker size="sm" label="From" value={since} onChange={(v) => setSince(v ? new Date(v) : null)} withSeconds />
                <DateTimePicker size="sm" label="To" value={until} onChange={(v) => setUntil(v ? new Date(v) : null)} withSeconds />
              </Group>
            )}
          </Stack>
        </Card>
      </Stack>

        {/* Pinned: on a short screen the query panel scrolls, and the one button you came here
            to press must never be the thing that scrolls away. */}
        <Card withBorder padding="sm" style={{ flexShrink: 0 }}>
          <Group gap="xs" wrap="nowrap">
            {running ? (
              <Button color="red" variant="light" leftSection={<IconX size={16} />} onClick={stop} style={{ flex: 1 }}>
                Stop
              </Button>
            ) : (
              // A disabled button cannot receive hover, so the tooltip lives on a wrapper: a
              // control that refuses to work should always be able to say why.
              <Tooltip label={ready ? "Search the selected sources in this window" : reason} multiline maw={260} position="top">
                <div style={{ flex: 1 }}>
                  <Button fullWidth leftSection={<IconSearch size={16} />} onClick={() => void run()} disabled={!ready}>
                    Search
                  </Button>
                </div>
              </Tooltip>
            )}
            <Tooltip label={running === "follow" ? "Stop following" : hasSources ? "Follow live output" : pickReason} multiline maw={260} position="top">
              <div>
                <ActionIcon
                  size="lg"
                  variant={running === "follow" ? "filled" : "default"}
                  color="teal"
                  onClick={running === "follow" ? stop : () => void startFollow()}
                  disabled={!hasSources && running !== "follow"}
                  aria-label="follow"
                >
                  {running === "follow" ? <IconPlayerPause size={18} /> : <IconPlayerPlay size={18} />}
                </ActionIcon>
              </div>
            </Tooltip>
            <Tooltip label={ready ? "Search and save the results as a bundle: one file per source, a merged timeline and a manifest" : reason} multiline maw={280} position="top">
              <div>
                <ActionIcon size="lg" variant="default" onClick={() => void run(true)} disabled={!!running || !ready} aria-label="download bundle">
                  <IconDownload size={18} />
                </ActionIcon>
              </div>
            </Tooltip>
            <Divider orientation="vertical" />
            <Tooltip label="Bundles saved earlier" position="top">
              <ActionIcon size="lg" variant="default" onClick={() => setBundlesOpen(true)} aria-label="saved bundles">
                <IconArchive size={18} />
              </ActionIcon>
            </Tooltip>
          </Group>
          {!ready && !running && (
            <Text size="xs" c="dimmed" mt={8} ta="center">
              {reason}
            </Text>
          )}
        </Card>
      </Stack>

      {/* ------------------------------------------------------ right column */}
      <Stack gap="sm" style={{ flex: 1, minWidth: 0, minHeight: 0 }}>
        <Card withBorder padding="sm">
          <Group justify="space-between" wrap="wrap" gap="sm">
            <Group gap="sm">
              {running && <Loader size={14} />}
              <Text size="sm" fw={600}>
                {running === "follow" ? "Following" : summary ? `${totalMatches} matches` : anyRunning ? "Searching…" : "Ready"}
              </Text>
              {summary && (
                <Text size="sm" c="dimmed">
                  {summary.lines.toLocaleString()} lines · {fmtBytes(summary.bytes)} · {summary.elapsed}s · {fmtDateTime(summary.since)} → {fmtDateTime(summary.until)}
                </Text>
              )}
              {summary?.bundle_name && (
                <Button size="xs" variant="light" color="teal" component="a" href={api.logs.bundleZipUrl(summary.bundle_name)} leftSection={<IconDownload size={14} />}>
                  Download bundle
                </Button>
              )}
            </Group>
            <Group gap="md">
              <Switch size="xs" label="Matches only" checked={matchesOnly} onChange={(e) => setMatchesOnly(e.currentTarget.checked)} />
              <Switch size="xs" label="Wrap" checked={wrap} onChange={(e) => setWrap(e.currentTarget.checked)} />
              <Switch size="xs" label="UTC" checked={useUtc} onChange={(e) => setUseUtc(e.currentTarget.checked)} />
            </Group>
          </Group>
          {Object.keys(sourceStates).length > 0 && (
            <Group gap={6} mt="sm" wrap="wrap">
              {Object.entries(sourceStates).map(([id, s]) => (
                <Tooltip key={id} label={s.message ?? `${s.lines} lines · ${fmtBytes(s.bytes)}`} disabled={!s.message && !s.lines}>
                  <Badge
                    variant={s.state === "error" ? "filled" : "light"}
                    color={s.state === "error" ? "red" : s.state === "truncated" || s.state === "partial" ? "yellow" : labelColor(s.label, labels)}
                    className="mono"
                    rightSection={
                      s.state === "running" ? <Loader size={8} color="gray" /> : <Text size="xs" fw={700}>{s.matches}</Text>
                    }
                  >
                    {s.label}
                  </Badge>
                </Tooltip>
              ))}
            </Group>
          )}
        </Card>

        <div ref={scrollRef} onScroll={onScroll} className={`log-viewer ${wrap ? "wrap" : ""}`} style={{ flex: 1, minHeight: 0 }}>
          {visible.length === 0 ? (
            <EmptyState
              title={running ? "Waiting for the first lines…" : summary ? "No lines matched" : "Search across services in one time window"}
              hint={running ? undefined : summary ? "Widen the window, loosen the pattern, or check the source chips above." : "Pick the sources, type a pattern, choose a window, then press Search or Enter."}
            />
          ) : (
            <div style={{ height: virtualizer.getTotalSize(), position: "relative", minWidth: "100%" }}>
              {virtualizer.getVirtualItems().map((item) => {
                const line = visible[item.index];
                const previous = item.index > 0 ? visible[item.index - 1] : null;
                const entryStart = !line.cont && previous !== null && previous.entry !== line.entry && labels.length > 1;
                return (
                  <div
                    key={item.key}
                    ref={wrap ? virtualizer.measureElement : undefined}
                    data-index={item.index}
                    className={`log-line ${line.match ? "match" : "context"} ${line.cont ? "continuation" : ""} ${entryStart ? "entry-start" : ""}`}
                    style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${item.start}px)`, ...(wrap ? {} : { height: 20 }) }}
                  >
                    <span className="log-label" style={{ color: `var(--mantine-color-${labelColor(line.label, labels)}-4)` }}>
                      {line.cont ? "" : line.label}
                    </span>
                    <span className="log-ts">{line.cont ? "" : fmtLogTime(line.ts, tz)}</span>
                    <span className="log-text">
                      <Highlighted text={line.text} regex={line.match ? highlighter : null} />
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </Stack>

      <BundlesDrawer opened={bundlesOpen} onClose={() => setBundlesOpen(false)} />
    </Group>
  );
}

function BundlesDrawer({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const bundles = useQuery({ queryKey: ["bundles"], queryFn: api.logs.bundles, enabled: opened });
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="lg" title="Saved bundles">
      {bundles.isLoading ? (
        <Loader />
      ) : !bundles.data?.length ? (
        <EmptyState title="No bundles yet" hint="Use the download button next to Search to save a search." />
      ) : (
        <Stack gap="sm">
          {bundles.data.map((bundle) => (
            <Card key={bundle.name} withBorder padding="sm">
              <Group justify="space-between" wrap="nowrap">
                <div style={{ minWidth: 0 }}>
                  <Text size="sm" fw={600} className="mono" truncate>
                    {bundle.name}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {bundle.env} · pattern <Text span className="mono">{bundle.pattern || "(all)"}</Text> · {bundle.totals?.matches ?? 0} matches · {fmtBytes(bundle.size)} ·{" "}
                    {fmtDateTime(bundle.created_at)}
                  </Text>
                </div>
                <Button size="xs" variant="light" component="a" href={api.logs.bundleZipUrl(bundle.name)} leftSection={<IconDownload size={14} />}>
                  zip
                </Button>
              </Group>
            </Card>
          ))}
        </Stack>
      )}
    </Drawer>
  );
}
