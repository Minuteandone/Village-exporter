(() => {
  'use strict';

  const API_ORIGIN = 'https://theaidigest.org/village';
  const RELAY_PREFIX = 'https://r.jina.ai/';
  const SCREENSHOT_CDN = 'https://village-screenshots-sfo2.sfo2.cdn.digitaloceanspaces.com/computer-use-turns';
  const COMPUTER_ACTION_PATTERN = /(COMPUTER|SCREENSHOT|MOUSE|KEYBOARD|BROWSER)_?(USE|TURN|ACTION|SESSION|START|STOP)?/i;
  const SESSION_KEYS = ['sessions', 'computerUseSessions', 'computer_use_sessions'];
  const TURN_KEYS = ['turns', 'computerUseTurns', 'computer_use_turns', 'computerTurns', 'computer_turns'];
  const nativeFetch = window.fetch.bind(window);
  const nativeMapSet = Map.prototype.set;

  let villageId = null;
  let bypassPrefetchGate = false;
  let prefetching = false;
  let exportDayQueue = [];
  const payloadByDay = new Map();
  const warningByDay = new Map();
  const dayByFileMap = new WeakMap();

  function parseRelay(text) {
    const marker = 'Markdown Content:';
    const index = text.indexOf(marker);
    return JSON.parse((index >= 0 ? text.slice(index + marker.length) : text).trim());
  }

  function json(value) {
    return JSON.stringify(value, null, 2) + '\n';
  }

  function jsonl(values) {
    return values.map((value) => JSON.stringify(value)).join('\n') + (values.length ? '\n' : '');
  }

  function parseJsonArray(value) {
    if (typeof value !== 'string') return [];
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  function objects(value) {
    return Array.isArray(value) ? value.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) : [];
  }

  function sessionsFromPayload(payload) {
    if (Array.isArray(payload)) return objects(payload);
    if (!payload || typeof payload !== 'object') return [];
    for (const key of SESSION_KEYS) {
      if (Array.isArray(payload[key])) return objects(payload[key]);
    }
    if (payload.data && typeof payload.data === 'object') {
      for (const key of SESSION_KEYS) {
        if (Array.isArray(payload.data[key])) return objects(payload.data[key]);
      }
    }
    return [];
  }

  function turnRows(session) {
    for (const key of TURN_KEYS) {
      if (Array.isArray(session?.[key])) return session[key];
    }
    if (session?.data && typeof session.data === 'object') {
      for (const key of TURN_KEYS) {
        if (Array.isArray(session.data[key])) return session.data[key];
      }
    }
    return [];
  }

  function flattenTurns(sessions, payload = null) {
    const result = [];
    const seen = new Set();

    function addRows(rows, fallbackSessionId = '') {
      rows.forEach((raw, index) => {
        let turn;
        if (raw && typeof raw === 'object' && !Array.isArray(raw)) turn = { ...raw };
        else if (typeof raw === 'string' || typeof raw === 'number') turn = { id: String(raw) };
        else return;
        const sessionId = String(turn.computerUseSessionId ?? turn.sessionId ?? turn.session_id ?? fallbackSessionId ?? '');
        const turnId = String(turn.id ?? turn.turnId ?? turn.turn_id ?? '');
        const identity = `${sessionId}\u0000${turnId || `index:${index}`}`;
        if (seen.has(identity)) return;
        seen.add(identity);
        if (turn.computerUseSessionId == null) turn.computerUseSessionId = sessionId || null;
        result.push(turn);
      });
    }

    for (const session of sessions) {
      const sessionId = String(session?.id ?? session?.sessionId ?? session?.session_id ?? '');
      addRows(turnRows(session), sessionId);
    }

    const containers = [];
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
      containers.push(payload);
      if (payload.data && typeof payload.data === 'object' && !Array.isArray(payload.data)) containers.push(payload.data);
    }
    for (const container of containers) {
      for (const key of TURN_KEYS) {
        if (Array.isArray(container[key])) {
          addRows(container[key]);
          break;
        }
      }
    }
    return result;
  }

  function normalizeTurns(sessions, payload = null) {
    const sessionMap = new Map();
    for (const session of sessions) {
      const sid = String(session?.id ?? session?.sessionId ?? session?.session_id ?? '');
      if (sid) sessionMap.set(sid, session);
    }
    const steps = flattenTurns(sessions, payload).map((raw) => {
      const sessionId = String(raw?.computerUseSessionId ?? raw?.sessionId ?? raw?.session_id ?? '');
      const session = sessionMap.get(sessionId) || {};
      const id = raw?.id ?? raw?.turnId ?? raw?.turn_id ?? null;
      const agentId = raw?.agentId ?? raw?.agent_id ?? session?.agentId ?? session?.agent_id ?? null;
      const action = raw?.agentAction ?? raw?.agent_action ?? raw?.action ?? null;
      return {
        stepIndex: 0,
        id,
        computerUseSessionId: sessionId || null,
        agentId,
        createdAt: raw?.createdAt ?? raw?.created_at ?? raw?.timestamp ?? raw?.ts ?? null,
        action,
        screenshotUrl: id ? `${SCREENSHOT_CDN}/${id}.png` : null,
        rawTurn: raw,
      };
    });
    steps.sort((a, b) => String(a.createdAt || '').localeCompare(String(b.createdAt || '')) || String(a.id || '').localeCompare(String(b.id || '')));
    steps.forEach((step, index) => { step.stepIndex = index + 1; });
    return steps;
  }

  function markerRows(activities, rawEvents) {
    const rawById = new Map(objects(rawEvents).filter((event) => event.id != null).map((event) => [String(event.id), event]));
    return objects(activities)
      .filter((activity) => activity?.category === 'computer' || COMPUTER_ACTION_PATTERN.test(String(activity?.actionType || '')))
      .map((activity, index) => ({
        markerIndex: index + 1,
        id: activity?.id ?? null,
        eventIndex: activity?.eventIndex ?? null,
        createdAt: activity?.createdAt ?? null,
        actionType: activity?.actionType ?? null,
        agentId: activity?.agentId ?? null,
        agentName: activity?.agentName ?? null,
        roomId: activity?.roomId ?? null,
        roomName: activity?.roomName ?? null,
        data: activity?.data ?? {},
        rawEvent: rawById.get(String(activity?.id)) ?? null,
      }));
  }

  function dbOpen() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) return resolve(null);
      const req = indexedDB.open('ai-village-real-computer-use', 1);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains('days')) req.result.createObjectStore('days');
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function cacheGet(key) {
    try {
      const db = await dbOpen();
      if (!db) return null;
      return await new Promise((resolve) => {
        const tx = db.transaction('days', 'readonly');
        const req = tx.objectStore('days').get(key);
        req.onsuccess = () => resolve(req.result ?? null);
        req.onerror = () => resolve(null);
      });
    } catch {
      return null;
    }
  }

  async function cacheSet(key, value) {
    try {
      const db = await dbOpen();
      if (!db) return;
      await new Promise((resolve) => {
        const tx = db.transaction('days', 'readwrite');
        tx.objectStore('days').put(value, key);
        tx.oncomplete = resolve;
        tx.onerror = resolve;
      });
    } catch {
      // Archive export should still work if browser storage is unavailable.
    }
  }

  function requestedDelayMs() {
    const input = document.getElementById('request-delay');
    return Math.round(Math.max(0.75, Number(input?.value) || 1.25) * 1000);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  let lastComputerFetch = 0;
  async function pacedFetchJson(path) {
    const wait = requestedDelayMs() - (performance.now() - lastComputerFetch);
    if (wait > 0) await sleep(wait);
    lastComputerFetch = performance.now();
    let lastError;
    for (let attempt = 0; attempt <= 5; attempt += 1) {
      try {
        const response = await nativeFetch(`${RELAY_PREFIX}${API_ORIGIN}${path}`, { headers: { Accept: 'text/plain' }, cache: 'no-store' });
        if (!response.ok) {
          const error = new Error(`computer-use relay returned HTTP ${response.status}`);
          error.status = response.status;
          error.retryAfter = response.headers.get('Retry-After');
          throw error;
        }
        return parseRelay(await response.text());
      } catch (error) {
        lastError = error;
        if (attempt >= 5 || (error.status && ![408, 425, 429, 500, 502, 503, 504].includes(error.status))) break;
        let delay = Math.min(60_000, (2 ** attempt) * 1000 + 250 + Math.random() * 750);
        if (error.retryAfter && Number.isFinite(Number(error.retryAfter))) delay = Math.max(delay, Number(error.retryAfter) * 1000);
        await sleep(delay);
      }
    }
    throw lastError || new Error('computer-use request failed');
  }

  async function resolveVillageId() {
    if (villageId) return villageId;
    const slug = document.getElementById('village-slug')?.value?.trim();
    if (!slug) throw new Error('Village slug is empty.');
    const payload = await pacedFetchJson(`/api/villages?slug=${encodeURIComponent(slug)}`);
    if (!payload?.id) throw new Error(`Could not resolve village “${slug}”.`);
    villageId = String(payload.id);
    return villageId;
  }

  function latestAvailableDay() {
    const values = [...document.querySelectorAll('#day-list input[type="checkbox"]')].map((input) => input.value).filter(Boolean).sort();
    return values.at(-1) || null;
  }

  function logLine(message) {
    const log = document.getElementById('export-log');
    if (!log) return;
    const row = document.createElement('div');
    row.className = 'log-row';
    row.textContent = `${new Date().toLocaleTimeString()}  ${message}`;
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
  }

  async function preloadComputerUse(days) {
    const id = await resolveVillageId();
    const latest = latestAvailableDay();
    const refreshAll = Boolean(document.getElementById('refresh-cache')?.checked);
    lastComputerFetch = 0;
    for (let index = 0; index < days.length; index += 1) {
      const day = days[index];
      const key = `${id}:${day}`;
      const forceFresh = refreshAll || day === latest;
      if (!forceFresh) {
        const cached = await cacheGet(key);
        if (cached?.payload !== undefined) {
          payloadByDay.set(day, cached.payload);
          warningByDay.set(day, cached.warning || null);
          logLine(`${day}: reused cached real computer-use sessions/turns.`);
          continue;
        }
      }
      const status = document.getElementById('export-status');
      if (status) status.textContent = `Fetching ${day} · real computer-use sessions…`;
      try {
        const payload = await pacedFetchJson(`/api/computer-use-sessions?villageId=${encodeURIComponent(id)}&date=${encodeURIComponent(day)}`);
        const warning = payload?.error ? `Computer-use session endpoint reported: ${payload.error}` : null;
        payloadByDay.set(day, payload);
        warningByDay.set(day, warning);
        await cacheSet(key, { savedAt: Date.now(), payload, warning });
        const sessions = sessionsFromPayload(payload);
        const turns = flattenTurns(sessions, payload);
        logLine(`${day}: loaded ${sessions.length.toLocaleString()} computer sessions and ${turns.length.toLocaleString()} real turn records.`);
      } catch (error) {
        const warning = `Computer-use session endpoint failed: ${error.message}`;
        payloadByDay.set(day, { sessions: [] });
        warningByDay.set(day, warning);
        logLine(`${day}: ${warning}`);
      }
    }
  }

  // Learn the village id from the exporter's existing lookup without adding a request.
  window.fetch = async function observedFetch(input, init) {
    const response = await nativeFetch(input, init);
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.includes('/api/villages?slug=')) {
      response.clone().text().then((body) => {
        try {
          const value = url.includes(RELAY_PREFIX) ? parseRelay(body) : JSON.parse(body);
          if (value?.id) villageId = String(value.id);
        } catch {
          // The pre-export fallback can resolve it if this response is unreadable.
        }
      });
    }
    return response;
  };

  // Gate export long enough to load the single bulk computer-use response for
  // each selected day. Then replay the click so the existing exporter proceeds.
  document.addEventListener('click', async (event) => {
    const button = event.target?.closest?.('#export-selected');
    if (!button || bypassPrefetchGate) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (prefetching) return;
    prefetching = true;
    const days = [...document.querySelectorAll('#day-list input[type="checkbox"]:checked')].map((input) => input.value).sort();
    exportDayQueue = [...days];
    try {
      await preloadComputerUse(days);
    } finally {
      prefetching = false;
    }
    bypassPrefetchGate = true;
    button.click();
    bypassPrefetchGate = false;
  }, true);

  function dayFor(map, key) {
    let day = dayByFileMap.get(map);
    if (!day && typeof key === 'string' && key.startsWith('event-pages/page-001.json')) {
      day = exportDayQueue.shift() || null;
      if (day) dayByFileMap.set(map, day);
    }
    return day;
  }

  function countsFor(map, day) {
    const payload = payloadByDay.get(day);
    const sessions = sessionsFromPayload(payload);
    const turns = normalizeTurns(sessions, payload);
    const markers = parseJsonArray(map.get('computer-session-markers.json'));
    return { sessions, turns, markers };
  }

  Map.prototype.set = function realComputerUseSet(key, value) {
    const day = dayFor(this, key);

    if (key === 'activities.json' && typeof value === 'string' && day) {
      const result = nativeMapSet.call(this, key, value);
      const markers = markerRows(parseJsonArray(value), parseJsonArray(this.get('events.raw.json')));
      nativeMapSet.call(this, 'computer-session-markers.json', json(markers));
      nativeMapSet.call(this, 'computer-session-markers.jsonl', jsonl(markers));

      const payload = payloadByDay.get(day) ?? { sessions: [] };
      const sessions = sessionsFromPayload(payload);
      const rawTurns = flattenTurns(sessions, payload);
      const steps = normalizeTurns(sessions, payload);
      nativeMapSet.call(this, 'computer-use-sessions.raw.json', json(payload));
      nativeMapSet.call(this, 'computer-use-sessions.json', json(sessions));
      nativeMapSet.call(this, 'computer-use-turns.raw.jsonl', jsonl(rawTurns));
      nativeMapSet.call(this, 'computer-steps.json', json(steps));
      nativeMapSet.call(this, 'computer-steps.jsonl', jsonl(steps));
      return result;
    }

    if (key === 'timeline.jsonl' && typeof value === 'string' && day) {
      const items = value.split('\n').filter(Boolean).flatMap((line) => {
        try {
          const item = JSON.parse(line);
          if (item?.category === 'computer' || COMPUTER_ACTION_PATTERN.test(String(item?.actionType || ''))) item.kind = 'computer-session-marker';
          return [item];
        } catch {
          return [];
        }
      });
      const { turns } = countsFor(this, day);
      items.push(...turns.map((turn) => ({ kind: 'computer-use-turn', ...turn })));
      items.sort((a, b) => String(a.createdAt || '').localeCompare(String(b.createdAt || '')) || (Number.isInteger(a.eventIndex) ? a.eventIndex : Number.MAX_SAFE_INTEGER) - (Number.isInteger(b.eventIndex) ? b.eventIndex : Number.MAX_SAFE_INTEGER) || String(a.id || '').localeCompare(String(b.id || '')));
      value = jsonl(items);
    }

    if (key === 'summary.md' && typeof value === 'string' && day) {
      const { sessions, turns, markers } = countsFor(this, day);
      value = value.replace(/^- Computer steps: \*\*[^\n]+\*\*\n?/m, '');
      value = value.replace(
        /(- Non-chat activities: \*\*[^\n]+\*\*)/,
        `$1\n- Computer session markers: **${markers.length.toLocaleString()}**\n- Computer-use sessions: **${sessions.length.toLocaleString()}**\n- Real computer-use turns: **${turns.length.toLocaleString()}**`,
      );
      value = value.replace(
        'Computer/browser actions are kept when they exist in the public event feed; the exporter does not intentionally filter them out.',
        'High-level computer-related `/api/events` rows are stored as `computer-session-markers.*`. Real `computer-steps.*` rows come from `/api/computer-use-sessions`. Screenshot URLs are referenced by turn id; images are not bulk-downloaded by default.',
      );
      const warning = warningByDay.get(day);
      if (warning && !value.includes(warning)) value += `\n## Computer-use warning\n\n- ${warning}\n`;
    }

    if (key === 'manifest.json' && typeof value === 'string' && day) {
      try {
        const manifest = JSON.parse(value);
        const { sessions, turns, markers } = countsFor(this, day);
        manifest.schemaVersion = Math.max(Number(manifest.schemaVersion) || 1, 3);
        manifest.counts = manifest.counts || {};
        manifest.counts.computerSessionMarkers = markers.length;
        manifest.counts.computerUseSessions = sessions.length;
        manifest.counts.computerUseTurns = turns.length;
        manifest.counts.computerSteps = turns.length;
        manifest.sources = manifest.sources || {};
        manifest.sources.computerUseSessions = '/api/computer-use-sessions?villageId=…&date=YYYY-MM-DD';
        const warning = warningByDay.get(day);
        if (warning) {
          manifest.warnings = Array.isArray(manifest.warnings) ? manifest.warnings : [];
          if (!manifest.warnings.includes(warning)) manifest.warnings.push(warning);
        }
        value = json(manifest);
      } catch {
        // Keep the original manifest if it is unexpectedly unreadable.
      }
    }

    return nativeMapSet.call(this, key, value);
  };
})();
