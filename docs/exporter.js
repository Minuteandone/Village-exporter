(() => {
  'use strict';

  const API_ORIGIN = 'https://theaidigest.org/village';
  const RELAY_PREFIX = 'https://r.jina.ai/';
  const MIN_DELAY_MS = 750;
  const DEFAULT_DELAY_MS = 1250;
  const MAX_EVENT_PAGES = 100;
  const CHAT_TYPES = new Set(['AGENT_TALK', 'USER_TALK']);
  const enc = new TextEncoder();

  const $ = (id) => document.getElementById(id);
  const ui = {
    slug: $('village-slug'),
    load: $('load-days'),
    dayTools: $('day-tools'),
    dayList: $('day-list'),
    selectionSummary: $('selection-summary'),
    selectAll: $('select-all-days'),
    selectLastFive: $('select-last-five'),
    clearDays: $('clear-days'),
    memoryMode: $('memory-mode'),
    delay: $('request-delay'),
    refresh: $('refresh-cache'),
    export: $('export-selected'),
    cancel: $('cancel-export'),
    progress: $('export-progress'),
    status: $('export-status'),
    log: $('export-log'),
    cacheNote: $('cache-note'),
  };

  let village = null;
  let controller = null;
  let running = false;
  let lastNetworkAt = 0;
  let networkRequests = 0;
  let cacheHits = 0;

  function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));
      const timer = setTimeout(resolve, ms);
      signal?.addEventListener('abort', () => {
        clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
      }, { once: true });
    });
  }

  function log(message, kind = '') {
    const row = document.createElement('div');
    row.className = `log-row ${kind}`.trim();
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    row.textContent = `${time}  ${message}`;
    ui.log.appendChild(row);
    ui.log.scrollTop = ui.log.scrollHeight;
  }

  function setStatus(text) {
    ui.status.textContent = text;
  }

  function setProgress(value, max = 100) {
    ui.progress.max = max;
    ui.progress.value = Math.min(value, max);
  }

  function safeSlug(value, fallback = 'archive') {
    const out = String(value || '')
      .normalize('NFKD')
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '');
    return out || fallback;
  }

  function json(value) {
    return JSON.stringify(value, null, 2) + '\n';
  }

  function jsonl(values) {
    return values.map((value) => JSON.stringify(value)).join('\n') + (values.length ? '\n' : '');
  }

  function parseRelayedJson(text) {
    const marker = 'Markdown Content:';
    const index = text.indexOf(marker);
    const body = (index >= 0 ? text.slice(index + marker.length) : text).trim();
    try {
      return JSON.parse(body);
    } catch (error) {
      throw new Error('The public data relay returned an unreadable response.');
    }
  }

  function requestedDelayMs() {
    const seconds = Math.max(MIN_DELAY_MS / 1000, Number(ui.delay.value) || DEFAULT_DELAY_MS / 1000);
    return Math.round(seconds * 1000);
  }

  async function pace(signal) {
    const remaining = requestedDelayMs() - (performance.now() - lastNetworkAt);
    if (remaining > 0) await sleep(remaining, signal);
    lastNetworkAt = performance.now();
  }

  function retryDelay(attempt, retryAfterHeader) {
    if (retryAfterHeader) {
      const seconds = Number(retryAfterHeader);
      if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
      const date = Date.parse(retryAfterHeader);
      if (Number.isFinite(date)) return Math.max(0, date - Date.now());
    }
    return Math.min(60_000, (2 ** attempt) * 1000 + 250 + Math.random() * 1000);
  }

  function dbOpen() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) return resolve(null);
      const req = indexedDB.open('ai-village-mass-exporter', 1);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains('responses')) {
          req.result.createObjectStore('responses');
        }
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
        const tx = db.transaction('responses', 'readonly');
        const req = tx.objectStore('responses').get(key);
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
        const tx = db.transaction('responses', 'readwrite');
        tx.objectStore('responses').put({ savedAt: Date.now(), value }, key);
        tx.oncomplete = resolve;
        tx.onerror = resolve;
      });
    } catch {
      // Cache failure should never block an export.
    }
  }

  async function requestJson(path, { signal, cache = false, forceFresh = false } = {}) {
    const cacheKey = `${API_ORIGIN}${path}`;
    if (cache && !forceFresh && !ui.refresh.checked) {
      const cached = await cacheGet(cacheKey);
      if (cached?.value !== undefined) {
        cacheHits += 1;
        return cached.value;
      }
    }

    const target = `${API_ORIGIN}${path}`;
    const url = `${RELAY_PREFIX}${target}`;
    let lastError = null;

    for (let attempt = 0; attempt <= 5; attempt += 1) {
      await pace(signal);
      try {
        networkRequests += 1;
        const response = await fetch(url, {
          signal,
          headers: { Accept: 'text/plain' },
          cache: 'no-store',
        });
        if (!response.ok) {
          const error = new Error(`Relay returned HTTP ${response.status}.`);
          error.status = response.status;
          error.retryAfter = response.headers.get('Retry-After');
          throw error;
        }
        const value = parseRelayedJson(await response.text());
        if (cache) await cacheSet(cacheKey, value);
        return value;
      } catch (error) {
        if (signal?.aborted) throw error;
        lastError = error;
        const status = error?.status;
        const retryable = status === undefined || [408, 425, 429, 500, 502, 503, 504].includes(status);
        if (!retryable || attempt >= 5) break;
        const wait = retryDelay(attempt, error?.retryAfter);
        log(`Request failed${status ? ` (${status})` : ''}; retrying after ${(wait / 1000).toFixed(1)}s…`, 'warn');
        await sleep(wait, signal);
      }
    }
    throw lastError || new Error('Request failed.');
  }

  function toList(value) {
    return Array.isArray(value) ? value.filter((item) => item && typeof item === 'object') : [];
  }

  function text(value) {
    return typeof value === 'string' && value.trim() ? value : null;
  }

  function sortEvents(events) {
    return [...events].sort((a, b) => {
      const ai = Number.isInteger(a?.eventIndex) ? a.eventIndex : Number.MAX_SAFE_INTEGER;
      const bi = Number.isInteger(b?.eventIndex) ? b.eventIndex : Number.MAX_SAFE_INTEGER;
      if (ai !== bi) return ai - bi;
      return String(a?.createdAt || '').localeCompare(String(b?.createdAt || '')) || String(a?.id || '').localeCompare(String(b?.id || ''));
    });
  }

  function actionCategory(action) {
    if (action === 'PAUSE') return 'pause';
    if (action === 'CONSOLIDATE') return 'consolidation';
    if (action === 'ENTER_ROOM' || action === 'LEAVE_ROOM') return 'room';
    if (action === 'SEARCH_HISTORY') return 'search';
    if (action.includes('HUMAN')) return 'human-helper';
    if (action.includes('OUTREACH')) return 'outreach';
    if (action.includes('GOOGLE_SIGN_IN')) return 'auth';
    if (/(COMPUTER|SCREENSHOT|MOUSE|KEYBOARD|BROWSER)/i.test(action)) return 'computer';
    return 'other';
  }

  function mapMessages(events, agents) {
    const names = new Map(agents.filter((a) => a?.id).map((a) => [String(a.id), a.name]));
    const messages = [];
    for (const event of events) {
      const data = event?.data && typeof event.data === 'object' ? event.data : {};
      const action = data.actionType;
      if (!CHAT_TYPES.has(action) || !text(data.content) || !text(data.roomId)) continue;
      const isAgent = action === 'AGENT_TALK';
      const speakerId = text(data.speakerId) || `unknown-${event?.id || 'event'}`;
      messages.push({
        id: text(data.messageId) || event?.id,
        eventIndex: event?.eventIndex,
        speakerId,
        speakerName: isAgent ? (names.get(speakerId) || 'Unknown agent') : (text(data.speakerName) || 'Human visitor'),
        speakerKind: isAgent ? 'agent' : 'human',
        content: data.content,
        roomId: data.roomId,
        createdAt: event?.createdAt,
        rawEventId: event?.id,
      });
    }
    return messages.sort((a, b) => String(a.createdAt || '').localeCompare(String(b.createdAt || '')) || (a.eventIndex || 0) - (b.eventIndex || 0));
  }

  function mapActivities(events, agents) {
    const names = new Map(agents.filter((a) => a?.id).map((a) => [String(a.id), a.name]));
    const out = [];
    for (const event of sortEvents(events)) {
      const data = event?.data && typeof event.data === 'object' ? event.data : {};
      const action = text(data.actionType);
      if (!action || CHAT_TYPES.has(action)) continue;
      const agentId = text(data.agentId) || text(data.speakerId);
      out.push({
        id: event?.id,
        eventIndex: event?.eventIndex,
        createdAt: event?.createdAt,
        actionType: action,
        category: actionCategory(action),
        agentId,
        agentName: names.get(agentId || '') || text(data.speakerName),
        roomId: text(data.roomId),
        roomName: text(data.roomName),
        data,
      });
    }
    return out;
  }

  function helperContextItems(sessions, agents) {
    const names = new Map(agents.filter((a) => a?.id).map((a) => [String(a.id), a.name]));
    const items = [];
    for (const session of sessions) {
      const sid = String(session?.id || 'session');
      const agentId = String(session?.agentId || '');
      const agentName = names.get(agentId) || 'Unknown agent';
      if (text(session?.userIntro)) {
        items.push({ kind: 'human-helper-context', id: `${sid}-intro`, createdAt: session.createdAt, sessionId: sid, speakerKind: 'human', speakerName: 'Human helper', content: session.userIntro });
      }
      for (const turn of Array.isArray(session?.turns) ? session.turns : []) {
        if (!turn || typeof turn !== 'object') continue;
        const action = turn.agentAction && typeof turn.agentAction === 'object' ? turn.agentAction : {};
        if (text(action.instructions)) {
          items.push({ kind: 'human-helper-context', id: `${turn.id || sid}-agent`, createdAt: turn.createdAt, sessionId: sid, speakerKind: 'agent', speakerName: agentName, content: action.instructions });
        }
        if (text(turn.userResponse)) {
          items.push({ kind: 'human-helper-context', id: `${turn.id || sid}-human`, createdAt: turn.updatedAt || turn.createdAt, sessionId: sid, speakerKind: 'human', speakerName: 'Human helper', content: turn.userResponse });
        }
      }
    }
    return items.sort((a, b) => String(a.createdAt || '').localeCompare(String(b.createdAt || '')));
  }

  function buildTimeline(events, agents, sessions) {
    const messagesByEvent = new Map(mapMessages(events, agents).map((m) => [m.rawEventId, m]));
    const timeline = [];
    for (const event of sortEvents(events)) {
      const message = messagesByEvent.get(event?.id);
      if (message) {
        timeline.push({ kind: 'message', ...message });
      } else {
        const data = event?.data && typeof event.data === 'object' ? event.data : {};
        const action = text(data.actionType);
        timeline.push({
          kind: action ? 'activity' : 'unknown-event',
          id: event?.id,
          eventIndex: event?.eventIndex,
          createdAt: event?.createdAt,
          actionType: action,
          category: action ? actionCategory(action) : 'unknown',
          data,
        });
      }
    }
    timeline.push(...helperContextItems(sessions, agents));
    return timeline.sort((a, b) => {
      const date = String(a.createdAt || '').localeCompare(String(b.createdAt || ''));
      if (date) return date;
      const ai = Number.isInteger(a.eventIndex) ? a.eventIndex : Number.MAX_SAFE_INTEGER;
      const bi = Number.isInteger(b.eventIndex) ? b.eventIndex : Number.MAX_SAFE_INTEGER;
      return ai - bi || String(a.id || '').localeCompare(String(b.id || ''));
    });
  }

  function discoverRooms(events, knownRooms) {
    const known = new Set(knownRooms.filter((r) => r?.id).map((r) => String(r.id)));
    const rooms = new Map(knownRooms.filter((r) => r?.id).map((r) => [String(r.id), { ...r }]));
    for (const event of events) {
      const data = event?.data && typeof event.data === 'object' ? event.data : {};
      for (const [idKey, nameKey] of [['roomId', 'roomName'], ['previousRoomId', 'previousRoomName']]) {
        const id = text(data[idKey]);
        if (!id) continue;
        const room = rooms.get(id) || { id };
        if (!room.name && text(data[nameKey])) room.name = data[nameKey];
        if (!known.has(id)) room.discoveredFromHistoricalEvent = true;
        rooms.set(id, room);
      }
    }
    return [...rooms.values()].sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')) || String(a.id || '').localeCompare(String(b.id || '')));
  }

  function actionCounts(events) {
    const counts = new Map();
    for (const event of events) {
      const data = event?.data && typeof event.data === 'object' ? event.data : {};
      const action = text(data.actionType) || '(missing actionType)';
      counts.set(action, (counts.get(action) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }

  function addDays(dateString, amount) {
    const [y, m, d] = dateString.split('-').map(Number);
    const date = new Date(Date.UTC(y, m - 1, d + amount));
    return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')}`;
  }

  function zonedMidnightUtcMs(dateString, timeZone = 'America/Los_Angeles') {
    const [year, month, day] = dateString.split('-').map(Number);
    let guess = Date.UTC(year, month - 1, day, 0, 0, 0);
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    });
    for (let i = 0; i < 3; i += 1) {
      const parts = Object.fromEntries(formatter.formatToParts(new Date(guess)).filter((p) => p.type !== 'literal').map((p) => [p.type, p.value]));
      const represented = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute), Number(parts.second));
      const target = Date.UTC(year, month - 1, day, 0, 0, 0);
      guess += target - represented;
    }
    return guess;
  }

  async function loadEventPages(villageId, day, signal, latestDate) {
    const pages = [];
    const events = [];
    for (let page = 1; page <= MAX_EVENT_PAGES; page += 1) {
      setStatus(`Fetching ${day} · event page ${page}…`);
      const path = `/api/events?villageId=${encodeURIComponent(villageId)}&date=${encodeURIComponent(day)}&page=${page}`;
      const payload = await requestJson(path, { signal, cache: true, forceFresh: day === latestDate });
      if (!payload || typeof payload !== 'object') throw new Error(`Event page ${page} returned an invalid payload.`);
      if (payload.error) throw new Error(String(payload.error));
      const batch = Array.isArray(payload.events) ? payload.events.filter((item) => item && typeof item === 'object') : [];
      pages.push(payload);
      events.push(...batch);
      if (!payload.hasMore) return { pages, events };
      if (page === MAX_EVENT_PAGES) throw new Error(`The day still had more event pages after the ${MAX_EVENT_PAGES}-page safety cap.`);
    }
    return { pages, events };
  }

  async function loadHumanSessions(villageId, day, signal, latestDate) {
    const path = `/api/human-use-sessions?villageId=${encodeURIComponent(villageId)}&date=${encodeURIComponent(day)}`;
    try {
      const payload = await requestJson(path, { signal, cache: true, forceFresh: day === latestDate });
      if (payload?.error) return { sessions: [], warning: `Human-use session endpoint reported: ${payload.error}` };
      return { sessions: toList(payload?.sessions), warning: null };
    } catch (error) {
      return { sessions: [], warning: `Human-use session endpoint failed: ${error.message}` };
    }
  }

  function memoryCandidateIds(mode, events, agents) {
    if (mode === 'none') return [];
    if (mode === 'all-agents') return agents.filter((a) => a?.id).map((a) => String(a.id));
    const ids = new Set();
    for (const event of events) {
      const data = event?.data && typeof event.data === 'object' ? event.data : {};
      if (data.actionType !== 'CONSOLIDATE') continue;
      const id = text(data.agentId) || text(data.speakerId);
      if (id) ids.add(id);
    }
    return [...ids].sort();
  }

  async function loadMemories(day, events, agents, signal, latestDate) {
    const mode = ui.memoryMode.value;
    const ids = memoryCandidateIds(mode, events, agents);
    if (!ids.length) return { memories: {}, warnings: [] };
    const dayStart = zonedMidnightUtcMs(day);
    const dayEnd = zonedMidnightUtcMs(addDays(day, 1));
    const cutoff = dayEnd + 1000;
    const memories = {};
    const warnings = [];
    for (let i = 0; i < ids.length; i += 1) {
      const agentId = ids[i];
      setStatus(`Fetching ${day} · memory ${i + 1}/${ids.length}…`);
      try {
        const path = `/api/agent/${encodeURIComponent(agentId)}/memories?createdAt=${encodeURIComponent(String(cutoff))}`;
        const payload = await requestJson(path, { signal, cache: true, forceFresh: day === latestDate });
        const versions = toList(payload?.memories).sort((a, b) => String(b.createdAt || '').localeCompare(String(a.createdAt || '')));
        if (mode === 'all-agents') {
          const inDay = [];
          let previous = null;
          for (const version of versions) {
            const created = Date.parse(version.createdAt || '');
            if (Number.isFinite(created) && created >= dayStart && created < dayEnd) inDay.push(version);
            else if (Number.isFinite(created) && created < dayStart && !previous) previous = version;
          }
          memories[agentId] = previous ? [...inDay, previous] : inDay;
        } else {
          memories[agentId] = versions;
        }
      } catch (error) {
        warnings.push(`Memory fetch failed for agent ${agentId}: ${error.message}`);
      }
    }
    return { memories, warnings };
  }

  function summaryMarkdown(villageInfo, day, events, messages, activities, sessions, memories, warnings) {
    const roomNames = new Map(villageInfo.rooms.filter((r) => r?.id).map((r) => [String(r.id), String(r.name || r.id)]));
    const speakers = new Map();
    const rooms = new Map();
    for (const message of messages) {
      const speaker = message.speakerName || message.speakerId || 'unknown';
      speakers.set(speaker, (speakers.get(speaker) || 0) + 1);
      const room = roomNames.get(String(message.roomId)) || String(message.roomId || 'unknown');
      rooms.set(room, (rooms.get(room) || 0) + 1);
    }
    const top = (map) => [...map.entries()].sort((a, b) => b[1] - a[1]);
    const topSpeakers = top(speakers).slice(0, 20);
    const topRooms = top(rooms);
    const lines = [
      `# AI Village day export — ${day}`,
      '',
      `Village: **${villageInfo.name}** (\`${villageInfo.slug}\`)`,
      '',
      `- Raw events: **${events.length.toLocaleString()}**`,
      `- Chat messages: **${messages.length.toLocaleString()}**`,
      `- Non-chat activities: **${activities.length.toLocaleString()}**`,
      `- Human-use sessions: **${sessions.length.toLocaleString()}**`,
      `- Agents with saved memory context: **${Object.keys(memories).length.toLocaleString()}**`,
      '',
      '## Action types',
      '',
      ...actionCounts(events).map(([name, count]) => `- \`${name}\`: ${count.toLocaleString()}`),
      '',
      '## Most active speakers',
      '',
      ...(topSpeakers.length ? topSpeakers.map(([name, count]) => `- ${name}: ${count.toLocaleString()} messages`) : ['- No messages']),
      '',
      '## Rooms',
      '',
      ...(topRooms.length ? topRooms.map(([name, count]) => `- #${name}: ${count.toLocaleString()} messages`) : ['- No rooms with messages']),
    ];
    if (warnings.length) lines.push('', '## Warnings', '', ...warnings.map((warning) => `- ${warning}`));
    lines.push(
      '',
      '## Archive notes',
      '',
      '`events.raw.json` is the flattened preservation source of truth. `event-pages/` additionally preserves each complete paginated API response wrapper.',
      '',
      'Computer/browser actions are kept when they exist in the public event feed; the exporter does not intentionally filter them out.',
      '',
      'This archive was produced entirely in the browser by AI Village Mass Exporter.',
      '',
    );
    return lines.join('\n');
  }

  async function sha256Hex(bytes) {
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
  }

  async function finalizeDayFiles(files, manifest) {
    const fileNames = [...files.keys(), 'manifest.json', 'checksums.sha256'].sort();
    manifest.files = fileNames;
    files.set('manifest.json', json(manifest));
    const checksumLines = [];
    for (const [name, content] of [...files.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const bytes = typeof content === 'string' ? enc.encode(content) : content;
      checksumLines.push(`${await sha256Hex(bytes)}  ${name}`);
    }
    files.set('checksums.sha256', checksumLines.join('\n') + '\n');
    return files;
  }

  async function buildDayArchive(day, signal) {
    const requestsBefore = networkRequests;
    const cacheBefore = cacheHits;
    const warnings = [];
    const { pages, events } = await loadEventPages(village.id, day, signal, village.latestDate);
    const helper = await loadHumanSessions(village.id, day, signal, village.latestDate);
    if (helper.warning) warnings.push(helper.warning);
    const { memories, warnings: memoryWarnings } = await loadMemories(day, events, village.agents, signal, village.latestDate);
    warnings.push(...memoryWarnings);

    const messages = mapMessages(events, village.agents);
    const activities = mapActivities(events, village.agents);
    const timeline = buildTimeline(events, village.agents, helper.sessions);
    const discoveredRooms = discoverRooms(events, village.rooms);
    const files = new Map();

    pages.forEach((payload, index) => files.set(`event-pages/page-${String(index + 1).padStart(3, '0')}.json`, json(payload)));
    files.set('events.raw.json', json(events));
    files.set('events.raw.jsonl', jsonl(events));
    files.set('messages.json', json(messages));
    files.set('activities.json', json(activities));
    files.set('timeline.jsonl', jsonl(timeline));
    files.set('human-use-sessions.raw.json', json(helper.sessions));
    files.set('agents.json', json(village.agents));
    files.set('rooms.json', json(village.rooms));
    files.set('rooms.discovered.json', json(discoveredRooms));
    files.set('village.json', json({ id: village.id, slug: village.slug, name: village.name, goal: village.goal }));
    for (const [agentId, versions] of Object.entries(memories)) {
      files.set(`memories/${safeSlug(agentId, 'agent')}.json`, json(versions));
    }
    files.set('summary.md', summaryMarkdown(village, day, events, messages, activities, helper.sessions, memories, warnings));

    const manifest = {
      schemaVersion: 2,
      exportType: 'ai-village-complete-day-browser',
      complete: true,
      exportedAt: new Date().toISOString(),
      source: API_ORIGIN,
      transport: 'https://r.jina.ai relay',
      village: { id: village.id, slug: village.slug, name: village.name },
      day,
      counts: {
        rawEvents: events.length,
        eventPages: pages.length,
        messages: messages.length,
        activities: activities.length,
        humanUseSessions: helper.sessions.length,
        memoryAgents: Object.keys(memories).length,
      },
      warnings,
      network: {
        networkRequestsThisDay: networkRequests - requestsBefore,
        cacheHitsThisDay: cacheHits - cacheBefore,
        minimumRequestDelaySeconds: requestedDelayMs() / 1000,
      },
      files: [],
    };
    await finalizeDayFiles(files, manifest);
    return { files, manifest };
  }

  const crcTable = (() => {
    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      table[n] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    let crc = 0xffffffff;
    for (const value of bytes) crc = crcTable[(crc ^ value) & 0xff] ^ (crc >>> 8);
    return (crc ^ 0xffffffff) >>> 0;
  }

  function dosDateTime(date = new Date()) {
    const year = Math.max(1980, date.getFullYear());
    const dosTime = (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2);
    const dosDate = ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate();
    return { dosTime, dosDate };
  }

  function view(size) {
    const buffer = new ArrayBuffer(size);
    return { bytes: new Uint8Array(buffer), data: new DataView(buffer) };
  }

  function makeZip(fileMap) {
    const localParts = [];
    const centralParts = [];
    const entries = [];
    let offset = 0;
    const now = dosDateTime();

    for (const [name, content] of fileMap) {
      const nameBytes = enc.encode(name);
      const dataBytes = typeof content === 'string' ? enc.encode(content) : content;
      if (dataBytes.byteLength > 0xffffffff || offset > 0xffffffff) throw new Error('This browser ZIP exceeded the 4 GB non-ZIP64 safety limit. Use the Python exporter for an archive this large.');
      const crc = crc32(dataBytes);
      const header = view(30);
      header.data.setUint32(0, 0x04034b50, true);
      header.data.setUint16(4, 20, true);
      header.data.setUint16(6, 0x0800, true);
      header.data.setUint16(8, 0, true);
      header.data.setUint16(10, now.dosTime, true);
      header.data.setUint16(12, now.dosDate, true);
      header.data.setUint32(14, crc, true);
      header.data.setUint32(18, dataBytes.byteLength, true);
      header.data.setUint32(22, dataBytes.byteLength, true);
      header.data.setUint16(26, nameBytes.byteLength, true);
      header.data.setUint16(28, 0, true);
      localParts.push(header.bytes, nameBytes, dataBytes);
      entries.push({ nameBytes, size: dataBytes.byteLength, crc, offset });
      offset += header.bytes.byteLength + nameBytes.byteLength + dataBytes.byteLength;
    }

    const centralOffset = offset;
    let centralSize = 0;
    for (const entry of entries) {
      const header = view(46);
      header.data.setUint32(0, 0x02014b50, true);
      header.data.setUint16(4, 20, true);
      header.data.setUint16(6, 20, true);
      header.data.setUint16(8, 0x0800, true);
      header.data.setUint16(10, 0, true);
      header.data.setUint16(12, now.dosTime, true);
      header.data.setUint16(14, now.dosDate, true);
      header.data.setUint32(16, entry.crc, true);
      header.data.setUint32(20, entry.size, true);
      header.data.setUint32(24, entry.size, true);
      header.data.setUint16(28, entry.nameBytes.byteLength, true);
      header.data.setUint16(30, 0, true);
      header.data.setUint16(32, 0, true);
      header.data.setUint16(34, 0, true);
      header.data.setUint16(36, 0, true);
      header.data.setUint32(38, 0, true);
      header.data.setUint32(42, entry.offset, true);
      centralParts.push(header.bytes, entry.nameBytes);
      centralSize += header.bytes.byteLength + entry.nameBytes.byteLength;
    }

    if (entries.length > 0xffff) throw new Error('This browser ZIP has too many files for the non-ZIP64 writer. Use the Python exporter.');
    const end = view(22);
    end.data.setUint32(0, 0x06054b50, true);
    end.data.setUint16(4, 0, true);
    end.data.setUint16(6, 0, true);
    end.data.setUint16(8, entries.length, true);
    end.data.setUint16(10, entries.length, true);
    end.data.setUint32(12, centralSize, true);
    end.data.setUint32(16, centralOffset, true);
    end.data.setUint16(20, 0, true);
    return new Blob([...localParts, ...centralParts, end.bytes], { type: 'application/zip' });
  }

  function downloadBlob(blob, fileName) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  function selectedDays() {
    return [...ui.dayList.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value).sort();
  }

  function updateSelectionSummary() {
    const total = ui.dayList.querySelectorAll('input[type="checkbox"]').length;
    const selected = selectedDays().length;
    ui.selectionSummary.textContent = `${selected} of ${total} day${total === 1 ? '' : 's'} selected`;
    ui.export.disabled = running || selected === 0;
  }

  function renderDays(dates) {
    ui.dayList.replaceChildren();
    for (const day of [...dates].reverse()) {
      const label = document.createElement('label');
      label.className = 'day-option';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = day;
      input.addEventListener('change', updateSelectionSummary);
      const span = document.createElement('span');
      span.textContent = day;
      if (day === village.latestDate) {
        const badge = document.createElement('small');
        badge.textContent = ' latest';
        span.appendChild(badge);
      }
      label.append(input, span);
      ui.dayList.appendChild(label);
    }
    ui.dayTools.hidden = false;
    updateSelectionSummary();
  }

  async function loadVillage() {
    if (running) return;
    const slug = ui.slug.value.trim();
    if (!slug) return;
    ui.load.disabled = true;
    ui.export.disabled = true;
    ui.dayTools.hidden = true;
    ui.dayList.replaceChildren();
    setProgress(0);
    setStatus('Resolving village…');
    log(`Resolving village “${slug}”…`);
    controller = new AbortController();
    try {
      const summary = await requestJson(`/api/villages?slug=${encodeURIComponent(slug)}`, { signal: controller.signal });
      if (!summary?.id || summary?.error) throw new Error(summary?.error || `No village was found for “${slug}”.`);
      const id = String(summary.id);
      setStatus('Loading village metadata…');
      const raw = await requestJson(`/api/villages/${encodeURIComponent(id)}`, { signal: controller.signal });
      const datesPayload = await requestJson(`/api/villages/${encodeURIComponent(id)}/active-dates`, { signal: controller.signal });
      const dates = (Array.isArray(datesPayload?.dates) ? datesPayload.dates : []).filter((date) => typeof date === 'string').sort();
      if (!dates.length) throw new Error('This village has no active dates.');
      village = {
        id,
        slug: String(raw?.slug || summary?.slug || slug),
        name: String(raw?.name || summary?.name || slug),
        goal: typeof raw?.villageGoal === 'string' ? raw.villageGoal : summary?.villageGoal,
        agents: toList(raw?.agents),
        rooms: toList(raw?.chatRooms),
        dates,
        latestDate: dates[dates.length - 1],
      };
      localStorage.setItem('ai-village-exporter-slug', village.slug);
      renderDays(dates);
      setStatus(`${village.name} loaded · ${dates.length} active days`);
      log(`Loaded ${village.name}: ${dates.length} active days, ${village.agents.length} agents.`, 'ok');
    } catch (error) {
      setStatus(`Could not load village: ${error.message}`);
      log(`Load failed: ${error.message}`, 'error');
    } finally {
      ui.load.disabled = false;
      controller = null;
    }
  }

  async function exportSelected() {
    const days = selectedDays();
    if (!village || !days.length || running) return;
    running = true;
    controller = new AbortController();
    ui.load.disabled = true;
    ui.export.disabled = true;
    ui.cancel.hidden = false;
    ui.log.replaceChildren();
    networkRequests = 0;
    cacheHits = 0;
    lastNetworkAt = 0;
    setProgress(0, days.length);
    log(`Starting browser export of ${days.length} day${days.length === 1 ? '' : 's'} with ${requestedDelayMs() / 1000}s minimum request spacing.`);
    if (days.length > 10) log('Large browser exports can use a lot of memory. The Python CLI remains safer for extremely large history dumps.', 'warn');

    const archive = new Map();
    const rootName = days.length === 1 ? `${safeSlug(village.slug)}-${days[0]}` : `${safeSlug(village.slug)}-${days[0]}-to-${days[days.length - 1]}`;
    const exportSummary = {
      schemaVersion: 1,
      exportType: 'ai-village-browser-multi-day',
      exportedAt: new Date().toISOString(),
      village: { id: village.id, slug: village.slug, name: village.name },
      days,
      dayManifests: {},
    };

    try {
      for (let index = 0; index < days.length; index += 1) {
        const day = days[index];
        log(`Exporting ${day} (${index + 1}/${days.length})…`);
        const result = await buildDayArchive(day, controller.signal);
        const dayPrefix = days.length === 1 ? rootName : `${rootName}/${day}`;
        for (const [name, content] of result.files) archive.set(`${dayPrefix}/${name}`, content);
        exportSummary.dayManifests[day] = result.manifest;
        setProgress(index + 1, days.length);
        log(`${day}: ${result.manifest.counts.rawEvents.toLocaleString()} events, ${result.manifest.counts.messages.toLocaleString()} messages, ${result.manifest.counts.activities.toLocaleString()} activities.`, 'ok');
      }

      if (days.length > 1) archive.set(`${rootName}/export-manifest.json`, json(exportSummary));
      setStatus('Building ZIP locally…');
      log('All network fetching finished. Building ZIP entirely on this device…');
      await new Promise((resolve) => setTimeout(resolve, 0));
      const blob = makeZip(archive);
      const fileName = `${rootName}.zip`;
      downloadBlob(blob, fileName);
      setStatus(`Done · ${fileName}`);
      log(`Downloaded ${fileName} (${(blob.size / 1024 / 1024).toFixed(2)} MiB). Network requests: ${networkRequests}; cache hits: ${cacheHits}.`, 'ok');
      ui.cacheNote.textContent = `This run used ${networkRequests} network request${networkRequests === 1 ? '' : 's'} and ${cacheHits} persistent browser-cache hit${cacheHits === 1 ? '' : 's'}.`;
    } catch (error) {
      if (error?.name === 'AbortError') {
        setStatus('Export cancelled.');
        log('Export cancelled. No partial ZIP was downloaded.', 'warn');
      } else {
        setStatus(`Export failed: ${error.message}`);
        log(`Export failed: ${error.message}`, 'error');
      }
    } finally {
      running = false;
      controller = null;
      ui.load.disabled = false;
      ui.cancel.hidden = true;
      updateSelectionSummary();
    }
  }

  ui.load.addEventListener('click', loadVillage);
  ui.slug.addEventListener('keydown', (event) => { if (event.key === 'Enter') loadVillage(); });
  ui.selectAll.addEventListener('click', () => {
    ui.dayList.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = true; });
    updateSelectionSummary();
  });
  ui.selectLastFive.addEventListener('click', () => {
    const inputs = [...ui.dayList.querySelectorAll('input[type="checkbox"]')];
    inputs.forEach((input, index) => { input.checked = index < 5; });
    updateSelectionSummary();
  });
  ui.clearDays.addEventListener('click', () => {
    ui.dayList.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = false; });
    updateSelectionSummary();
  });
  ui.export.addEventListener('click', exportSelected);
  ui.cancel.addEventListener('click', () => controller?.abort());
  ui.delay.addEventListener('change', () => {
    const value = Math.max(0.75, Number(ui.delay.value) || 1.25);
    ui.delay.value = value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
  });

  const remembered = localStorage.getItem('ai-village-exporter-slug');
  if (remembered) ui.slug.value = remembered;
  updateSelectionSummary();
})();
