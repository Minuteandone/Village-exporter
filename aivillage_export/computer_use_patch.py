from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .exporter import VillageExporter
from .utils import dump_json, dump_jsonl, safe_slug

SCREENSHOT_CDN = "https://village-screenshots-sfo2.sfo2.cdn.digitaloceanspaces.com/computer-use-turns"
TURN_ARRAY_KEYS = (
    "turns",
    "computerUseTurns",
    "computer_use_turns",
    "computerTurns",
    "computer_turns",
)
SESSION_ARRAY_KEYS = (
    "sessions",
    "computerUseSessions",
    "computer_use_sessions",
)


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def sessions_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Return session rows while keeping the original API wrapper separately."""
    if isinstance(payload, list):
        return _objects(payload)
    if not isinstance(payload, dict):
        return []
    for key in SESSION_ARRAY_KEYS:
        rows = _objects(payload.get(key))
        if rows or isinstance(payload.get(key), list):
            return rows
    data = payload.get("data")
    if isinstance(data, dict):
        for key in SESSION_ARRAY_KEYS:
            rows = _objects(data.get(key))
            if rows or isinstance(data.get(key), list):
                return rows
    return []


def _turn_rows(session: dict[str, Any]) -> list[Any]:
    for key in TURN_ARRAY_KEYS:
        value = session.get(key)
        if isinstance(value, list):
            return value
    data = session.get("data")
    if isinstance(data, dict):
        for key in TURN_ARRAY_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def flatten_turns(sessions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten API-provided turn rows and retain the parent session id.

    The live endpoint has existed across multiple Village versions, so the parser
    accepts both full turn objects and bare turn ids instead of silently dropping
    either representation.
    """
    flattened: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for session in sessions:
        session_id = str(session.get("id") or session.get("sessionId") or session.get("session_id") or "")
        for index, raw in enumerate(_turn_rows(session)):
            if isinstance(raw, dict):
                turn = dict(raw)
            elif isinstance(raw, (str, int)):
                turn = {"id": str(raw)}
            else:
                continue
            turn_id = str(turn.get("id") or turn.get("turnId") or turn.get("turn_id") or "")
            identity = (session_id, turn_id or f"index:{index}")
            if identity in seen:
                continue
            seen.add(identity)
            turn.setdefault("computerUseSessionId", session_id or None)
            flattened.append(turn)
    return flattened


