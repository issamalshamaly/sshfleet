import { notifications } from "@mantine/notifications";
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

import { logFollowStream, logSearchStream } from "../api/client";
import type { LogEvent, LogLine, LogSearchBody } from "../api/types";

/** How one source is doing in the search that is running, or the one that just ran. */
export interface SourceState {
  label: string;
  matches: number;
  lines: number;
  bytes: number;
  state: "queued" | "running" | "done" | "truncated" | "partial" | "error";
  message?: string;
}

export type LogSummary = Extract<LogEvent, { type: "summary" }>;
export type LogFollowBody = Parameters<typeof logFollowStream>[0];

/**
 * Everything the Logs page had on it when you left it.
 *
 * A search is a question that takes a while to compose — which hosts, which containers, the
 * pattern, the window, how much of it to pull — and then sends you off to look at something
 * else: the container in the fleet, a file on the host. Coming back to an empty form each
 * time made the page hostile to the way it is actually used, so the page hands its state here
 * on the way out and takes it back on the way in.
 *
 * The results are not in here: they belong to the search itself, which lives in the provider
 * below and keeps running while the page is away. Sets and Dates are stored the way they are
 * typed, not serialised: this lives in memory for as long as the tab does and is never written
 * anywhere.
 */
export interface LogsSnapshot {
  /** The environment picked on this page, used only when the sidebar is on all of them. */
  pick: string | null;
  mode: "main" | "custom";
  picked: Set<string>;
  excluded: Set<string>;
  hostFilter: string;
  pattern: string;
  matchMode: "regex" | "fixed";
  caseSensitive: boolean;
  before: number;
  after: number;
  preset: string;
  maxLines: number;
  maxMb: number;
  since: Date | null;
  until: Date | null;
  wrap: boolean;
  matchesOnly: boolean;
  useUtc: boolean;
  /** How far down the results you had read. Coming back to the top of 20,000 lines loses your place. */
  scrollTop: number;
  /** Which half a narrow screen was showing, the query or the results. A wide one shows both. */
  pane: "query" | "results";
  /** True once the page has been left at least once, so a restored visit knows not to apply
   *  the configured defaults over what the operator typed. */
  restored: boolean;
}

/** A page that has never been opened, and what a fresh start goes back to. */
export function emptySnapshot(): LogsSnapshot {
  return {
    pick: null,
    mode: "main",
    picked: new Set(),
    excluded: new Set(),
    hostFilter: "",
    pattern: "",
    matchMode: "regex",
    caseSensitive: false,
    before: 60,
    after: 0,
    preset: "1h",
    maxLines: 20000,
    maxMb: 20,
    since: new Date(Date.now() - 3600_000),
    until: new Date(),
    wrap: false,
    matchesOnly: false,
    useUtc: false,
    scrollTop: 0,
    pane: "query",
    restored: false,
  };
}

/** The defaults, plus whatever the link that opened the page asked for. */
export function seedFromParams(params: URLSearchParams): LogsSnapshot {
  const seed = emptySnapshot();
  const host = params.get("host");
  const container = params.get("container");
  if (host && container) {
    seed.mode = "custom";
    seed.picked = new Set([`c:${host}:${container}`]);
  } else if (host) {
    seed.mode = "custom";
    seed.picked = new Set([`h:${host}`]);
  }
  seed.pattern = params.get("pattern") ?? "";
  return seed;
}

interface LogsStore {
  /** What the page was left with, or an empty page the first time. */
  get: () => LogsSnapshot;
  save: (snapshot: LogsSnapshot) => void;
  /** Throw it away: the next visit starts from scratch. */
  clear: () => void;
}

/**
 * The search itself: the stream from the server and what it has sent so far.
 *
 * It lives above the routes, like an open shell, so walking off to the fleet while a search is
 * still reading does not cut it short — you come back to the whole answer, or to it still
 * arriving. Only the page renders from it, so its updates cost nothing elsewhere.
 */
export interface LogsEngine {
  lines: LogLine[];
  sourceStates: Record<string, SourceState>;
  summary: LogSummary | null;
  running: "search" | "follow" | null;
  /** Start a search, replacing whatever was on screen. Resolves when the stream ends. */
  search: (body: LogSearchBody) => Promise<void>;
  /** Tail the selected sources live until stopped. */
  follow: (body: LogFollowBody) => Promise<void>;
  /** End the stream; what it had sent stays on screen. */
  stop: () => void;
  /** End the stream and forget its results: a new question is being asked. */
  reset: () => void;
}

const LINE_CAP = 200_000;

const LogsStoreContext = createContext<LogsStore>({ get: emptySnapshot, save: () => {}, clear: () => {} });
const LogsEngineContext = createContext<LogsEngine>({
  lines: [],
  sourceStates: {},
  summary: null,
  running: null,
  search: async () => {},
  follow: async () => {},
  stop: () => {},
  reset: () => {},
});

