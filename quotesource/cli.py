"""qs — quotesource CLI.

Every command prints JSON to stdout (--pretty for human output).
Exit codes: 0 success, 1 error (error JSON on stderr), 2 usage.
"""
import argparse
import json
import sys


def _out(data, pretty: bool):
    if pretty:
        _pretty_print(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def _fail(msg: str, code: int = 1):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def _pretty_print(data):
    if isinstance(data, list):
        if not data:
            print("(none)")
            return
        for row in data:
            if isinstance(row, dict):
                line = "  ".join(f"{v}" for v in _pretty_row(row))
                print(line)
            else:
                print(row)
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"{k}: {v}")
    else:
        print(data)


def _pretty_row(row: dict):
    prefer = ["id", "episode_id", "status", "upload_date", "type", "name", "title", "url"]
    seen = []
    for k in prefer:
        if k in row and row[k] is not None:
            seen.append(row[k])
    return seen or list(row.values())


def cmd_sources(args):
    from . import registry

    if args.action == "list":
        _out(registry.list_sources(), args.pretty)
    elif args.action == "add":
        if not (args.id and args.name and args.type and args.url):
            _fail("add requires --id --name --type --url", 2)
        entry = registry.add_source(
            args.id, args.name, args.type, args.url,
            people=args.people, notes=args.notes or "",
        )
        _out(entry, args.pretty)
    elif args.action == "remove":
        if not args.id:
            _fail("remove requires --id", 2)
        ok = registry.remove_source(args.id)
        if not ok:
            _fail(f"source '{args.id}' not found")
        _out({"removed": args.id}, args.pretty)


def cmd_ingest(args):
    from . import registry
    from .ingest import ingest_source

    if args.all:
        sources = registry.list_sources()
        if not sources:
            _fail("no sources registered")
    else:
        if not args.source_id:
            _fail("ingest requires a source id or --all", 2)
        src = registry.get_source(args.source_id)
        if not src:
            _fail(f"source '{args.source_id}' not found")
        sources = [src]

    results = []
    for src in sources:
        try:
            results.append(ingest_source(src, limit=args.limit, quiet=args.quiet))
        except Exception as e:
            results.append({"source": src["id"], "error": str(e)})
    _out(results if args.all else results[0], args.pretty)
    if any("error" in r for r in results):
        sys.exit(1)


def cmd_episodes(args):
    from .ingest import list_episodes

    _out(list_episodes(args.source_id), args.pretty)


def main(argv=None):
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--pretty", action="store_true", help="human output instead of JSON")

    parser = argparse.ArgumentParser(
        prog="qs", description="quotesource — spoken-word sourcing for palette",
        parents=[shared],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", help="manage the source registry", parents=[shared])
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("--id")
    p.add_argument("--name")
    p.add_argument("--type", choices=["youtube_channel", "youtube_playlist", "rss"])
    p.add_argument("--url")
    p.add_argument("--people", nargs="*", default=None)
    p.add_argument("--notes")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("ingest", help="fetch episode metadata and captions", parents=[shared])
    p.add_argument("source_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="max new episodes per source")
    p.add_argument("--quiet", action="store_true", help="suppress progress lines")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("episodes", help="list episodes for a source with transcript status", parents=[shared])
    p.add_argument("source_id")
    p.set_defaults(func=cmd_episodes)

    args = parser.parse_args(argv)

    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        _fail(str(e))


if __name__ == "__main__":
    main()
