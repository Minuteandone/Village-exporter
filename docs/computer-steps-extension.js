(() => {
  'use strict';

  const originalSet = Map.prototype.set;
  const COMPUTER_ACTION_PATTERN = /(COMPUTER|SCREENSHOT|MOUSE|KEYBOARD|BROWSER)_?(USE|TURN|ACTION|SESSION)?/i;

  function parseJsonArray(value) {
    if (typeof value !== 'string') return [];
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  function json(value) {
    return JSON.stringify(value, null, 2) + '\n';
  }

  function jsonl(values) {
    return values.map((value) => JSON.stringify(value)).join('\n') + (values.length ? '\n' : '');
  }

  function computerSteps(activities, rawEvents) {
    const rawById = new Map(
      rawEvents
        .filter((event) => event && event.id !== undefined && event.id !== null)
        .map((event) => [String(event.id), event]),
    );

    const steps = [];
    for (const activity of activities) {
      const actionType = typeof activity?.actionType === 'string' ? activity.actionType : '';
      if (activity?.category !== 'computer' && !COMPUTER_ACTION_PATTERN.test(actionType)) continue;
      steps.push({
        stepIndex: steps.length + 1,
        id: activity?.id ?? null,
        eventIndex: activity?.eventIndex ?? null,
        createdAt: activity?.createdAt ?? null,
        actionType: actionType || null,
        agentId: activity?.agentId ?? null,
        agentName: activity?.agentName ?? null,
        roomId: activity?.roomId ?? null,
        roomName: activity?.roomName ?? null,
        data: activity?.data ?? {},
        rawEvent: rawById.get(String(activity?.id)) ?? null,
      });
    }
    return steps;
  }

  function stepCount(fileMap) {
    return parseJsonArray(fileMap.get('computer-steps.json')).length;
  }

  Map.prototype.set = function patchedSet(key, value) {
    if (key === 'activities.json' && typeof value === 'string') {
      const result = originalSet.call(this, key, value);
      const activities = parseJsonArray(value);
      const rawEvents = parseJsonArray(this.get('events.raw.json'));
      const steps = computerSteps(activities, rawEvents);
      originalSet.call(this, 'computer-steps.json', json(steps));
      originalSet.call(this, 'computer-steps.jsonl', jsonl(steps));
      return result;
    }

    if (key === 'timeline.jsonl' && typeof value === 'string') {
      const lines = value.split('\n').filter(Boolean);
      const rewritten = lines.map((line) => {
        try {
          const item = JSON.parse(line);
          if (item?.category === 'computer' || COMPUTER_ACTION_PATTERN.test(String(item?.actionType || ''))) {
            item.kind = 'computer-step';
          }
          return JSON.stringify(item);
        } catch {
          return line;
        }
      });
      value = rewritten.join('\n') + (rewritten.length ? '\n' : '');
    }

    if (key === 'summary.md' && typeof value === 'string') {
      const count = stepCount(this);
      if (!value.includes('- Computer steps:')) {
        value = value.replace(
          /(- Non-chat activities: \*\*[^\n]+\*\*)/,
          `$1\n- Computer steps: **${count.toLocaleString()}**`,
        );
      }
      value = value.replace(
        'Computer/browser actions are kept when they exist in the public event feed; the exporter does not intentionally filter them out.',
        'Computer/browser actions are kept when they exist in the public event feed. `computer-steps.json` and `computer-steps.jsonl` provide a dedicated chronological view while retaining each original raw event.',
      );
    }

    if (key === 'manifest.json' && typeof value === 'string') {
      try {
        const manifest = JSON.parse(value);
        manifest.counts = manifest.counts || {};
        manifest.counts.computerSteps = stepCount(this);
        value = json(manifest);
      } catch {
        // Keep the original manifest if it is unexpectedly unreadable.
      }
    }

    return originalSet.call(this, key, value);
  };
})();
