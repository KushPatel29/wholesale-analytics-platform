(() => {
  if (window.analyticsPageCache) return;

  const VERSION = "20260401-v1";
  const SNAPSHOT_PREFIX = `wholesale:${VERSION}:snapshot:`;
  const REQUEST_PREFIX = `wholesale:${VERSION}:request:`;
  const MAX_KEYS = 48;
  const DEFAULT_FRESH_MS = 90 * 1000;
  const DEFAULT_MAX_AGE_MS = 20 * 60 * 1000;

  const getStorage = () => {
    try {
      return window.sessionStorage || null;
    } catch (_err) {
      return null;
    }
  };

  const storage = getStorage();
  const now = () => Date.now();
  const currentUserId = () => {
    try {
      const value = window.__FILTER_CTX__?.user_id;
      return String(value || "anon");
    } catch (_err) {
      return "anon";
    }
  };

  const normalizeQs = (qs) => {
    const params = new URLSearchParams(String(qs || "").replace(/^\?/, ""));
    const ordered = new URLSearchParams();
    [...params.keys()]
      .sort()
      .forEach((key) => {
        const values = params.getAll(key).filter((value) => value !== "");
        values.forEach((value) => ordered.append(key, value));
      });
    return ordered.toString();
  };

  const snapshotKey = (pageId, qs, pathname = window.location.pathname || "") =>
    `${SNAPSHOT_PREFIX}${currentUserId()}:${pathname}:${pageId}:${normalizeQs(qs)}`;

  const requestKey = (url) => `${REQUEST_PREFIX}${currentUserId()}:${String(url || "")}`;

  const readJson = (key) => {
    if (!storage || !key) return null;
    try {
      const raw = storage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_err) {
      return null;
    }
  };

  const keysForPrefix = (prefix) => {
    if (!storage) return [];
    const keys = [];
    for (let idx = 0; idx < storage.length; idx += 1) {
      const key = storage.key(idx);
      if (key && key.startsWith(prefix)) keys.push(key);
    }
    return keys;
  };

  const prunePrefix = (prefix) => {
    if (!storage) return;
    const ranked = keysForPrefix(prefix)
      .map((key) => ({ key, savedAt: Number(readJson(key)?.saved_at || 0) }))
      .sort((a, b) => b.savedAt - a.savedAt);
    ranked.slice(MAX_KEYS).forEach(({ key }) => {
      try {
        storage.removeItem(key);
      } catch (_err) {
        /* ignore storage failures */
      }
    });
  };

  const writeJson = (key, value, prefix) => {
    if (!storage || !key) return false;
    try {
      storage.setItem(key, JSON.stringify(value));
      if (prefix) prunePrefix(prefix);
      return true;
    } catch (_err) {
      try {
        prunePrefix(prefix || SNAPSHOT_PREFIX);
        storage.setItem(key, JSON.stringify(value));
        return true;
      } catch (_retryErr) {
        return false;
      }
    }
  };

  const saveSnapshot = (pageId, options = {}) => {
    const pathname = options.pathname || window.location.pathname || "";
    const key = snapshotKey(pageId, options.qs || "", pathname);
    const previous = readJson(key) || {};
    const payload = options.payload === undefined ? previous.payload : options.payload;
    if (payload === undefined || payload === null) return false;
    const entry = {
      version: VERSION,
      user_id: currentUserId(),
      page_id: String(pageId || ""),
      pathname,
      qs: normalizeQs(options.qs || ""),
      saved_at: now(),
      payload,
      ui_state: options.uiState === undefined ? previous.ui_state : options.uiState,
      scroll_y: options.scrollY === undefined ? previous.scroll_y : options.scrollY,
      meta: options.meta === undefined ? previous.meta : options.meta,
    };
    return writeJson(key, entry, SNAPSHOT_PREFIX);
  };

  const loadSnapshot = (pageId, options = {}) => {
    const pathname = options.pathname || window.location.pathname || "";
    const key = snapshotKey(pageId, options.qs || "", pathname);
    const entry = readJson(key);
    if (!entry || entry.version !== VERSION || entry.user_id !== currentUserId()) return null;
    const ageMs = Math.max(0, now() - Number(entry.saved_at || 0));
    const freshMs = Math.max(0, Number(options.freshMs || DEFAULT_FRESH_MS));
    const maxAgeMs = Math.max(freshMs, Number(options.maxAgeMs || DEFAULT_MAX_AGE_MS));
    if (ageMs > maxAgeMs) return null;
    return {
      ...entry,
      key,
      ageMs,
      fresh: ageMs <= freshMs,
      stale: ageMs > freshMs,
    };
  };

  const saveScroll = (pageId, options = {}) =>
    saveSnapshot(pageId, { ...options, scrollY: Number(options.scrollY ?? window.scrollY ?? 0) || 0 });

  const restoreScroll = (pageId, options = {}) => {
    const snapshot = loadSnapshot(pageId, options);
    if (!snapshot || snapshot.scroll_y == null) return false;
    const delayMs = Math.max(0, Number(options.delayMs || 0));
    window.setTimeout(() => {
      try {
        window.scrollTo({ top: Number(snapshot.scroll_y) || 0, behavior: "auto" });
      } catch (_err) {
        window.scrollTo(0, Number(snapshot.scroll_y) || 0);
      }
    }, delayMs);
    return true;
  };

  const prepareHeaders = (url, headers = {}) => {
    const nextHeaders = { ...headers };
    const meta = readJson(requestKey(url));
    const ageMs = Math.max(0, now() - Number(meta?.saved_at || 0));
    if (meta?.etag && ageMs <= DEFAULT_MAX_AGE_MS && !nextHeaders["If-None-Match"]) {
      nextHeaders["If-None-Match"] = meta.etag;
    }
    return nextHeaders;
  };

  const rememberResponse = (url, response) => {
    if (!storage || !response || !url) return false;
    const etag = response.headers?.get?.("ETag");
    if (!etag) return false;
    return writeJson(
      requestKey(url),
      {
        version: VERSION,
        user_id: currentUserId(),
        url: String(url),
        etag,
        dataset_version: response.headers.get("X-Dataset-Version") || null,
        saved_at: now(),
      },
      REQUEST_PREFIX
    );
  };

  window.analyticsPageCache = {
    VERSION,
    normalizeQs,
    snapshotKey,
    loadSnapshot,
    saveSnapshot,
    saveScroll,
    restoreScroll,
    prepareHeaders,
    rememberResponse,
  };
})();