def _agent_names(agents: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {
        str(agent.get("id")): str(agent.get("name"))
        for agent in agents
        if agent.get("id") and agent.get("name")
    }


def normalize_turns(
    sessions: Iterable[dict[str, Any]],
    agents: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = _agent_names(agents)
    session_map: dict[str, dict[str, Any]] = {}
    for session in sessions:
        sid = str(session.get("id") or session.get("sessionId") or session.get("session_id") or "")
        if sid:
            session_map[sid] = session

    normalized: list[dict[str, Any]] = []
    for raw in flatten_turns(session_map.values()):
        session_id = str(raw.get("computerUseSessionId") or raw.get("sessionId") or raw.get("session_id") or "")
        session = session_map.get(session_id, {})
        turn_id = raw.get("id") or raw.get("turnId") or raw.get("turn_id")
        agent_id = raw.get("agentId") or raw.get("agent_id") or session.get("agentId") or session.get("agent_id")
        created_at = raw.get("createdAt") or raw.get("created_at") or raw.get("timestamp") or raw.get("ts")
        action = raw.get("agentAction")
        if action is None:
            action = raw.get("agent_action")
        if action is None:
            action = raw.get("action")
        screenshot_url = f"{SCREENSHOT_CDN}/{turn_id}.png" if turn_id else None
        normalized.append(
            {
                "stepIndex": len(normalized) + 1,
                "id": turn_id,
                "computerUseSessionId": session_id or None,
                "agentId": agent_id,
                "agentName": names.get(str(agent_id)) if agent_id is not None else None,
                "createdAt": created_at,
                "action": action,
                "screenshotUrl": screenshot_url,
                "rawTurn": raw,
            }
        )
    normalized.sort(key=lambda row: (str(row.get("createdAt") or ""), str(row.get("id") or "")))
    for index, row in enumerate(normalized, 1):
        row["stepIndex"] = index
    return normalized


def _marker_rows(base: Path) -> list[dict[str, Any]]:
    path = base / "computer-steps.json"
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return _objects(value)


def _rewrite_timeline(base: Path, real_steps: list[dict[str, Any]]) -> None:
    path = base / "timeline.jsonl"
    existing: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "computer-step" and item.get("rawTurn") is None:
                item["kind"] = "computer-session-marker"
            existing.append(item)
    for step in real_steps:
        existing.append({"kind": "computer-use-turn", **step})
    existing.sort(
        key=lambda item: (
            str(item.get("createdAt") or ""),
            item.get("eventIndex") if isinstance(item.get("eventIndex"), int) else 10**18,
            str(item.get("id") or ""),
        )
    )
    dump_jsonl(path, existing)


def _update_summary(base: Path, marker_count: int, session_count: int, turn_count: int, warning: str | None) -> None:
    path = base / "summary.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "# AI Village day export\n"
    text = re.sub(r"^- Computer steps: \*\*[^\n]+\*\*\n?", "", text, flags=re.M)
    anchor = re.search(r"^- Non-chat activities: \*\*[^\n]+\*\*", text, flags=re.M)
    lines = (
        f"- Computer session markers: **{marker_count:,}**\n"
        f"- Computer-use sessions: **{session_count:,}**\n"
        f"- Real computer-use turns: **{turn_count:,}**"
    )
    if anchor:
        end = anchor.end()
        text = text[:end] + "\n" + lines + text[end:]
    else:
        text += "\n" + lines + "\n"
    old_notes = [
        "Computer/browser actions are kept when they exist in the public event feed. `computer-steps.json` and `computer-steps.jsonl` provide a dedicated chronological view while retaining each original raw event.",
        "Computer/browser actions are kept when they exist in the public event feed; unlike the viewer UI, the exporter does not intentionally filter them out.",
    ]
    replacement = (
        "High-level computer-related `/api/events` rows are preserved separately as `computer-session-markers.*`. "
        "The real `computer-steps.*` files come from `/api/computer-use-sessions` and represent the API-provided computer-use turns. "
        "Screenshot URLs are referenced by turn id; images are not bulk-downloaded by default."
    )
    for note in old_notes:
        text = text.replace(note, replacement)
    if replacement not in text:
        text += "\n\n" + replacement + "\n"
    if warning and warning not in text:
        text += f"\n## Computer-use warning\n\n- {warning}\n"
    path.write_text(text, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finalize(base: Path, result: dict[str, Any], exporter: VillageExporter, counts: dict[str, int], warning: str | None) -> None:
    manifest_path = base / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except Exception:
            pass
    manifest["schemaVersion"] = max(int(manifest.get("schemaVersion") or 1), 2)
    manifest.setdefault("counts", {}).update(counts)
    manifest.setdefault("sources", {})["computerUseSessions"] = "/api/computer-use-sessions?villageId=…&date=YYYY-MM-DD"
    warnings = manifest.setdefault("warnings", [])
    if warning and warning not in warnings:
        warnings.append(warning)
    file_names = sorted(
        str(path.relative_to(base)).replace("\\", "/")
        for path in base.rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    )
    file_names.append("checksums.sha256")
    manifest["files"] = sorted(set(file_names))
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    checksum_lines: list[str] = []
    for path in sorted((p for p in base.rglob("*") if p.is_file() and p.name != "checksums.sha256"), key=lambda p: str(p.relative_to(base))):
        rel = str(path.relative_to(base)).replace("\\", "/")
        checksum_lines.append(f"{_sha256(path)}  {rel}")
    (base / "checksums.sha256").write_text("\n".join(checksum_lines) + ("\n" if checksum_lines else ""), encoding="utf-8")

    if exporter.make_zip:
        zip_path_text = result.get("zip")
        if zip_path_text:
            zip_path = Path(zip_path_text)
        else:
            zip_path = exporter.output / "archives" / f"{safe_slug(manifest.get('village', {}).get('slug') or 'village')}-{manifest.get('day', base.name)}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted((p for p in base.rglob("*") if p.is_file()), key=lambda p: str(p.relative_to(base))):
                archive.write(path, arcname=f"{base.name}/{path.relative_to(base)}")
        result["zip"] = str(zip_path)
    result["manifest"] = manifest
    result["skipped"] = False


def _fetch_computer_payload(exporter: VillageExporter, village_id: str, day: str) -> tuple[Any, str | None]:
    params = urllib.parse.urlencode({"villageId": village_id, "date": day})
    try:
        payload = exporter.http.get_json(exporter._url(f"/api/computer-use-sessions?{params}"))
    except Exception as exc:
        return {"sessions": []}, f"Computer-use session endpoint failed: {exc}"
    if isinstance(payload, dict) and payload.get("error"):
        return payload, f"Computer-use session endpoint reported: {payload.get('error')}"
    if not isinstance(payload, (dict, list)):
        return payload, "Computer-use session endpoint returned an unexpected payload type."
    return payload, None


_original_export_day = VillageExporter.export_day


def _export_day_with_real_computer_turns(self: VillageExporter, village: Any, day: str, *, overwrite: bool = False) -> dict[str, Any]:
    result = _original_export_day(self, village, day, overwrite=overwrite)
    base = Path(result["path"])

    # The previous release called event markers "computer steps". Preserve them
    # under an honest name before replacing computer-steps.* with real API turns.
    markers = _marker_rows(base)
    dump_json(base / "computer-session-markers.json", markers)
    dump_jsonl(base / "computer-session-markers.jsonl", markers)

    payload, warning = _fetch_computer_payload(self, village.id, day)
    sessions = sessions_from_payload(payload)
    raw_turns = flatten_turns(sessions)
    steps = normalize_turns(sessions, village.agents)

    dump_json(base / "computer-use-sessions.raw.json", payload)
    dump_json(base / "computer-use-sessions.json", sessions)
    dump_jsonl(base / "computer-use-turns.raw.jsonl", raw_turns)
    dump_json(base / "computer-steps.json", steps)
    dump_jsonl(base / "computer-steps.jsonl", steps)
    _rewrite_timeline(base, steps)
    _update_summary(base, len(markers), len(sessions), len(steps), warning)
    _finalize(
        base,
        result,
        self,
        {
            "computerSessionMarkers": len(markers),
            "computerUseSessions": len(sessions),
            "computerUseTurns": len(steps),
            "computerSteps": len(steps),
        },
        warning,
    )
    return result


VillageExporter.export_day = _export_day_with_real_computer_turns
