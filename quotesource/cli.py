"""qs — quotesource CLI.

Every command prints JSON to stdout (--pretty for human output).
Exit codes: 0 success, 1 error (error JSON on stderr), 2 usage.
"""
import argparse
import json
import re
import sys
import time


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
        # Typing the natural positional form used to produce "unrecognized
        # arguments:" followed by every argument passed - which names what
        # was ignored and never what was wanted, and made --pretty look
        # rejected too because it was in the list. Both forms work now, and
        # what is missing is named either way.
        rest = list(getattr(args, "rest", None) or [])
        fields = {}
        for i, key in enumerate(("id", "name", "type", "url")):
            fields[key] = getattr(args, key) or (rest[i] if len(rest) > i else None)
        missing = [f"--{k}" for k in ("id", "name", "type", "url")
                   if not fields[k]]
        if missing:
            _fail(f"sources add needs {' '.join(missing)}  "
                  f"(or positionally: qs sources add <id> <name> <type> <url>)",
                  2)
        entry = registry.add_source(
            fields["id"], fields["name"], fields["type"], fields["url"],
            people=args.people, notes=args.notes or "",
            min_duration=args.min_duration,
        )
        _out(entry, args.pretty)
    elif args.action == "remove":
        rest = list(getattr(args, "rest", None) or [])
        source_id = args.id or (rest[0] if rest else None)
        if not source_id:
            _fail("sources remove needs --id (or: qs sources remove <id>)", 2)
        ok = registry.remove_source(source_id)
        if not ok:
            _fail(f"source '{source_id}' not found")
        _out({"removed": source_id}, args.pretty)


def _guest_source_id(person: str) -> str:
    """A stable source id for one person's scattered appearances."""
    slug = re.sub(r"[^a-z0-9]+", "_", (person or "").lower()).strip("_")
    return f"guest_{slug}" if slug else "guest"


def cmd_guest(args):
    """Individually added episodes, grouped by the person worth quoting.

    A guest source exists so `--person` keeps working: search already treats
    every episode of a source whose `people` list names someone as that
    person's, so grouping by person rather than by show is what makes these
    findable later.
    """
    from . import registry
    from .ingest import add_episode, remove_episode, _pause

    if args.action == "list":
        _out([s for s in registry.list_sources() if s.get("type") == "episodes"],
             args.pretty)
        return

    if args.action == "remove":
        # Dry by default. What is in here can cost an hour of throttled
        # fetching to replace, so the removal is shown before it is done.
        if not args.urls:
            _fail("guest remove requires at least one episode id or URL", 2)
        from .ingest import youtube_id

        removed, results = 0, []
        for value in args.urls:
            episode_id = youtube_id(value) or value
            try:
                row = remove_episode(episode_id, source_id=args.source_id,
                                     apply=args.yes)
                results.append(row)
                removed += 1 if args.yes else 0
            except Exception as e:
                results.append({"episode_id": episode_id, "error": str(e)})
        out = {"removed": removed, "episodes": results}
        if not args.yes:
            out["note"] = "dry run - nothing was removed. Pass --yes to apply."
        else:
            out["next"] = "run `qs embed` if you want coverage recomputed"
        _out(out, args.pretty)
        return

    if not args.urls:
        _fail("guest add requires at least one URL", 2)
    if not args.person:
        _fail("guest add requires --person", 2)

    source_id = args.source_id or _guest_source_id(args.person)
    source = registry.get_source(source_id)
    if source is None:
        source = registry.add_source(
            source_id, f"{args.person} (appearances)", "episodes", "",
            people=[args.person],
            notes=args.notes or "Individually added episodes.")
    elif source.get("type") != "episodes":
        _fail(f"source '{source_id}' is a {source.get('type')}, not an "
              f"episodes source; pass --source-id to pick another")

    results, added, failed = [], 0, 0
    for index, url in enumerate(args.urls):
        if index:
            time.sleep(_pause())     # same politeness as a bulk ingest
        try:
            row = add_episode(url, source, quiet=args.quiet)
            results.append(row)
            added += 0 if row.get("already_had_it") else 1
        except Exception as e:
            failed += 1
            results.append({"url": url, "error": str(e)})
    _out({"source": source_id, "person": args.person,
          "added": added, "failed": failed,
          "next": "run `qs index` then `qs embed` to make these searchable",
          "episodes": results}, args.pretty)


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

    try:
        override = registry.parse_duration(args.min_duration)
    except ValueError as e:
        _fail(str(e), 2)

    results = []
    for src in sources:
        try:
            results.append(ingest_source(src, limit=args.limit, quiet=args.quiet,
                                         min_duration=override))
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


