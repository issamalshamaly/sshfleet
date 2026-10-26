import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { useConfig } from "../hooks/useFleet";

// What the pages are looking at, in the order the fleet is organised: a project, and inside it
// either one environment or all of them. `env: null` means every environment of the current
// project — never every environment everywhere, because a host always sits inside a project.
// Both are remembered per browser. An environment is held by its id: its name is unique only
// inside its project, so two projects can each have a "staging".
interface EnvState {
  project: string | null;
  setProject: (project: string) => void;
  /** The id of the environment in view, or null for all of the project's. */
  env: string | null;
  setEnv: (env: string | null) => void;
  /** Environment ids of the current project, in config order. */
  envIds: string[];
  /** Does a host in this environment (by id) belong to what is being looked at? */
  inScope: (envId: string) => boolean;
}

const EnvContext = createContext<EnvState>({
  project: null,
  setProject: () => {},
  env: null,
  setEnv: () => {},
  envIds: [],
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

  const envIds = useMemo(
    () => (environments ?? []).filter((e) => e.project === project).map((e) => e.id),
    [environments, project],
  );

  // An environment from another project is not a view anyone asked for, so it is dropped rather
  // than left selected and silently filtering everything out.
  useEffect(() => {
    if (!environments?.length || !project) return;
    if (env && !envIds.includes(env)) {
      setEnvState(null);
      remember(KEY, null);
    }
  }, [environments, project, env, envIds]);

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

  const envName = environments?.find((e) => e.id === env)?.name ?? env;
  useEffect(() => {
    document.title = envName ? `sshfleet · ${envName}` : project ? `sshfleet · ${project}` : "sshfleet";
  }, [envName, project]);

  const loaded = !!config.data;
  const inScope = useCallback(
    (envId: string) => {
      if (env) return envId === env;
      // Before the config arrives there is nothing to narrow by, so nothing is hidden. Once it
      // has arrived, a project with no environments shows nothing — "all of an empty project" is
      // empty, not everything.
      return loaded ? envIds.includes(envId) : true;
    },
    [env, envIds, loaded],
  );

  const value = useMemo(
    () => ({ project, setProject, env, setEnv, envIds, inScope }),
    [project, setProject, env, setEnv, envIds, inScope],
  );
  return <EnvContext.Provider value={value}>{children}</EnvContext.Provider>;
}

export function useEnv(): EnvState {
  return useContext(EnvContext);
}
