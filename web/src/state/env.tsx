import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { useConfig } from "../hooks/useFleet";

// What the pages are looking at, in the order the fleet is organised: a project, and inside it
// either one environment or all of them. `env: null` means every environment of the current
// project — never every environment everywhere, because a host always sits inside a project.
// Both are remembered per browser.
interface EnvState {
  project: string | null;
  setProject: (project: string) => void;
  env: string | null;
  setEnv: (env: string | null) => void;
  /** Environment names of the current project, in config order. */
  envNames: string[];
  /** Does a host in this environment belong to what is being looked at? */
  inScope: (envName: string) => boolean;
}

const EnvContext = createContext<EnvState>({
  project: null,
  setProject: () => {},
  env: null,
  setEnv: () => {},
  envNames: [],
  inScope: () => true,
});
const KEY = "sshfleet.env";
const PROJECT_KEY = "sshfleet.project";

function remembered(key: string): string | null {
  try {
    return localStorage.getItem(key) || null;
  } catch {
    return null; // private mode
  }
}

function remember(key: string, value: string | null): void {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    /* private mode */
  }
}

export function EnvProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [project, setProjectState] = useState<string | null>(() => remembered(PROJECT_KEY));
  const [env, setEnvState] = useState<string | null>(() => remembered(KEY));

  const projects = config.data?.projects;
  const environments = config.data?.environments;

  // Land on a real project: the remembered one while it still exists, otherwise the first.
  useEffect(() => {
    if (!projects?.length) return;
    if (project && projects.some((p) => p.name === project)) return;
    const first = projects[0].name;
    setProjectState(first);
    remember(PROJECT_KEY, first);
  }, [projects, project]);

  const envNames = useMemo(
    () => (environments ?? []).filter((e) => e.project === project).map((e) => e.name),
    [environments, project],
  );

  // An environment from another project is not a view anyone asked for, so it is dropped rather
  // than left selected and silently filtering everything out.
  useEffect(() => {
    if (!environments?.length || !project) return;
    if (env && !envNames.includes(env)) {
      setEnvState(null);
      remember(KEY, null);
    }
  }, [environments, project, env, envNames]);

  const setProject = useCallback((next: string) => {
    setProjectState(next);
    remember(PROJECT_KEY, next);
    setEnvState(null); // a project's environments are its own; start at all of them
    remember(KEY, null);
  }, []);

  const setEnv = useCallback((next: string | null) => {
    setEnvState(next);
    remember(KEY, next);
  }, []);

  useEffect(() => {
    document.title = env ? `sshfleet · ${env}` : project ? `sshfleet · ${project}` : "sshfleet";
  }, [env, project]);

  const loaded = !!config.data;
  const inScope = useCallback(
    (envName: string) => {
      if (env) return envName === env;
      // Before the config arrives there is nothing to narrow by, so nothing is hidden. Once it
      // has arrived, a project with no environments shows nothing — "all of an empty project" is
      // empty, not everything.
      return loaded ? envNames.includes(envName) : true;
    },
    [env, envNames, loaded],
  );

  const value = useMemo(
    () => ({ project, setProject, env, setEnv, envNames, inScope }),
    [project, setProject, env, setEnv, envNames, inScope],
  );
  return <EnvContext.Provider value={value}>{children}</EnvContext.Provider>;
}

export function useEnv(): EnvState {
  return useContext(EnvContext);
}