def cmd_fetch_audio(args):
    from .feedaudio import fetch_source_audio

    progress = None if args.quiet else (
        lambda line: print(f"  {line}", flush=True))
    _out(fetch_source_audio(args.source_id, limit=args.limit,
                            sleep_s=args.sleep, progress=progress), args.pretty)


def cmd_link_audio(args):
    from .feedaudio import link_matching

    result = link_matching(args.caption_source, args.feed_source,
                           tolerance=args.tolerance, apply=args.apply)
    if not args.apply:
        result["note"] = "dry run - pass --apply to link"
    _out(result, args.pretty)


def cmd_transcribe(args):
    from .transcribe import transcribe_batch, transcribe_episode
    from .search import _find_episode_dir

    if args.batch:
        _out(transcribe_batch(source=args.source, limit=args.limit,
                              quiet=args.quiet), args.pretty)
        return
    if not args.episode_id:
        _fail("transcribe requires an episode id or --batch", 2)
    ep_dir = _find_episode_dir(args.episode_id)
    if not ep_dir:
        _fail(f"episode '{args.episode_id}' not found")
    _out(transcribe_episode(ep_dir, quiet=args.quiet), args.pretty)


def cmd_words(args):
    from .cut import word_map

    res = word_map(args.episode_id, args.range[0], args.range[1],
                   pad=args.pad, model_size=args.model)
    if args.pretty:
        for w in res["words"]:
            mark = ""
            if w["gap_before"] and w["gap_before"] >= 0.15:
                mark = f"   <== PAUSE {w['gap_before']*1000:.0f}ms"
            print(f"{w['start']:9.2f}  {w['word']:<18}{mark}")
        print(f"\n{len(res['pauses'])} pause(s) >=150ms — cut just before one "
              f"for a clean tail")
    else:
        _out(res, False)


def _keep_working_copy(args) -> bool:
    """Whether an unstaged clip should stay in the library's media/ folder.

    Only the CLI can answer this: a remote job leaves the files there
    precisely so the caller can download them, and cleans up afterwards.
    Here the clip is already in the outbox and nothing else will ever come
    looking, so a second copy is just litter — unless there is no outbox, in
    which case media/ holds the only copy and must be left alone.
    """
    from .outbox import outbox_dir

    return not (args.no_stage and outbox_dir(args.outbox) is not None)


def cmd_cut(args):
    from .cut import cut_quote

    res = cut_quote(args.episode_id, args.range[0], args.range[1],
                    palette_name=args.palette, person=args.person,
                    model_size=args.model, stage=not args.no_stage,
                    outbox=args.outbox,
                    keep_working_copy=_keep_working_copy(args),
                    progress_cb=(None if args.quiet else
                                 lambda m: print(f"  {m}…", flush=True)))
    if args.pretty:
        d = res["cut_diagnostics"]
        print(f"{res['filename']}  ({res['duration']}s, {len(res['words'])} words)")
        print(f"  head  {'clean onset' if d.get('head_clean') else 'NO CLEAR ONSET'}"
              f"   lead silence {d.get('lead_silence_ms')} ms"
              f"   energy {d.get('head_energy_ratio')}x threshold")
        tail = ("clean pause" if d.get('tail_clean')
                else f"RUN-ON (faded {d.get('tail_faded_ms')} ms)")
        print(f"  tail  {tail}   trail silence {d.get('trail_silence_ms')} ms")
        if d.get("words_dropped_at_edges"):
            print(f"  dropped {d['words_dropped_at_edges']} partial word(s) at edges")
        print(f"  manifest      {res['manifest']}")
        print(f"  quote: {res['attribution']['quote_text'][:110]}")
    else:
        _out(res, False)


def cmd_pull(args):
    from .pull import pull

    item = pull(args.episode_id, args.range[0], args.range[1],
                mode=args.mode, palette_name=args.palette,
                person=args.person, pad=args.pad, rough=args.rough,
                outbox=args.outbox)
    _out(item, args.pretty)


