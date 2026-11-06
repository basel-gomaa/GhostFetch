import { useEffect, useState } from "react";
import { Actor, api, PlatformOption } from "./api";

/**
 * Labels and logos for Actors and platforms, loaded ONCE per page and shared.
 *
 * Runs and accounts carry only a name ("the run's actor"), and a list of fifty
 * runs must not mean fifty requests. Both lists are small, change only when
 * config/actors.json does, and every screen wants the same answer, so one
 * promise each is kept for the life of the page. A failed load is forgotten so
 * the next screen tries again; until then names are shown as they are.
 */
let actors: Promise<Actor[]> | null = null;
let platforms: Promise<PlatformOption[]> | null = null;

function load<T>(get: () => Promise<T[]>, reset: () => void): Promise<T[]> {
  return get().catch(() => { reset(); return [] as T[]; });
}

export function useCatalog() {
  const [state, setState] = useState<{ actors: Actor[]; platforms: PlatformOption[] }>(
    { actors: [], platforms: [] });
  useEffect(() => {
    actors ??= load(api.actors, () => { actors = null; });
    platforms ??= load(api.platforms, () => { platforms = null; });
    let live = true;
    Promise.all([actors, platforms]).then(([a, p]) => { if (live) setState({ actors: a, platforms: p }); });
    return () => { live = false; };
  }, []);
  return {
    actor: (name: string) => state.actors.find((a) => a.name === name),
    platform: (name: string) => state.platforms.find((p) => p.name === name),
  };
}
