import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

/** One live session: a WebSocket and the ssh behind it, either a host shell or a quick action. */
export interface TerminalSession {
  /** Stable identity, and the React key that keeps the session mounted. */
  id: string;
  alias: string;
  /** What the tab says: the host, or the container the action is running in. */
  title: string;
  /** The second line: what this session is, when it is not just a shell on the host. */
  subtitle?: string;
  /** A quick action id from the server's catalogue, absent for a plain host shell. */
  action?: string;
  container?: string;
}

// Open shells, and which of them is on screen. A session is a live WebSocket and a running ssh,
// so minimising must not unmount it: the list here is what stays mounted, and `activeId` is only
// what is shown. Sessions outlive navigation between pages, which is the point — you can be
// looking at the fleet, or the logs, with a shell still open behind them.
interface TerminalState {
  /** Live sessions, oldest first. */
  sessions: TerminalSession[];
  /** The session on screen, or null when they are all minimised. */
  activeId: string | null;
  /** A plain shell on this host, or the one already open for it. */
  openHost: (alias: string) => void;
  /** Another plain shell on this host, alongside any already open there. */
  newHost: (alias: string) => void;
  /** A quick action in its own session, or the one already running that same action. */
  openAction: (session: Omit<TerminalSession, "id">) => void;
  /** Bring a session back to the front. */
  show: (id: string) => void;
  /** Leave every session running and show none of them. */
  minimise: () => void;
  /** End one session for good. */
  close: (id: string) => void;
  /** End every session. */
  closeAll: () => void;
  /** The plain shells open on this host, oldest first. The fleet row draws itself from this. */
  hostSessions: (alias: string) => TerminalSession[];
  /** The shell behind this session has exited; it is still on screen to be read. */
  ended: (id: string) => boolean;
  markEnded: (id: string) => void;
}

const NOOP: TerminalState = {
  sessions: [],
  activeId: null,
  openHost: () => {},
  newHost: () => {},
  openAction: () => {},
  show: () => {},
  minimise: () => {},
  close: () => {},
  closeAll: () => {},
  hostSessions: () => [],
  ended: () => false,
  markEnded: () => {},
};

const TerminalContext = createContext<TerminalState>(NOOP);

/** One session per (container, action) pair: pressing the same quick action twice comes back to
 *  the session already running it rather than opening a second. A host's first shell is keyed by
 *  the alias alone; the extra ones asked for with `newHost` get their own ids. */
function sessionId(session: Omit<TerminalSession, "id">): string {
  return session.action ? `${session.alias}|${session.container ?? ""}|${session.action}` : session.alias;
}

export function TerminalProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<TerminalSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [dead, setDead] = useState<string[]>([]);

  const open = useCallback((session: Omit<TerminalSession, "id">) => {
    const id = sessionId(session);
    setSessions((current) => (current.some((s) => s.id === id) ? current : [...current, { ...session, id }]));
    setActiveId(id);
  }, []);

  // Bring back the newest shell on this host, if there is one, rather than always the first.
  const openHost = useCallback(
    (alias: string) => {
      const existing = sessions.filter((s) => !s.action && s.alias === alias).at(-1);
      if (existing) setActiveId(existing.id);
      else open({ alias, title: alias });
    },
    [sessions, open],
  );

  // The first free number, so closing "web (2)" and opening another gives "web (2)" again.
  const newHost = useCallback(
    (alias: string) => {
      if (!sessions.some((s) => s.id === alias)) return open({ alias, title: alias });
      let n = 2;
      while (sessions.some((s) => s.id === `${alias}#${n}`)) n += 1;
      const id = `${alias}#${n}`;
      setSessions((current) => [...current, { id, alias, title: `${alias} (${n})` }]);
      setActiveId(id);
    },
    [sessions, open],
  );

  const show = useCallback((id: string) => setActiveId(id), []);

  const markEnded = useCallback((id: string) => {
    setDead((current) => (current.includes(id) ? current : [...current, id]));
  }, []);

  const minimise = useCallback(() => setActiveId(null), []);

  const close = useCallback(
    (id: string) => {
      setSessions((current) => current.filter((s) => s.id !== id));
      setDead((current) => current.filter((s) => s !== id));
      // Closing the tab you are looking at moves you to the next one rather than hiding the
      // dock: the others are still running, and losing sight of them reads as losing them.
      setActiveId((active) => (active === id ? (sessions.filter((s) => s.id !== id).at(-1)?.id ?? null) : active));
    },
    [sessions],
  );

  const closeAll = useCallback(() => {
    setSessions([]);
    setDead([]);
    setActiveId(null);
  }, []);

  const value = useMemo<TerminalState>(
    () => ({
      sessions,
      activeId,
      openHost,
      newHost,
      openAction: open,
      show,
      minimise,
      close,
      closeAll,
      hostSessions: (alias) => sessions.filter((s) => !s.action && s.alias === alias),
      ended: (id) => dead.includes(id),
      markEnded,
    }),
    [sessions, activeId, dead, open, openHost, newHost, show, minimise, close, closeAll, markEnded],
  );
  return <TerminalContext.Provider value={value}>{children}</TerminalContext.Provider>;
}

export function useTerminals(): TerminalState {
  return useContext(TerminalContext);
}
