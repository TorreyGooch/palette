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


def cmd_index(args):
    from .indexer import build_index

    _out(build_index(rebuild=args.rebuild, quiet=args.quiet), args.pretty)


def cmd_embed(args):
    from .embedder import embed_pending

    _out(embed_pending(batch_size=args.batch_size, limit=args.limit,
                       reset=args.reset, quiet=args.quiet), args.pretty)


def cmd_search(args):
    from .embedder import embed_stats, semantic_search

    stats = embed_stats()
    if stats["embedded"] == 0:
        _fail("no embeddings yet — run: qs embed")
    hits = semantic_search(args.query, source=args.source, person=args.person,
                           after=args.after, before=args.before, limit=args.limit)
    if args.pretty:
        if stats["coverage"] < 1.0:
            print(f"(note: {stats['coverage']:.0%} of chunks embedded)")
        _print_hits(hits)
    else:
        _out({"coverage": stats["coverage"], "hits": hits}, False)


def _print_hits(hits):
    for h in hits:
        print(f"[{h['score']:6.2f}] {h['episode_id']} {h['start']:8.1f}s  {(h['episode_title'] or '')[:50]}")
        print(f"         {h['text'][:160]}")
        print(f"         {h['url_ts']}")


def cmd_grep(args):
    from .search import grep

    hits = grep(args.terms, source=args.source, person=args.person,
                after=args.after, before=args.before, limit=args.limit)
    if args.pretty:
        _print_hits(hits)
    else:
        _out(hits, False)


def cmd_context(args):
    from .search import context

    if args.timestamp is None and not args.range:
        _fail("context requires a timestamp or --range START END", 2)
    range_ = tuple(args.range) if args.range else None
    data = context(args.episode_id, timestamp=args.timestamp,
                   range_=range_, window=args.window)
    if args.pretty:
        print(f"{data['episode_title']}  [{data['window'][0]:.1f} – {data['window'][1]:.1f}s]")
        for s in data["segments"]:
            print(f"  {s['start']:8.1f}  {s['text']}")
    else:
        _out(data, False)


def cmd_episode_info(args):
    from .search import episode_info

    _out(episode_info(args.episode_id), args.pretty)


def cmd_pull(args):
    from .pull import pull

    item = pull(args.episode_id, args.range[0], args.range[1],
                mode=args.mode, palette_name=args.palette,
                person=args.person, pad=args.pad)
    _out(item, args.pretty)


def cmd_status(args):
    from .status import corpus_status

    data = corpus_status()
    if not args.pretty:
        _out(data, False)
        return
    print(f"data root: {data['data_root']}")
    print(f"disk: {data['disk']['total_mb']} MB")
    idx = data.get("index") or {}
    if idx.get("exists"):
        print(f"index: {idx['episodes']} episodes, {idx['chunks']} chunks, {idx['db_mb']} MB")
    emb = data.get("embeddings")
    if emb and emb.get("embedded"):
        print(f"embeddings: {emb['embedded']}/{emb['chunks']} ({emb['coverage']:.0%}) [{emb['model']}]")
    print()
    hdr = f"{'source':<22}{'episodes':>9}{'captions':>9}{'whisper':>9}{'needs_tx':>9}{'pending':>9}"
    print(hdr)
    print("-" * len(hdr))
    for s in data["sources"]:
        tag = "" if s["registered"] else "  (unregistered)"
        print(f"{s['source']:<22}{s['episodes']:>9}{s['captions']:>9}{s['whisper']:>9}"
              f"{s['needs_transcription']:>9}{s['captions_pending']:>9}{tag}")
    t = data["totals"]
    print("-" * len(hdr))
    print(f"{'TOTAL':<22}{t['episodes']:>9}{t['captions']:>9}{t['whisper']:>9}"
          f"{t['needs_transcription']:>9}{t['captions_pending']:>9}")


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

    p = sub.add_parser("status", help="corpus-wide progress and disk usage", parents=[shared])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("index", help="build/refresh the search index (incremental)", parents=[shared])
    p.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_index)

    filt = argparse.ArgumentParser(add_help=False)
    filt.add_argument("--source", help="filter by source id")
    filt.add_argument("--person", help="match source people lists and episode title/description")
    filt.add_argument("--after", help="upload date >= (YYYY-MM-DD)")
    filt.add_argument("--before", help="upload date <= (YYYY-MM-DD)")
    filt.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("grep", help="full-text keyword search over transcript chunks",
                       parents=[shared, filt])
    p.add_argument("terms", help='FTS5 query: words, "quoted phrases", OR, NOT, prefix*')
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("search", help="semantic search over transcript chunks",
                       parents=[shared, filt])
    p.add_argument("query")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("embed", help="embed index chunks (resumable batch job)",
                       parents=[shared])
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--reset", action="store_true", help="drop all vectors and re-embed")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("context", help="read raw transcript around a point or range",
                       parents=[shared])
    p.add_argument("episode_id")
    p.add_argument("timestamp", nargs="?", type=float, default=None)
    p.add_argument("--range", nargs=2, type=float, metavar=("START", "END"))
    p.add_argument("--window", type=float, default=30.0, help="seconds around timestamp (default 30)")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("episode-info", help="full metadata plus transcript stats",
                       parents=[shared])
    p.add_argument("episode_id")
    p.set_defaults(func=cmd_episode_info)

    p = sub.add_parser("pull", help="fetch a verified range and stage it on a palette",
                       parents=[shared])
    p.add_argument("episode_id")
    p.add_argument("--range", nargs=2, type=float, required=True, metavar=("START", "END"))
    p.add_argument("--mode", choices=["audio", "av"], default="av")
    p.add_argument("--palette", help="palette name (created if missing)")
    p.add_argument("--person", help="speaker for attribution + tag")
    p.add_argument("--pad", type=float, default=0.0, help="extra seconds each side after snapping")
    p.set_defaults(func=cmd_pull)

    args = parser.parse_args(argv)

    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        _fail(str(e))


if __name__ == "__main__":
    main()
