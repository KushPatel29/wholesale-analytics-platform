(() => {
  const AUTO_KEY = 'wa_auto_refresh';

  const state = {
    toastEl: null,
    toast: null,
    messageEl: null,
    refreshBtn: null,
    toggleEl: null,
    auto: false,
    refreshing: false,
    pending: false,
    lastPayload: null,
    eventSource: null,
    sseAttempted: false,
  };

  const disableSse =
    window.__WA_DISABLE_SSE__ === true ||
    window.__WA_DISABLE_SSE__ === 'true';
  const maxSseRetries = Math.max(
    0,
    Number.parseInt(window.__WA_SSE_MAX_RETRIES, 10) || 5,
  );

  const handlers = new Set();

  function ensureGlobalNamespace() {
    const global = window.Wholesale Analytics || (window.Wholesale Analytics = {});

    global.registerRefreshHandler = (handler) => {
      if (typeof handler !== 'function') return () => {};
      handlers.add(handler);
      startEventStream();
      return () => {
        handlers.delete(handler);
        if (!handlers.size && state.eventSource) {
          try {
            state.eventSource.close();
          } catch (err) {
            /* ignore */
          }
          state.eventSource = null;
          state.sseAttempted = false;
        }
      };
    };

    global.unregisterRefreshHandler = (handler) => {
      handlers.delete(handler);
      if (!handlers.size && state.eventSource) {
        try {
          state.eventSource.close();
        } catch (err) {
          /* ignore */
        }
        state.eventSource = null;
        state.sseAttempted = false;
      }
    };

    global.triggerRefresh = (reason = 'manual') => triggerRefresh(reason);
    global.isAutoRefreshEnabled = () => state.auto;
  }

  ensureGlobalNamespace();

  function readAutoPreference() {
    try {
      return window.localStorage.getItem(AUTO_KEY) === 'true';
    } catch (err) {
      console.warn('Unable to read auto-refresh preference', err);
      return false;
    }
  }

  function persistAutoPreference(enabled) {
    try {
      window.localStorage.setItem(AUTO_KEY, enabled ? 'true' : 'false');
    } catch (err) {
      console.warn('Unable to persist auto-refresh preference', err);
    }
  }

  function formatTimeLabel(isoString) {
    if (!isoString) {
      return 'just now';
    }
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) {
      return 'just now';
    }
    try {
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (err) {
      return 'just now';
    }
  }

  function updateToastMessage(text) {
    if (state.messageEl) {
      state.messageEl.textContent = text;
    }
  }

  function setRefreshingUi(active) {
    state.refreshing = active;
    if (state.refreshBtn) {
      state.refreshBtn.disabled = active;
      state.refreshBtn.classList.toggle('disabled', active);
      state.refreshBtn.textContent = active ? 'Refreshing…' : 'Refresh';
    }
    if (state.toastEl) {
      state.toastEl.classList.toggle('toast-updating', active);
    }
  }

  function showToast(payload) {
    state.lastPayload = payload || null;
    const timeText = formatTimeLabel(payload?.built_at);
    updateToastMessage(`New data loaded (${timeText}). Refresh charts?`);
    if (state.toggleEl) {
      state.toggleEl.checked = state.auto;
    }
    if (state.refreshBtn) {
      state.refreshBtn.disabled = state.refreshing;
      state.refreshBtn.classList.toggle('disabled', state.refreshing);
      state.refreshBtn.textContent = state.refreshing ? 'Refreshing…' : 'Refresh';
    }
    if (state.toast) {
      state.toast.show();
    } else if (state.toastEl) {
      state.toastEl.classList.add('show');
    }
  }

  function hideToastAfter(delay = 0) {
    const hide = () => {
      if (state.toast) {
        state.toast.hide();
      } else if (state.toastEl) {
        state.toastEl.classList.remove('show');
      }
    };
    if (delay > 0) {
      setTimeout(hide, delay);
    } else {
      hide();
    }
  }

  async function triggerRefresh(reason = 'manual') {
    if (state.refreshing) {
      state.pending = true;
      return Promise.resolve();
    }

    setRefreshingUi(true);
    if (reason === 'auto') {
      updateToastMessage('Refreshing charts automatically…');
    } else {
      updateToastMessage('Refreshing charts…');
    }

    window.dispatchEvent(new CustomEvent('wholesale:refresh', { detail: { reason } }));

    const callbacks = Array.from(handlers);
    const executions = callbacks.map((handler) => {
      try {
        const result = handler({ reason });
        return (result && typeof result.then === 'function') ? result : Promise.resolve(result);
      } catch (err) {
        console.error('Refresh handler failed', err);
        return Promise.reject(err);
      }
    });

    return Promise.allSettled(executions)
      .catch((err) => {
        console.error('Refresh sequence encountered errors', err);
      })
      .finally(() => {
        setRefreshingUi(false);
        const rerun = state.pending;
        state.pending = false;
        if (rerun) {
          triggerRefresh('auto');
          return;
        }
        const timeText = formatTimeLabel(state.lastPayload?.built_at);
        updateToastMessage(`Charts updated (${timeText}).`);
        hideToastAfter(1500);
      });
  }

  function setAutoRefresh(enabled) {
    state.auto = !!enabled;
    if (state.toggleEl) {
      state.toggleEl.checked = state.auto;
    }
    persistAutoPreference(state.auto);
    if (state.auto && state.lastPayload && !state.refreshing) {
      triggerRefresh('auto');
    }
  }

  function handleDataRefresh(payload) {
    showToast(payload);
    if (state.auto) {
      triggerRefresh('auto');
    }
  }

  function startEventStream() {
    if (disableSse) {
      console.info('Live updates disabled by configuration.');
      return;
    }
    if (!handlers.size) {
      return;
    }
    if (state.eventSource || state.sseAttempted) {
      return;
    }
    state.sseAttempted = true;

    if (!('EventSource' in window)) {
      console.warn('EventSource not supported in this browser; live updates disabled.');
      return;
    }

    const source = new EventSource('/api/events');
    state.eventSource = source;
    let errorCount = 0;

    source.onmessage = (event) => {
      if (!event || typeof event.data !== 'string' || !event.data.trim()) {
        return;
      }
      try {
        const payload = JSON.parse(event.data);
        if (payload && payload.type === 'data_refresh') {
          handleDataRefresh(payload);
        }
      } catch (err) {
        console.warn('Failed to parse SSE payload', err);
      }
    };

    source.onerror = () => {
      console.warn('EventSource connection issue detected. The browser will attempt to reconnect automatically.');
      errorCount += 1;
      if (maxSseRetries > 0 && errorCount >= maxSseRetries) {
        console.warn('Disabling live updates after repeated connection failures.');
        try {
          source.close();
        } catch (err) {
          /* ignore */
        }
        state.eventSource = null;
      }
    };

    window.addEventListener('beforeunload', () => {
      try {
        source.close();
      } catch (err) {
        /* ignore */
      }
    }, { once: true });
  }

  function init() {
    state.toastEl = document.getElementById('liveRefreshToast');
    if (!state.toastEl) return;

    if (window.bootstrap?.Toast) {
      state.toast = bootstrap.Toast.getOrCreateInstance(state.toastEl, { autohide: false });
    }

    state.messageEl = state.toastEl.querySelector('[data-refresh-message]');
    state.refreshBtn = state.toastEl.querySelector('[data-action="refresh-now"]');
    state.toggleEl = state.toastEl.querySelector('[data-auto-refresh]');

    state.auto = readAutoPreference();
    if (state.toggleEl) {
      state.toggleEl.checked = state.auto;
      state.toggleEl.addEventListener('change', (event) => {
        setAutoRefresh(event.target.checked);
      });
    }

    if (state.refreshBtn) {
      state.refreshBtn.addEventListener('click', () => triggerRefresh('manual'));
    }

    startEventStream();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