export function LogsProvider({ children }: { children: ReactNode }) {
  // ------------------------------------------------------------ the form
  // A ref, not state: nothing renders from this. The page reads it once on the way in and
  // writes it once on the way out, and a re-render in between would only cost frames.
  const held = useRef<LogsSnapshot | null>(null);
  const store = useMemo<LogsStore>(
    () => ({
      get: () => held.current ?? emptySnapshot(),
      save: (snapshot) => {
        held.current = snapshot;
      },
      clear: () => {
        held.current = null;
      },
    }),
    [],
  );

  // ---------------------------------------------------------- the search
  const [lines, setLines] = useState<LogLine[]>([]);
  const [sourceStates, setSourceStates] = useState<Record<string, SourceState>>({});
  const [summary, setSummary] = useState<LogSummary | null>(null);
  const [running, setRunning] = useState<"search" | "follow" | null>(null);
  const abort = useRef<AbortController | null>(null);
  const pending = useRef<LogLine[]>([]);
  const flushTimer = useRef<number | null>(null);

  // Lines arrive far faster than React should render: they are batched and appended a few
  // times a second, and the oldest are dropped past the cap so a wide window cannot eat the tab.
  const flush = useCallback(() => {
    flushTimer.current = null;
    if (!pending.current.length) return;
    const batch = pending.current;
    pending.current = [];
    setLines((current) => {
      const next = current.concat(batch);
      return next.length > LINE_CAP ? next.slice(next.length - LINE_CAP) : next;
    });
  }, []);

  const handleEvent = useCallback(
    (event: LogEvent) => {
      switch (event.type) {
        case "start":
          setSourceStates(Object.fromEntries(event.sources.map((s) => [s.id, { label: s.label, matches: 0, lines: 0, bytes: 0, state: "running" as const }])));
          break;
        case "line":
          pending.current.push(event);
          if (flushTimer.current === null) flushTimer.current = globalThis.setTimeout(flush, 80);
          break;
        case "progress":
          setSourceStates((s) => ({ ...s, [event.source]: { ...(s[event.source] ?? { label: event.source }), matches: event.matches, lines: event.lines, bytes: event.bytes, state: "running" } }));
          break;
        case "source_done":
          setSourceStates((s) => ({
            ...s,
            [event.source]: {
              label: event.label,
              matches: event.matches,
              lines: event.lines,
              bytes: event.bytes,
              state: event.truncated ? "truncated" : event.coverage_ok ? "done" : "partial",
              message: event.truncated
                ? event.truncated_reason
                : event.coverage_ok
                  ? undefined
                  : "hit the line cap before reaching the start of the window — raise Max lines or narrow the window",
            },
          }));
          break;
        case "source_error":
          setSourceStates((s) => ({ ...s, [event.source]: { label: event.label, matches: 0, lines: 0, bytes: 0, state: "error", message: `${event.kind}: ${event.message}` } }));
          break;
        case "summary":
          setSummary(event);
          // Said here, once, when it happens — not by the page, which would say it again on
          // every return. You may well be on another page when the bundle lands.
          if (event.bundle_name) {
            notifications.show({
              title: "Bundle ready",
              message: `${event.matches} matches saved to ${event.bundle_name}`,
              color: "teal",
              autoClose: 10_000,
            });
          }
          break;
      }
    },
    [flush],
  );

  // Follow never sends source_done — it only ends when someone stops it — and a search that is
  // stopped early never sends the rest of them either. Without this the chips keep their
  // spinner for the rest of the session, which reads as "still working" long after nothing is.
  const settleSources = useCallback(() => {
    setSourceStates((current) => {
      if (!Object.values(current).some((s) => s.state === "running")) return current;
      return Object.fromEntries(
        Object.entries(current).map(([id, s]) => [id, s.state === "running" ? { ...s, state: "done" as const } : s]),
      );
    });
  }, []);

  const stop = useCallback(() => {
    abort.current?.abort();
    abort.current = null;
    setRunning(null);
  }, []);

  const clear = useCallback(() => {
    if (flushTimer.current !== null) globalThis.clearTimeout(flushTimer.current);
    flushTimer.current = null;
    pending.current = [];
    setLines([]);
    setSummary(null);
    setSourceStates({});
  }, []);

  const reset = useCallback(() => {
    stop();
    clear();
  }, [stop, clear]);

  /** One stream at a time: starting another ends the one before it. */
  const begin = useCallback(
    (kind: "search" | "follow") => {
      stop();
      clear();
      const controller = new AbortController();
      abort.current = controller;
      setRunning(kind);
      return controller;
    },
    [stop, clear],
  );

  const finish = useCallback(
    (controller: AbortController) => {
      settleSources();
      if (abort.current === controller) {
        abort.current = null;
        setRunning(null);
      }
    },
    [settleSources],
  );

  const search = useCallback(
    async (body: LogSearchBody) => {
      const controller = begin("search");
      try {
        await logSearchStream(body, handleEvent, controller.signal);
        flush();
      } catch (error) {
        if (!controller.signal.aborted) notifications.show({ title: "search failed", message: String((error as Error).message), color: "red" });
      } finally {
        finish(controller);
      }
    },
    [begin, finish, handleEvent, flush],
  );

  const follow = useCallback(
    async (body: LogFollowBody) => {
      const controller = begin("follow");
      try {
        await logFollowStream(body, handleEvent, controller.signal);
        flush();
      } catch (error) {
        if (!controller.signal.aborted) notifications.show({ title: "follow stopped", message: String((error as Error).message), color: "red" });
      } finally {
        finish(controller);
      }
    },
    [begin, finish, handleEvent, flush],
  );

  const engine = useMemo<LogsEngine>(
    () => ({ lines, sourceStates, summary, running, search, follow, stop, reset }),
    [lines, sourceStates, summary, running, search, follow, stop, reset],
  );

  return (
    <LogsStoreContext.Provider value={store}>
      <LogsEngineContext.Provider value={engine}>{children}</LogsEngineContext.Provider>
    </LogsStoreContext.Provider>
  );
}

export function useLogsStore(): LogsStore {
  return useContext(LogsStoreContext);
}

export function useLogsEngine(): LogsEngine {
  return useContext(LogsEngineContext);
}
