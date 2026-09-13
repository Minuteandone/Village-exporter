# AI Village Mass Exporter 📦

A polite, resumable command-line archiver for exporting **entire public AI Village days**, inspired by the Aivillagenews / Village Archive viewer.

The important difference: Aivillagenews's existing day JSON download is message-focused. This tool treats the raw day event feed as the preservation source of truth and saves normalized convenience views alongside it.

## What a day export contains

Each completed day directory contains:

- `events.raw.json` — every event returned by the public paginated day feed
- `events.raw.jsonl` — the same events as newline-delimited JSON
- `messages.json` — normalized `AGENT_TALK` / `USER_TALK` messages
- `activities.json` — every non-chat event, including computer/browser event types when present
- `timeline.jsonl` — chronological merged event/message/helper timeline
- `human-use-sessions.raw.json` — public helper-session records and turns
- `agents.json`, `rooms.json`, `village.json`
- `memories/*.json` — memory context around consolidations by default
- `summary.md` — quick counts by action type, speaker, and room
- `manifest.json` — schema, counts, warnings, network/cache stats, file list
- `checksums.sha256` — integrity hashes
- optionally `git/*.json` — public GitHub/GitLab commit activity

By default it also makes a ZIP for each day under `OUTPUT/archives/`.

## Anti-spam design 🐌

This is intentionally conservative:

- **one request at a time**; no request fan-out
- **1.25 seconds minimum** between official Village API requests by default
- hard floor of **0.75 seconds**, even if a smaller `--delay` is passed
- persistent URL-keyed **disk cache** so reruns reuse old responses
- completed day directories are **skipped** unless `--refresh` is explicitly used
- exponential backoff with jitter on transient failures
- honors `Retry-After` on 429 / temporary server responses
- event pagination has a safety cap and **fails loudly instead of silently truncating**
- Git history is **opt-in** because it can involve many public API calls
- GitHub commit search is paced at **6.25 seconds/request** to stay friendly to unauthenticated search limits
- GitLab repo scanning is sequential and cached

## Install

Requires Python 3.11+ and uses only the standard library at runtime.

```bash
python -m pip install -e .
```

Or run directly from the project folder:

```bash
python -m aivillage_export --help
```

## Examples

Export one full day:

```bash
python -m aivillage_export actual-launch-1 --date 2026-09-11
```

Export a range of active Village days:

```bash
python -m aivillage_export actual-launch-1 --from 2026-09-01 --to 2026-09-11
```

Export the last five active days:

```bash
python -m aivillage_export actual-launch-1 --last 5
```

Export every active day (cached/resumable):

```bash
python -m aivillage_export actual-launch-1 --all --keep-going
```

Add public Git activity too:

```bash
python -m aivillage_export actual-launch-1 --date 2026-09-11 --include-git
```

List which active days would be selected without exporting them:

```bash
python -m aivillage_export actual-launch-1 --from 2026-09-01 --to 2026-09-11 --list-days
```

## Memory modes

`--memories consolidations` is the default. It fetches memory versions only for agents that consolidated on that day, giving useful before/after archival context without querying every agent.

`--memories none` makes a lighter archive. `--memories all-agents` captures versions created during the selected day (plus one earlier boundary version) for every agent, but naturally uses more requests.

## Git history caveat

The core Village event export is the archival priority. `--include-git` is separate because Git history comes from GitHub/GitLab rather than the Village event API and can require many requests. It saves commit-list records, not every commit diff or a full clone of every repository. For repo-content preservation, pair this with a separate repository snapshot/export tool rather than making this day exporter hammer hundreds of diff endpoints.

## Resume behavior

Every GET response is cached under `OUTPUT/.http-cache/`. If an export is interrupted, rerunning the command reuses cached event pages and other already-fetched responses. A day with a complete `manifest.json` is skipped.

Use `--refresh` only when you deliberately want to bypass the HTTP cache and replace existing complete day exports.

## Testing

```bash
python -m unittest discover -s tests -v
```

The test suite is offline; it does not contact AI Digest, GitHub, or GitLab.
