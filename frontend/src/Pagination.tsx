import { useCallback, useEffect, useRef, useState } from "react";

export function useCursorPage<T>(filterKey: string, fetchPage: (cursor: string | null, limit: number) => Promise<{ items: T[]; next_cursor: string | null }>) {
  const [position, setPosition] = useState<{ key: string; cursors: (string | null)[] }>({ key: filterKey, cursors: [null] });
  const [limit, setLimit] = useState(20);
  const key = `${filterKey}:${limit}`;
  const cursors = position.key === key ? position.cursors : [null];
  const cursor = cursors[cursors.length - 1];
  const [items, setItems] = useState<T[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const loader = useRef(fetchPage); loader.current = fetchPage;
  const sequence = useRef(0);
  const inFlight = useRef<number | null>(null);
  const load = useCallback(async (quiet = false) => {
    if (quiet && inFlight.current !== null) return;
    const request = ++sequence.current;
    inFlight.current = request;
    if (!quiet) { setLoading(true); setItems(null); }
    setError("");
    try {
      const page = await loader.current(cursor, limit);
      if (request !== sequence.current) return;
      setItems(page.items); setNextCursor(page.next_cursor);
    } catch (reason) {
      if (request !== sequence.current) return;
      setError(reason instanceof Error ? reason.message : "加载失败，请重试");
      if (!quiet) { setItems([]); setNextCursor(null); }
    } finally { if (inFlight.current === request) inFlight.current = null; if (request === sequence.current) setLoading(false); }
  }, [key, cursor, limit]);
  useEffect(() => { void load(); return () => { sequence.current++; }; }, [load]);
  return {
    items, setItems, error, setError, loading, load, limit, page: cursors.length,
    hasNext: Boolean(nextCursor),
    next: () => { if (nextCursor && !loading) setPosition({ key, cursors: [...cursors, nextCursor] }); },
    previous: () => { if (!loading && cursors.length > 1) setPosition({ key, cursors: cursors.slice(0, -1) }); },
    resize: (value: number) => { setLimit(value); setPosition({ key: "", cursors: [null] }); },
  };
}

export function Pagination({ pager }: { pager: { page: number; limit: number; loading: boolean; hasNext: boolean; next: () => void; previous: () => void; resize: (size: number) => void; load: () => Promise<void> } }) {
  return <nav className="user-pagination" aria-label="分页">
    <label>每页 <select value={pager.limit} onChange={(event) => pager.resize(Number(event.target.value))} disabled={pager.loading}><option value={20}>20 条</option><option value={40}>40 条</option><option value={80}>80 条</option></select></label>
    <div className="pagination-navigation">
    <button type="button" className="user-secondary" onClick={pager.previous} disabled={pager.loading || pager.page === 1}>上一页</button>
    <span aria-live="polite">第 {pager.page} 页</span>
    <button type="button" className="user-secondary" onClick={pager.next} disabled={pager.loading || !pager.hasNext}>下一页</button>
    </div>
    <button type="button" className="pagination-refresh" onClick={() => void pager.load()} disabled={pager.loading}>{pager.loading ? "加载中" : "刷新"}</button>
  </nav>;
}
