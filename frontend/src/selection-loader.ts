import { api, selectionPixels } from "./user-api";

/** One short-lived, in-memory entry per editor. Share hover and open requests. */
export function createSelectionLoader() {
  let current: { id: string; started: number; abort: AbortController; layers: ReturnType<typeof start> } | null = null;
  let expiry: ReturnType<typeof setTimeout> | undefined;
  function start(id: string, abort: AbortController) {
    const context = api.selection(id, abort.signal);
    const source = context.then((value) => { abort.signal.throwIfAborted(); return selectionPixels(value.source_url, abort.signal); });
    const result = context.then((value) => { abort.signal.throwIfAborted(); return value.source_url === value.result_url ? undefined : selectionPixels(value.result_url, abort.signal); });
    for (const promise of [context, source, result]) void promise.catch(() => undefined);
    return { context, source, result };
  }
  function clear() { clearTimeout(expiry); current?.abort.abort(); current = null; }
  function load(id: string) {
    if (current?.id === id && Date.now() - current.started < 30000) return current.layers;
    clear();
    const abort = new AbortController();
    const entry = { id, started: Date.now(), abort, layers: start(id, abort) };
    current = entry;
    void Promise.all([entry.layers.source, entry.layers.result]).then(([source, result]) => {
      if (current !== entry) return;
      if (source.size + (result?.size || 0) > 64 * 1024 * 1024) current = null;
      else expiry = setTimeout(() => { if (current === entry) current = null; }, 30000);
    }, () => { if (current === entry) current = null; });
    return entry.layers;
  }
  return { load, clear, prefetch: (id: string) => { load(id); } };
}

export type SelectionLoader = ReturnType<typeof createSelectionLoader>;
