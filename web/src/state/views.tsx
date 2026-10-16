import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
} from "react";
import { useLocation } from "react-router-dom";

/**
 * What every page had on it when you left it.
 *
 * A page is a place you go back to: the fleet filtered to one service, the Hosts tab of
 * Settings, a directory three levels down on a server. Walking off to look at something else
 * and coming back to a blank page made each trip cost the work of setting it up again. So the
 * pieces of a page that are yours — filters, tabs, how far a table was widened, where it was
 * scrolled, the address it was on — are held here, above the routes, and handed back on return.
 *
 * In memory only, like the Logs page's own store (state/logs): for as long as the tab lives,
 * never written anywhere. A deliberate new question — a Logs or Files icon beside a host —
 * still arrives with its own address and starts from there.
 */
interface ViewStore {
  has: (key: string) => boolean;
  get: <T>(key: string) => T | undefined;
  set: (key: string, value: unknown) => void;
  /** The sidebar's link for a page: the page, at the address you left it on. */
  resume: (path: string) => string;
  /** Where the main column was scrolled, per page. Read and written on scroll, never rendered. */
  scrolls: Map<string, number>;
}

// The Logs page keeps its own state, and its address is only a seed for a new search: one of
// its links names an environment, and resuming that link would move the sidebar back to it.
const OWN_STATE = new Set(["/logs"]);

const ViewStoreContext = createContext<ViewStore>({
  has: () => false,
  get: () => undefined,
  set: () => {},
  resume: (path) => path,
  scrolls: new Map(),
});

export function ViewStateProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  // Refs, not state: nothing renders from these. A page reads them once on the way in.
  const values = useRef(new Map<string, unknown>());
  const searches = useRef(new Map<string, string>());
  const scrolls = useRef(new Map<string, number>());

  useEffect(() => {
    searches.current.set(location.pathname, location.search);
  }, [location.pathname, location.search]);

  const store = useMemo<ViewStore>(
    () => ({
      has: (key) => values.current.has(key),
      get: <T,>(key: string) => values.current.get(key) as T | undefined,
      set: (key, value) => {
        values.current.set(key, value);
      },
      resume: (path) => (OWN_STATE.has(path) ? path : path + (searches.current.get(path) ?? "")),
      scrolls: scrolls.current,
    }),
    [],
  );

  return <ViewStoreContext.Provider value={store}>{children}</ViewStoreContext.Provider>;
}

export function useViewStore(): ViewStore {
  return useContext(ViewStoreContext);
}

/** useState that outlives the page: it starts from what the page was left with. */
export function useViewState<T>(key: string, initial: T): [T, Dispatch<SetStateAction<T>>] {
  const store = useViewStore();
  const [value, setValue] = useState<T>(() => (store.has(key) ? (store.get<T>(key) as T) : initial));
  useEffect(() => {
    store.set(key, value);
  }, [store, key, value]);
  return [value, setValue];
}

/**
 * How many rows a long table draws, kept between visits — but only for the list it was widened
 * on. `question` is whatever narrows that list; once it changes the table starts from one page
 * again, as it always has. Derived rather than reset in an effect, so arriving back on the page
 * is not mistaken for a new filter.
 */
export function usePageLimit(key: string, question: string, page: number): [number, Dispatch<SetStateAction<number>>] {
  const [held, setHeld] = useViewState(key, { question, limit: page });
  const limit = held.question === question ? held.limit : page;
  const setLimit = useCallback<Dispatch<SetStateAction<number>>>(
    (next) =>
      setHeld((current) => {
        const base = current.question === question ? current.limit : page;
        return { question, limit: typeof next === "function" ? next(base) : next };
      }),
    [question, page, setHeld],
  );
  return [limit, setLimit];
}

/** Each page comes back to where its column was scrolled, and a page never seen starts at the top
 *  — not at whatever offset the page before it was left at. */
export function useKeptScroll(ref: RefObject<HTMLElement | null>) {
  const { scrolls } = useViewStore();
  const { pathname } = useLocation();
  // Before the first paint of the page, so it is never seen at the wrong offset. The listener is
  // the page's own and leaves with it, so one page's offset is never written under another's name.
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    element.scrollTop = scrolls.get(pathname) ?? 0;
    const onScroll = () => scrolls.set(pathname, element.scrollTop);
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, [pathname, ref, scrolls]);
}