def cmd_status(args):
    from .status import corpus_status

    data = corpus_status()
    # The ./qs wrapper hunts for an interpreter that actually has the deps;
    # an ad-hoc `python3 -c` does not, and fails with ModuleNotFoundError on
    # something the wrapper imports fine. Naming the one in use turns that
    # into a one-line answer instead of an investigation.
    data["python"] = sys.executable
    if not args.pretty:
        _out(data, False)
        return
    print(f"data root: {data['data_root']}")
    print(f"python: {data['python']}")
    print(f"disk: {data['disk']['total_mb']} MB")
    idx = data.get("index") or {}
    if idx.get("exists"):
        print(f"index: {idx['episodes']} episodes, {idx['chunks']} chunks, {idx['db_mb']} MB")
    emb = data.get("embeddings")
    if emb and emb.get("embedded"):
        print(f"embeddings: {emb['embedded']}/{emb['chunks']} ({emb['coverage']:.0%}) [{emb['model']}]")
        if emb.get("model_mismatch"):
            print(f"  WARNING: {emb['model_mismatch']}")
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


# Commands that read the corpus off disk. `sources` and `status` are useful
# even with no corpus, so they are not listed.
_NEEDS_CORPUS = {
    "episodes", "index", "embed", "grep", "search", "context",
    "episode-info", "transcribe", "pull", "words", "cut",
}
# `ingest` and `guest` are deliberately absent: both *build* a corpus, so
# refusing to run them when there is not one yet would forbid the first step.


def _warn_if_corpus_elsewhere(command: str):
    """Say where the corpus went instead of reporting an empty one.

    The CLI reads the corpus directly and cannot proxy to QS_REMOTE the way
    the web app does. When the corpus has moved to another machine, every
    command here would otherwise return zero results as though the corpus
    were merely empty, which reads like data loss rather than a relocation.
    """
    import os
    import sys

    from .paths import data_root

    if command not in _NEEDS_CORPUS:
        return
    # ensure_root() creates an empty episodes/ as scaffolding, so its mere
    # existence proves nothing — look for actual sources inside it.
    episodes = data_root() / "episodes"
    if episodes.is_dir() and any(episodes.iterdir()):
        return

    remote = os.environ.get("QS_REMOTE")
    print(f"No corpus at {data_root()}", file=sys.stderr)
    if remote:
        print(f"The app is configured to use the corpus at {remote}; the CLI "
              f"cannot proxy there.\nRun qs on that machine, or use the app's "
              f"Quotes page from here.", file=sys.stderr)
    else:
        print("Run `qs ingest` to build one, or point QUOTESOURCE_DATA at an "
              "existing corpus.", file=sys.stderr)
    raise SystemExit(1)


# A YouTube id is exactly 11 characters of [A-Za-z0-9_-], so roughly one in
# thirty starts with a dash — argparse then reads it as an unknown option and
# the command dies with "the following arguments are required". Real ids in
# the corpus include -RXD4bTuFTo and --xKsIgv7tE. No option in this CLI has
# that shape, so inserting the end-of-options marker is unambiguous.
_YT_ID = re.compile(r"-[A-Za-z0-9_-]{10}$")


