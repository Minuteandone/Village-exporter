from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .client import PoliteHttpClient
from .exporter import VillageExporter
from .utils import parse_day


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aivillage-export",
        description="Polite, resumable whole-day exporter for the public AI Village archive.",
    )
    p.add_argument("slug", nargs="?", default="actual-launch-1", help="public village slug")
    select = p.add_mutually_exclusive_group(required=True)
    select.add_argument("--date", action="append", dest="dates", help="export one day; repeatable (YYYY-MM-DD)")
    select.add_argument("--from", dest="from_date", help="first active day to export (inclusive)")
    select.add_argument("--all", action="store_true", help="export every active day")
    select.add_argument("--last", type=int, metavar="N", help="export the last N active days")
    p.add_argument("--to", dest="to_date", help="last active day for --from (inclusive; defaults latest)")
    p.add_argument("-o", "--output", type=Path, default=Path("ai-village-exports"))
    p.add_argument("--cache-dir", type=Path, default=None, help="HTTP cache path (default: OUTPUT/.http-cache)")
    p.add_argument("--delay", type=float, default=1.25, help="minimum seconds between official API requests; hard floor 0.75")
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--max-event-pages", type=int, default=100)
    p.add_argument("--memories", choices=["none", "consolidations", "all-agents"], default="consolidations")
    p.add_argument("--include-git", action="store_true", help="also scan public GitHub/GitLab commit activity (much heavier and slower)")
    p.add_argument("--no-zip", action="store_true", help="leave day directories only")
    p.add_argument("--refresh", action="store_true", help="bypass HTTP cache and overwrite complete day exports")
    p.add_argument("--keep-going", action="store_true", help="continue to later days after a failed day")
    p.add_argument("--list-days", action="store_true", help="print selected days after resolving the village, then stop")
    p.add_argument("--version", action="version", version=__version__)
    return p


def choose_days(args: argparse.Namespace, active: list[str]) -> list[str]:
    if args.dates:
        requested = [parse_day(d).isoformat() for d in args.dates]
        missing = [d for d in requested if d not in active]
        if missing:
            raise ValueError(f"Not active Village day(s): {', '.join(missing)}")
        return sorted(set(requested))
    if args.all:
        return list(active)
    if args.last is not None:
        if args.last <= 0:
            raise ValueError("--last must be positive")
        return active[-args.last :]
    start = parse_day(args.from_date).isoformat()
    end = parse_day(args.to_date).isoformat() if args.to_date else (active[-1] if active else start)
    if end < start:
        raise ValueError("--to must be on or after --from")
    return [day for day in active if start <= day <= end]


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output = args.output.resolve()
    cache_dir = (args.cache_dir or output / ".http-cache").resolve()
    http = PoliteHttpClient(
        cache_dir,
        delay=args.delay,
        retries=args.retries,
        refresh=args.refresh,
    )
    exporter = VillageExporter(
        http,
        output,
        max_event_pages=args.max_event_pages,
        include_git=args.include_git,
        memory_mode=args.memories,
        make_zip=not args.no_zip,
    )

    try:
        print(f"Resolving village {args.slug!r}…", file=sys.stderr)
        village = exporter.load_village(args.slug)
        days = choose_days(args, village.dates)
        if not days:
            raise ValueError("No active days matched that selection.")
        if args.list_days:
            print("\n".join(days))
            return 0

        print(
            f"Exporting {len(days)} day(s) for {village.name} with a {http.default_delay:.2f}s minimum official-API delay.",
            file=sys.stderr,
        )
        results = []
        failures = []
        for index, day in enumerate(days, 1):
            print(f"[{index}/{len(days)}] {day}", file=sys.stderr)
            try:
                result = exporter.export_day(village, day, overwrite=args.refresh)
                results.append(result)
                if result["skipped"]:
                    print("  complete export already exists; skipped", file=sys.stderr)
                else:
                    c = result["manifest"]["counts"]
                    print(
                        f"  saved {c['rawEvents']} raw events, {c['messages']} messages, {c['activities']} activities",
                        file=sys.stderr,
                    )
            except Exception as exc:
                failures.append({"day": day, "error": str(exc)})
                print(f"  ERROR: {exc}", file=sys.stderr)
                if not args.keep_going:
                    break

        run_manifest = {
            "village": {"id": village.id, "slug": village.slug, "name": village.name},
            "selectedDays": days,
            "completed": [r["day"] for r in results],
            "failures": failures,
            "networkRequests": http.network_requests,
            "cacheHits": http.cache_hits,
        }
        output.mkdir(parents=True, exist_ok=True)
        (output / "last-run.json").write_text(json.dumps(run_manifest, indent=2) + "\n", encoding="utf-8")
        print(
            f"Done: {len(results)} day(s), {len(failures)} failure(s), {http.network_requests} network request(s), {http.cache_hits} cache hit(s).",
            file=sys.stderr,
        )
        return 1 if failures else 0
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
