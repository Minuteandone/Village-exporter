from __future__ import annotations

import re
from collections import Counter
from typing import Any

CHAT_TYPES = {"AGENT_TALK", "USER_TALK"}
COMPUTER_RE = re.compile(r"(COMPUTER|SCREENSHOT|MOUSE|KEYBOARD|BROWSER)", re.I)


def _s(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda event: (
            event.get("eventIndex") if isinstance(event.get("eventIndex"), int) else 10**18,
            event.get("createdAt") or "",
            event.get("id") or "",
        ),
    )


def map_messages(events: list[dict[str, Any]], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {str(a.get("id")): a.get("name") for a in agents if a.get("id")}
    messages: list[dict[str, Any]] = []
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        action = data.get("actionType")
        if action not in CHAT_TYPES or not _s(data.get("content")) or not _s(data.get("roomId")):
            continue
        is_agent = action == "AGENT_TALK"
        speaker_id = _s(data.get("speakerId")) or f"unknown-{event.get('id', 'event')}"
        messages.append(
            {
                "id": _s(data.get("messageId")) or event.get("id"),
                "eventIndex": event.get("eventIndex"),
                "speakerId": speaker_id,
                "speakerName": (names.get(speaker_id) or "Unknown agent") if is_agent else (_s(data.get("speakerName")) or "Human visitor"),
                "speakerKind": "agent" if is_agent else "human",
                "content": data.get("content"),
                "roomId": data.get("roomId"),
                "createdAt": event.get("createdAt"),
                "rawEventId": event.get("id"),
            }
        )
    return sorted(messages, key=lambda m: (m.get("createdAt") or "", m.get("eventIndex") or 0))


def action_category(action: str) -> str:
    if action == "PAUSE":
        return "pause"
    if action == "CONSOLIDATE":
        return "consolidation"
    if action in {"ENTER_ROOM", "LEAVE_ROOM"}:
        return "room"
    if action == "SEARCH_HISTORY":
        return "search"
    if "HUMAN" in action:
        return "human-helper"
    if "OUTREACH" in action:
        return "outreach"
    if "GOOGLE_SIGN_IN" in action:
        return "auth"
    if COMPUTER_RE.search(action):
        return "computer"
    return "other"


def map_activities(events: list[dict[str, Any]], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {str(a.get("id")): a.get("name") for a in agents if a.get("id")}
    activities: list[dict[str, Any]] = []
    for event in sort_events(events):
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        action = _s(data.get("actionType"))
        if not action or action in CHAT_TYPES:
            continue
        agent_id = _s(data.get("agentId")) or _s(data.get("speakerId"))
        activities.append(
            {
                "id": event.get("id"),
                "eventIndex": event.get("eventIndex"),
                "createdAt": event.get("createdAt"),
                "actionType": action,
                "category": action_category(action),
                "agentId": agent_id,
                "agentName": names.get(agent_id or "") or _s(data.get("speakerName")),
                "roomId": _s(data.get("roomId")),
                "roomName": _s(data.get("roomName")),
                "data": data,
            }
        )
    return activities


def map_computer_steps(events: list[dict[str, Any]], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return computer/browser/mouse/keyboard/screenshot events as a dedicated chronological view."""
    names = {str(a.get("id")): a.get("name") for a in agents if a.get("id")}
    steps: list[dict[str, Any]] = []
    for event in sort_events(events):
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        action = _s(data.get("actionType"))
        if not action or not COMPUTER_RE.search(action):
            continue
        agent_id = _s(data.get("agentId")) or _s(data.get("speakerId"))
        steps.append(
            {
                "stepIndex": len(steps) + 1,
                "id": event.get("id"),
                "eventIndex": event.get("eventIndex"),
                "createdAt": event.get("createdAt"),
                "actionType": action,
                "agentId": agent_id,
                "agentName": names.get(agent_id or "") or _s(data.get("speakerName")),
                "roomId": _s(data.get("roomId")),
                "roomName": _s(data.get("roomName")),
                "data": data,
                "rawEvent": event,
            }
        )
    return steps


def helper_context_items(sessions: list[dict[str, Any]], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {str(a.get("id")): a.get("name") for a in agents if a.get("id")}
    items: list[dict[str, Any]] = []
    for session in sessions:
        sid = str(session.get("id") or "session")
        agent_id = str(session.get("agentId") or "")
        agent_name = names.get(agent_id) or "Unknown agent"
        if _s(session.get("userIntro")):
            items.append(
                {
                    "kind": "human-helper-context",
                    "id": f"{sid}-intro",
                    "createdAt": session.get("createdAt"),
                    "sessionId": sid,
                    "speakerKind": "human",
                    "speakerName": "Human helper",
                    "content": session.get("userIntro"),
                }
            )
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            action = turn.get("agentAction") if isinstance(turn.get("agentAction"), dict) else {}
            if _s(action.get("instructions")):
                items.append(
                    {
                        "kind": "human-helper-context",
                        "id": f"{turn.get('id', sid)}-agent",
                        "createdAt": turn.get("createdAt"),
                        "sessionId": sid,
                        "speakerKind": "agent",
                        "speakerName": agent_name,
                        "content": action.get("instructions"),
                    }
                )
            if _s(turn.get("userResponse")):
                items.append(
                    {
                        "kind": "human-helper-context",
                        "id": f"{turn.get('id', sid)}-human",
                        "createdAt": turn.get("updatedAt") or turn.get("createdAt"),
                        "sessionId": sid,
                        "speakerKind": "human",
                        "speakerName": "Human helper",
                        "content": turn.get("userResponse"),
                    }
                )
    return sorted(items, key=lambda item: item.get("createdAt") or "")


def build_timeline(
    events: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages_by_event = {m.get("rawEventId"): m for m in map_messages(events, agents)}
    timeline: list[dict[str, Any]] = []
    for event in sort_events(events):
        event_id = event.get("id")
        if event_id in messages_by_event:
            timeline.append({"kind": "message", **messages_by_event[event_id]})
        else:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            action = _s(data.get("actionType"))
            category = action_category(action) if action else "unknown"
            timeline.append(
                {
                    "kind": "computer-step" if category == "computer" else ("activity" if action else "unknown-event"),
                    "id": event_id,
                    "eventIndex": event.get("eventIndex"),
                    "createdAt": event.get("createdAt"),
                    "actionType": action,
                    "category": category,
                    "data": data,
                }
            )
    timeline.extend(helper_context_items(sessions, agents))
    return sorted(
        timeline,
        key=lambda item: (
            item.get("createdAt") or "",
            item.get("eventIndex") if isinstance(item.get("eventIndex"), int) else 10**18,
            item.get("id") or "",
        ),
    )


def counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        action = _s(data.get("actionType")) or "(missing actionType)"
        counter[action] += 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))
