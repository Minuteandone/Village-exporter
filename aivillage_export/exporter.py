from __future__ import annotations

import hashlib
import json
import shutil
import urllib.parse
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import HttpError, PoliteHttpClient
from .git_export import GitExporter
from .normalize import build_timeline, counts, map_activities, map_messages
from .utils import dump_json, dump_jsonl, iso_to_dt, safe_slug, sha256_file, village_day_bounds

API_ORIGIN = "https://theaidigest.org/village"


@dataclass
class Village:
    id: str
    slug: str
    name: str
    goal: str | None
    agents: list[dict[str, Any]]
    rooms: list[dict[str, Any]]
    dates: list[str]
    raw: dict[str, Any]


class VillageExporter:
    def __init__(
        self,
        http: PoliteHttpClient,
        output: Path,
        *,
        max_event_pages: int = 100,
        include_git: bool = False,
        memory_mode: str = "consolidations",
        make_zip: bool = True,
    ) -> None:
        self.http = http
        self.output = output
        self.max_event_pages = max(1, max_event_pages)
        self.include_git = include_git
        self.memory_mode = memory_mode
        self.make_zip = make_zip
        self.git = GitExporter(http) if include_git else None

    @staticmethod
    def _url(path: str) -> str:
        return f"{API_ORIGIN}{path}"

    def load_village(self, slug: str) -> Village:
        summary = self.http.get_json(self._url(f"/api/villages?slug={urllib.parse.quote(slug)}"))
        if not isinstance(summary, dict) or not summary.get("id"):
            raise RuntimeError(f"No public AI Village found for slug {slug!r}: {summary}")
        village_id = str(summary["id"])
        raw = self.http.get_json(self._url(f"/api/villages/{urllib.parse.quote(village_id)}"))
        dates_payload = self.http.get_json(self._url(f"/api/villages/{urllib.parse.quote(village_id)}/active-dates"))
        if not isinstance(raw, dict):
            raise RuntimeError("Village metadata response was not an object.")
        dates = sorted(d for d in (dates_payload.get("dates", []) if isinstance(dates_payload, dict) else []) if isinstance(d, str))
        return Village(
            id=village_id,
            slug=str(raw.get("slug") or summary.get("slug") or slug),
            name=str(raw.get("name") or summary.get("name") or slug),
            goal=raw.get("villageGoal") if isinstance(raw.get("villageGoal"), str) else summary.get("villageGoal"),
            agents=[a for a in raw.get("agents", []) if isinstance(a, dict)],
            rooms=[r for r in raw.get("chatRooms", []) if isinstance(r, dict)],
            dates=dates,
            raw=raw,
        )

    def load_events(self, village_id: str, day: str) -> tuple[list[dict[str, Any]], int]:
        events: list[dict[str, Any]] = []
        pages = 0
        for page in range(1, self.max_event_pages + 1):
            params = urllib.parse.urlencode({"villageId": village_id, "date": day, "page": page})
            payload = self.http.get_json(self._url(f"/api/events?{params}"))
            pages += 1
            if not isinstance(payload, dict):
                raise RuntimeError(f"Event page {page} returned an invalid payload.")
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            batch = payload.get("events", [])
            if not isinstance(batch, list):
                raise RuntimeError(f"Event page {page} contained a non-list events field.")
            events.extend(item for item in batch if isinstance(item, dict))
            if not payload.get("hasMore"):
                return events, pages
        raise RuntimeError(
            f"The day still had more event pages after the safety cap ({self.max_event_pages}). "
            "Nothing is silently omitted; rerun with a higher --max-event-pages if intentional."
        )

    def load_human_sessions(self, village_id: str, day: str) -> tuple[list[dict[str, Any]], str | None]:
        params = urllib.parse.urlencode({"villageId": village_id, "date": day})
        try:
            payload = self.http.get_json(self._url(f"/api/human-use-sessions?{params}"))
        except HttpError as exc:
            return [], f"Human-use session endpoint failed: {exc}"
        if not isinstance(payload, dict):
            return [], "Human-use session endpoint returned an invalid payload."
        if payload.get("error"):
            return [], f"Human-use session endpoint reported: {payload.get('error')}"
        return [s for s in payload.get("sessions", []) if isinstance(s, dict)], None

    def _memory_candidates(self, day: str, events: list[dict[str, Any]], village: Village) -> list[str]:
        if self.memory_mode == "none":
            return []
        if self.memory_mode == "all-agents":
            return [str(a["id"]) for a in village.agents if a.get("id")]
        ids: set[str] = set()
        for event in events:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if data.get("actionType") != "CONSOLIDATE":
                continue
            agent_id = data.get("agentId") or data.get("speakerId")
            if isinstance(agent_id, str) and agent_id:
                ids.add(agent_id)
        return sorted(ids)

    def load_memories(self, day: str, events: list[dict[str, Any]], village: Village) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        agent_ids = self._memory_candidates(day, events, village)
        if not agent_ids:
            return {}, []
        _, end = village_day_bounds(day)
        cutoff_ms = int(end.timestamp() * 1000) + 1000
        start, _ = village_day_bounds(day)
        result: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        for agent_id in agent_ids:
            query = urllib.parse.urlencode({"createdAt": cutoff_ms})
            try:
                payload = self.http.get_json(
                    self._url(f"/api/agent/{urllib.parse.quote(agent_id)}/memories?{query}")
                )
            except Exception as exc:
                warnings.append(f"Memory fetch failed for agent {agent_id}: {exc}")
                continue
            versions = payload.get("memories", []) if isinstance(payload, dict) else []
            if not isinstance(versions, list):
                continue
            versions = [v for v in versions if isinstance(v, dict)]
            versions.sort(key=lambda v: v.get("createdAt") or "", reverse=True)
            if self.memory_mode == "all-agents":
                # Keep versions created during the day plus one earlier version for a useful diff boundary.
                in_day: list[dict[str, Any]] = []
                previous: dict[str, Any] | None = None
                for version in versions:
                    created = iso_to_dt(version.get("createdAt"))
                    if created and start <= created < end:
                        in_day.append(version)
                    elif created and created < start and previous is None:
                        previous = version
                result[agent_id] = in_day + ([previous] if previous else [])
            else:
                # A single endpoint call generally includes the nearby versions Aivillagenews needs for consolidation diffs.
                result[agent_id] = versions
        return result, warnings


    @staticmethod
    def discover_rooms(events: list[dict[str, Any]], known_rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rooms: dict[str, dict[str, Any]] = {
            str(room.get("id")): dict(room) for room in known_rooms if room.get("id")
        }
        for event in events:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            room_id = data.get("roomId")
            room_name = data.get("roomName")
            if isinstance(room_id, str) and room_id:
                current = rooms.setdefault(room_id, {"id": room_id})
                if isinstance(room_name, str) and room_name and not current.get("name"):
                    current["name"] = room_name
                current.setdefault("discoveredFromHistoricalEvent", room_id not in {str(r.get("id")) for r in known_rooms if r.get("id")})
            previous_id = data.get("previousRoomId")
            previous_name = data.get("previousRoomName")
            if isinstance(previous_id, str) and previous_id:
                current = rooms.setdefault(previous_id, {"id": previous_id})
                if isinstance(previous_name, str) and previous_name and not current.get("name"):
                    current["name"] = previous_name
                current.setdefault("discoveredFromHistoricalEvent", previous_id not in {str(r.get("id")) for r in known_rooms if r.get("id")})
        return sorted(rooms.values(), key=lambda room: (str(room.get("name") or ""), str(room.get("id") or "")))

    def _summary_markdown(
        self,
        village: Village,
        day: str,
        events: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        activities: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        memories: dict[str, list[dict[str, Any]]],
        git_counts: dict[str, int],
        warnings: list[str],
    ) -> str:
        room_names = {str(r.get("id")): str(r.get("name")) for r in village.rooms if r.get("id")}
        agent_names = {str(a.get("id")): str(a.get("name")) for a in village.agents if a.get("id")}
        speaker_counts = Counter(m.get("speakerName") or m.get("speakerId") or "unknown" for m in messages)
        room_counts = Counter(room_names.get(str(m.get("roomId")), str(m.get("roomId") or "unknown")) for m in messages)
        action_counts = counts(events)
        lines = [
            f"# AI Village day export — {day}",
            "",
            f"Village: **{village.name}** (`{village.slug}`)",
            "",
            f"- Raw events: **{len(events):,}**",
            f"- Chat messages: **{len(messages):,}**",
            f"- Non-chat activities: **{len(activities):,}**",
            f"- Human-use sessions: **{len(sessions):,}**",
            f"- Agents with saved memory context: **{len(memories):,}**",
        ]
        if git_counts:
            lines += [
                f"- GitHub commits captured: **{git_counts.get('github', 0):,}**",
                f"- GitLab commits captured: **{git_counts.get('gitlab', 0):,}**",
            ]
        lines += ["", "## Action types", ""]
        lines += [f"- `{name}`: {count:,}" for name, count in action_counts.items()]
        lines += ["", "## Most active speakers", ""]
        lines += [f"- {name}: {count:,} messages" for name, count in speaker_counts.most_common(20)] or ["- No messages"]
        lines += ["", "## Rooms", ""]
        lines += [f"- #{name}: {count:,} messages" for name, count in room_counts.most_common()] or ["- No rooms with messages"]
        if warnings:
            lines += ["", "## Warnings", ""] + [f"- {warning}" for warning in warnings]
        lines += [
            "",
            "## Archive notes",
            "",
            "`events.raw.json` is the preservation source of truth. Normalized files are convenience views and never replace the raw event payloads.",
            "",
            "Computer/browser actions are kept when they exist in the public event feed; unlike the viewer UI, the exporter does not intentionally filter them out.",
        ]
        return "\n".join(lines) + "\n"

    def export_day(self, village: Village, day: str, *, overwrite: bool = False) -> dict[str, Any]:
        base = self.output / safe_slug(village.slug) / day
        manifest_path = base / "manifest.json"
        if manifest_path.exists() and not overwrite:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("complete") is True:
                return {"day": day, "skipped": True, "path": str(base), "manifest": existing}
        if base.exists() and overwrite:
            shutil.rmtree(base)
        base.mkdir(parents=True, exist_ok=True)

        request_count_before = self.http.network_requests
        cache_hits_before = self.http.cache_hits
        warnings: list[str] = []
        exported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        events, event_pages = self.load_events(village.id, day)
        sessions, session_warning = self.load_human_sessions(village.id, day)
        if session_warning:
            warnings.append(session_warning)
        memories, memory_warnings = self.load_memories(day, events, village)
        warnings.extend(memory_warnings)

        messages = map_messages(events, village.agents)
        activities = map_activities(events, village.agents)
        timeline = build_timeline(events, village.agents, sessions)
        discovered_rooms = self.discover_rooms(events, village.rooms)

        git_counts: dict[str, int] = {}
        git_meta: dict[str, Any] | None = None
        if self.git is not None:
            try:
                git_result = self.git.export_day(day)
                dump_json(base / "git" / "github-commits.raw.json", git_result.github)
                dump_json(base / "git" / "gitlab-commits.raw.json", git_result.gitlab)
                git_counts = {"github": len(git_result.github), "gitlab": len(git_result.gitlab)}
                git_meta = {
                    "githubCommitCount": len(git_result.github),
                    "gitlabCommitCount": len(git_result.gitlab),
                    "gitlabProjectsScanned": git_result.gitlab_projects_scanned,
                }
                warnings.extend(git_result.warnings)
            except Exception as exc:
                warnings.append(f"Optional Git history export failed: {exc}")

        dump_json(base / "events.raw.json", events)
        dump_jsonl(base / "events.raw.jsonl", events)
        dump_json(base / "messages.json", messages)
        dump_json(base / "activities.json", activities)
        dump_jsonl(base / "timeline.jsonl", timeline)
        dump_json(base / "human-use-sessions.raw.json", sessions)
        dump_json(base / "agents.json", village.agents)
        dump_json(base / "rooms.json", village.rooms)
        dump_json(base / "rooms.discovered.json", discovered_rooms)
        dump_json(
            base / "village.json",
            {
                "id": village.id,
                "slug": village.slug,
                "name": village.name,
                "goal": village.goal,
            },
        )
        if memories:
            for agent_id, versions in memories.items():
                dump_json(base / "memories" / f"{safe_slug(agent_id, 'agent')}.json", versions)

        (base / "summary.md").write_text(
            self._summary_markdown(
                village,
                day,
                events,
                messages,
                activities,
                sessions,
                memories,
                git_counts,
                warnings,
            ),
            encoding="utf-8",
        )

        manifest = {
            "schemaVersion": 1,
            "exportType": "ai-village-complete-day",
            "complete": True,
            "exportedAt": exported_at,
            "source": API_ORIGIN,
            "village": {"id": village.id, "slug": village.slug, "name": village.name},
            "day": day,
            "counts": {
                "rawEvents": len(events),
                "eventPages": event_pages,
                "messages": len(messages),
                "activities": len(activities),
                "humanUseSessions": len(sessions),
                "memoryAgents": len(memories),
                **({"git": git_meta} if git_meta else {}),
            },
            "warnings": warnings,
            "network": {
                "networkRequestsThisDay": self.http.network_requests - request_count_before,
                "cacheHitsThisDay": self.http.cache_hits - cache_hits_before,
                "minimumOfficialDelaySeconds": self.http.default_delay,
            },
            "files": [],
        }

        dump_json(manifest_path, manifest)
        self._write_checksums(base)
        files = sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())
        manifest["files"] = files
        dump_json(manifest_path, manifest)
        self._write_checksums(base)

        zip_path = None
        if self.make_zip:
            archive_dir = self.output / "archives"
            archive_dir.mkdir(parents=True, exist_ok=True)
            zip_path = archive_dir / f"{safe_slug(village.slug)}-{day}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        zf.write(path, arcname=f"{safe_slug(village.slug)}-{day}/{path.relative_to(base)}")

        return {
            "day": day,
            "skipped": False,
            "path": str(base),
            "zip": str(zip_path) if zip_path else None,
            "manifest": manifest,
        }

    @staticmethod
    def _write_checksums(base: Path) -> None:
        checksum_path = base / "checksums.sha256"
        files = [p for p in base.rglob("*") if p.is_file() and p != checksum_path]
        lines = [f"{sha256_file(path)}  {path.relative_to(base).as_posix()}" for path in sorted(files)]
        checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