def _protect_leading_dash_ids(argv):
    """Move a dash-leading episode id behind an end-of-options marker.

    The marker has to go last, not in front of the id where it sits: `--`
    makes everything after it positional, so protecting the id in place would
    stop the flags that follow from parsing at all. Moving the id to the end
    is safe because these commands take exactly one positional besides the
    subcommand, and a subcommand never has an id's shape.
    """
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if "--" in argv:
        return argv
    for i, tok in enumerate(argv):
        if _YT_ID.fullmatch(tok):
            return argv[:i] + argv[i + 1:] + ["--", tok]
    return argv


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
    p.add_argument("rest", nargs="*",
                   help="for add: <id> <name> <type> <url>, if you prefer "
                        "positionals to flags")
    p.add_argument("--id")
    p.add_argument("--name")
    p.add_argument("--type", choices=["youtube_channel", "youtube_playlist",
                                      "rss", "episodes"])
    p.add_argument("--url")
    p.add_argument("--people", nargs="*", default=None)
    p.add_argument("--notes")
    p.add_argument("--min-duration", default=None,
                   help="skip anything shorter, e.g. 30m or 1800; stored on the source")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("guest", parents=[shared],
                       help="add single episodes by URL, grouped by person")
    p.add_argument("action", choices=["add", "list", "remove"])
    p.add_argument("urls", nargs="*",
                   help="YouTube video URLs (or bare ids); ids for remove")
    p.add_argument("--yes", action="store_true",
                   help="actually remove; without it `remove` only reports")
    p.add_argument("--person", help="who this is worth quoting for")
    p.add_argument("--source-id", help="override the derived guest_<person> id")
    p.add_argument("--notes")
    p.add_argument("--quiet", action="store_true", help="suppress per-episode logging")
    p.set_defaults(func=cmd_guest)

    p = sub.add_parser("ingest", help="fetch episode metadata and captions", parents=[shared])
    p.add_argument("source_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="max new episodes per source")
    p.add_argument("--quiet", action="store_true", help="suppress progress lines")
    p.add_argument("--min-duration", default=None,
                   help="override the source's min_duration for this run")
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
    p.add_argument("--mode", choices=["audio", "av"], default="audio",
                   help="audio (default, ~50 MB) or av (~2.5 GB for a 2h episode)")
    p.add_argument("--palette", help="palette name (created if missing)")
    p.add_argument("--person", help="speaker for attribution + tag")
    p.add_argument("--pad", type=float, default=0.0, help="extra seconds each side after snapping")
    p.add_argument("--rough", action="store_true",
                   help="fast stream-copy: keyframe-aligned ~10s padding, original quality, trim downstream")
    p.add_argument("--outbox", default=None,
                   help="also copy the clip to this staging folder (default: $QS_OUTBOX, off if unset)")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("words", help="word timings + pauses for a region (pick cut boundaries with this)",
                       parents=[shared])
    p.add_argument("episode_id")
    p.add_argument("--range", nargs=2, type=float, required=True, metavar=("START", "END"))
    p.add_argument("--pad", type=float, default=3.0, help="seconds of context each side (default 3)")
    p.add_argument("--model", help="whisper model (default: env/auto; use small or better)")
    p.set_defaults(func=cmd_words)

    p = sub.add_parser("cut", help="word-accurate clip: whisper a window, snap to waveform, manifest",
                       parents=[shared])
    p.add_argument("episode_id")
    p.add_argument("--range", nargs=2, type=float, required=True, metavar=("START", "END"))
    p.add_argument("--palette")
    p.add_argument("--person")
    p.add_argument("--model", help="whisper model for the window (default: env/auto)")
    p.add_argument("--no-stage", action="store_true", help="write clip + manifest without adding to library")
    p.add_argument("--outbox", default=None,
                   help="also copy clip + manifest to this staging folder "
                        "(default: $QS_OUTBOX, off if unset)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_cut)

    p = sub.add_parser("fetch-audio",
                       help="download feed audio for an RSS source (no transcription)",
                       parents=[shared])
    p.add_argument("source_id")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--sleep", type=float, default=2.0,
                   help="seconds between requests (default 2)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_fetch_audio)

    p = sub.add_parser("link-audio",
                       help="lend feed audio to a captioned source's episodes",
                       parents=[shared])
    p.add_argument("caption_source", help="the source with transcripts, e.g. dwarkesh_yt")
    p.add_argument("feed_source", help="the source with audio, e.g. dwarkesh")
    p.add_argument("--tolerance", type=float, default=1.0,
                   help="max duration difference in seconds to treat as the "
                        "same timeline (default 1)")
    p.add_argument("--apply", action="store_true",
                   help="actually link; without it, report what would happen")
    p.set_defaults(func=cmd_link_audio)

    p = sub.add_parser("transcribe", help="whisper transcription (single or batch queue)",
                       parents=[shared])
    p.add_argument("episode_id", nargs="?")
    p.add_argument("--batch", action="store_true",
                   help="process the queue: needs_transcription, then auto, then manual captions")
    p.add_argument("--source", help="restrict batch to one source")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_transcribe)

    args = parser.parse_args(_protect_leading_dash_ids(argv))
    _warn_if_corpus_elsewhere(args.command)

    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        _fail(str(e))


if __name__ == "__main__":
    main()
