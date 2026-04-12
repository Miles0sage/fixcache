"""
cli.py — Command-line interface for lore-memory.

Usage:
    lore-memory remember "User prefers dark mode"
    lore-memory recall "theme preference"
    lore-memory stats
    lore-memory identity get
    lore-memory identity set name=Miles role=CTO
    lore-memory teach "we use pnpm not npm"
    lore-memory fix "ECONNREFUSED.*5432" --steps "docker compose up -d postgres" "pg_isready"
    lore-memory sync [--dir PATH] [--format claude|cursor|codex|all]
    lore-memory hook install [--dir PATH]
"""

from __future__ import annotations

import argparse
import json
import sys

from . import LoreMemory
from .config import LoreConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lore-memory",
        description="Memory that learns from forgetting.",
    )
    parser.add_argument(
        "--db", metavar="PATH", help="Path to SQLite database file"
    )
    parser.add_argument(
        "--config", metavar="PATH", help="Path to .lore-memory.yml config file"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # remember
    rem = sub.add_parser("remember", help="Store a memory")
    rem.add_argument("content", help="Text to remember")
    rem.add_argument(
        "--type",
        dest="memory_type",
        default="fact",
        choices=["fact", "experience", "opinion", "meta"],
        help="Memory type (default: fact)",
    )
    rem.add_argument("--format", dest="source_format", help="Source format label")

    # recall
    rec = sub.add_parser("recall", help="Retrieve memories by query")
    rec.add_argument("query", help="Search query")
    rec.add_argument("--top-k", type=int, default=5, help="Max results (default: 5)")
    rec.add_argument("--type", dest="memory_type", help="Filter by memory type")
    rec.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    # stats
    sub.add_parser("stats", help="Show memory statistics")

    # identity subcommands
    id_parser = sub.add_parser("identity", help="Manage L0 identity")
    id_sub = id_parser.add_subparsers(dest="id_command", required=True)

    id_sub.add_parser("get", help="Show current identity")

    id_set = id_sub.add_parser("set", help="Set identity fields (key=value ...)")
    id_set.add_argument("pairs", nargs="+", metavar="key=value")

    id_sub.add_parser("clear", help="Clear identity")

    # teach
    teach = sub.add_parser("teach", help="Store a convention or rule as a fact")
    teach.add_argument("convention", help="Convention, rule, or preference to remember")
    teach.add_argument(
        "--tags",
        nargs="*",
        metavar="TAG",
        help="Optional tags",
    )

    # fix
    fix = sub.add_parser("fix", help="Store an error recipe in procedural memory")
    fix.add_argument("error_signature", help="Error string or regex pattern")
    fix.add_argument(
        "--steps",
        nargs="+",
        metavar="STEP",
        required=True,
        help="Ordered solution steps",
    )
    fix.add_argument("--tags", nargs="*", metavar="TAG", help="Optional tags")
    fix.add_argument(
        "--outcome",
        default="success",
        choices=["success", "failure", "partial", "corrected"],
        help="Outcome of applying this fix (default: success)",
    )

    # sync
    sync = sub.add_parser("sync", help="Export memories to agent config files")
    sync.add_argument(
        "--dir",
        dest="project_dir",
        default=".",
        metavar="PATH",
        help="Project directory (default: current directory)",
    )
    sync.add_argument(
        "--format",
        dest="sync_format",
        default="all",
        choices=["claude", "cursor", "windsurf", "codex", "all"],
        help="Output format (default: all)",
    )

    # ingest-wiki
    iw = sub.add_parser("ingest-wiki", help="Ingest wiki articles into the cognition base")
    iw.add_argument(
        "--dir",
        dest="wiki_dir",
        default="/root/lore/wiki",
        metavar="PATH",
        help="Path to wiki directory containing .md files (default: /root/lore/wiki)",
    )
    iw.add_argument(
        "--tags",
        nargs="*",
        metavar="TAG",
        help="Optional extra tags to apply to all ingested memories",
    )

    # darwin — Darwin Replay: classify/stats/export of failure fingerprints
    darwin = sub.add_parser("darwin", help="Darwin Replay: fingerprints + efficacy")
    darwin_sub = darwin.add_subparsers(dest="darwin_command", required=True)

    d_cls = darwin_sub.add_parser("classify", help="Classify an error → fingerprint + ranked recipes")
    d_cls.add_argument("error_text", help="Raw error text (or - for stdin)")
    d_cls.add_argument("--top-k", type=int, default=3, help="Max recipes to show")
    d_cls.add_argument("--json", dest="darwin_json", action="store_true")

    d_stats = darwin_sub.add_parser("stats", help="Corpus-wide Darwin stats")
    d_stats.add_argument("--json", dest="darwin_json", action="store_true")

    d_exp = darwin_sub.add_parser("export", help="Export sanitized fingerprint corpus")
    d_exp.add_argument("--min-seen", type=int, default=1, help="Floor for inclusion")
    d_exp.add_argument("--out", help="Write JSON to this path (default: stdout)")

    # ingest — parse Claude Code transcripts for auto-fix recipes
    ing = sub.add_parser("ingest", help="Ingest Claude Code transcripts")
    ing_sub = ing.add_subparsers(dest="ingest_command", required=True)

    ing_trans = ing_sub.add_parser("transcript", help="Ingest a specific transcript file")
    ing_trans.add_argument("path", help="Path to Claude Code session .jsonl")
    ing_trans.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract recipes but do not store them",
    )

    ing_last = ing_sub.add_parser(
        "last-session", help="Ingest the most recent Claude Code transcript"
    )
    ing_last.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract recipes but do not store them",
    )

    # doctor
    doc = sub.add_parser("doctor", help="Health check and self-repair")
    doc.add_argument(
        "--fix",
        action="store_true",
        help="Attempt to repair any fixable issues",
    )
    doc.add_argument(
        "--json",
        dest="doctor_json",
        action="store_true",
        help="Output as JSON",
    )

    # hook
    hook_parser = sub.add_parser("hook", help="Manage Claude Code hooks")
    hook_sub = hook_parser.add_subparsers(dest="hook_command", required=True)
    hook_install = hook_sub.add_parser("install", help="Install lore-memory hooks")
    hook_install.add_argument(
        "--dir",
        dest="project_dir",
        default=".",
        metavar="PATH",
        help="Project directory (default: current directory)",
    )

    return parser


def _open_mem(args: argparse.Namespace) -> LoreMemory:
    cfg = LoreConfig(config_path=getattr(args, "config", None))
    db_path = getattr(args, "db", None) or cfg.db_path
    return LoreMemory(db_path=db_path, config=cfg)


def _cmd_remember(args: argparse.Namespace, mem: LoreMemory) -> int:
    mid = mem.remember(
        content=args.content,
        memory_type=args.memory_type,
        source_format=getattr(args, "source_format", None),
    )
    print(f"Stored: {mid}")
    return 0


def _cmd_recall(args: argparse.Namespace, mem: LoreMemory) -> int:
    results = mem.recall(query=args.query, top_k=args.top_k, memory_type=args.memory_type)
    if not results:
        print("No results found.")
        return 0

    if getattr(args, "as_json", False):
        print(json.dumps(results, indent=2, default=str))
        return 0

    for i, r in enumerate(results, 1):
        mtype = r.get("memory_type", "?")
        content = r.get("content", "")
        mid = r.get("id", "?")
        print(f"[{i}] ({mtype}) {content[:120]}")
        print(f"     id={mid}")
    return 0


def _cmd_stats(mem: LoreMemory) -> int:
    s = mem.stats()
    print(f"Total memories : {s['total']}")
    print(f"  fact         : {s['by_type'].get('fact', 0)}")
    print(f"  experience   : {s['by_type'].get('experience', 0)}")
    print(f"  opinion      : {s['by_type'].get('opinion', 0)}")
    print(f"  meta         : {s['by_type'].get('meta', 0)}")
    print(f"WAL entries    : {s['wal_entries']}")
    print(f"Decay avg/min/max: {s['decay_avg']}/{s['decay_min']}/{s['decay_max']}")
    print(f"Identity       : {'set' if s['identity_configured'] else 'not set'} "
          f"(~{s['identity_tokens']} tokens)")
    return 0


def _cmd_identity(args: argparse.Namespace, mem: LoreMemory) -> int:
    cmd = args.id_command
    if cmd == "get":
        print(mem.identity.render())
        return 0
    if cmd == "set":
        pairs: dict = {}
        for p in args.pairs:
            if "=" not in p:
                print(f"Invalid pair (expected key=value): {p!r}", file=sys.stderr)
                return 1
            k, v = p.split("=", 1)
            pairs[k.strip()] = v.strip()
        mem.identity.update(pairs)
        print(f"Identity updated: {list(pairs.keys())}")
        return 0
    if cmd == "clear":
        mem.identity.clear()
        print("Identity cleared.")
        return 0
    return 1


def _cmd_teach(args: argparse.Namespace, mem: LoreMemory) -> int:
    import hashlib, json, time as _time
    content = args.convention
    tags = getattr(args, "tags", None) or []
    now = _time.time()
    prov = hashlib.sha256(f"{content}{now}".encode()).hexdigest()
    meta = json.dumps({
        "convention": True,
        "tags": tags,
        "trust_score": 1.0,
        "source_type": "user",
        "provenance_hash": prov,
    })
    mid = mem.store.add(
        content=content,
        memory_type="fact",
        metadata=meta,
    )
    print(f"Stored convention: {mid}")
    return 0


def _cmd_fix(args: argparse.Namespace, mem: LoreMemory) -> int:
    """
    CLI wrapper for `lore-memory fix`. Delegates to handle_lore_fix so the
    CLI path and MCP path share the same transaction semantics, fingerprint
    upsert, and validation logic.
    """
    from .mcp.server import handle_lore_fix
    import lore_memory.mcp.server as server_mod

    # Wire the module-level MCP store to the CLI's store so handle_lore_fix
    # writes to the same database the user opened.
    server_mod._store = mem.store
    try:
        result = handle_lore_fix(
            error_signature=args.error_signature,
            solution_steps=args.steps,
            tags=getattr(args, "tags", None) or [],
            outcome=args.outcome,
        )
    finally:
        server_mod._store = None

    if not result.get("success"):
        print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
        return 1

    print(f"Stored fix: recipe_id={result['recipe_id']}")
    print(f"  error_signature : {result['error_signature']}")
    print(f"  steps           : {result['steps_count']}")
    print(f"  memory_id       : {result['memory_id']}")
    print(f"  fingerprint     : {result['fingerprint_hash']} ({result['fingerprint_error_type']}/{result['fingerprint_ecosystem']})")
    return 0


def _cmd_ingest_wiki(args: argparse.Namespace, mem: LoreMemory) -> int:
    from .cognition import ingest_wiki, extract_procedures
    from pathlib import Path

    wiki_dir = Path(args.wiki_dir).expanduser().resolve()
    tags = getattr(args, "tags", None) or []

    print(f"Ingesting wiki articles from: {wiki_dir}")
    ingest_result = ingest_wiki(mem.store, wiki_dir, tags=tags or None)
    proc_result = extract_procedures(mem.store, wiki_dir)

    print(f"Articles ingested : {ingest_result['ingested']}")
    print(f"Articles skipped  : {ingest_result['skipped']} (already ingested)")
    print(f"Sections stored   : {ingest_result['sections']}")
    print(f"Procedures found  : {proc_result['procedures_extracted']}")
    return 0


def _cmd_sync(args: argparse.Namespace, mem: LoreMemory) -> int:
    from .sync import (
        sync_claude_md, sync_cursorrules, sync_windsurfrules,
        sync_agents_md, sync_all,
    )
    from pathlib import Path

    project_dir = Path(args.project_dir).expanduser().resolve()
    fmt = args.sync_format

    if fmt == "all":
        report = sync_all(mem.store, project_dir)
        synced = report["synced"]
        created = report["created"]
        if synced:
            print(f"Updated : {', '.join(synced)}")
        if created:
            print(f"Created : {', '.join(created)}")
        if not synced and not created:
            print("Nothing to sync.")
    elif fmt == "claude":
        path = project_dir / "CLAUDE.md"
        sync_claude_md(mem.store, path)
        print(f"Written : {path}")
    elif fmt == "cursor":
        path = project_dir / ".cursorrules"
        sync_cursorrules(mem.store, path)
        print(f"Written : {path}")
    elif fmt == "windsurf":
        path = project_dir / ".windsurfrules"
        sync_windsurfrules(mem.store, path)
        print(f"Written : {path}")
    elif fmt == "codex":
        path = project_dir / "AGENTS.md"
        sync_agents_md(mem.store, path)
        print(f"Written : {path}")
    return 0


def _cmd_darwin(args: argparse.Namespace, mem: LoreMemory) -> int:
    from .darwin_replay import classify, darwin_stats, export_sanitized

    sub_cmd = args.darwin_command

    if sub_cmd == "classify":
        text = args.error_text
        if text == "-":
            text = sys.stdin.read()
        result = classify(mem.store, text, top_k=args.top_k)
        if getattr(args, "darwin_json", False):
            print(json.dumps(result, indent=2, default=str))
            return 0
        fp = result["fingerprint"]
        print(f"Fingerprint : {fp['hash']}  ({fp['error_type']} / {fp['ecosystem']})")
        print(f"Essence     : {fp['essence']}")
        stats = result.get("fingerprint_stats")
        if stats:
            eff = stats.get("efficacy")
            eff_str = f"{eff:.0%}" if eff is not None else "unrated"
            print(f"Seen {stats['total_seen']}x — {stats['total_success']} pass, {stats['total_failure']} fail — efficacy {eff_str}")
        else:
            print("Seen 0x — first time this class of failure has been fingerprinted.")
        print()
        if result["candidates"]:
            print(f"Top {len(result['candidates'])} recipes:")
            for i, c in enumerate(result["candidates"], 1):
                print(f"  [{i}] confidence={c['confidence']}  freq={c['frequency']}")
                print(f"      {c['description'][:120]}")
                for step in c["solution_steps"][:5]:
                    print(f"      → {step}")
        else:
            print("No recipes found for this fingerprint yet.")
        return 0

    if sub_cmd == "stats":
        s = darwin_stats(mem.store)
        if getattr(args, "darwin_json", False):
            print(json.dumps(s, indent=2, default=str))
            return 0
        print(f"Total fingerprints : {s['total_fingerprints']}")
        print(f"Total seen events  : {s['total_seen_events']}")
        print(f"Successes          : {s['total_success']}")
        print(f"Failures           : {s['total_failure']}")
        if s["overall_efficacy"] is not None:
            print(f"Overall efficacy   : {s['overall_efficacy']:.1%}")
        else:
            print("Overall efficacy   : (no outcomes yet)")
        print()
        if s["top_ecosystems"]:
            print("Top ecosystems:")
            for eco, count in s["top_ecosystems"].items():
                print(f"  {eco:12s}  {count}")
        if s["top_error_types"]:
            print("Top error types:")
            for et, count in s["top_error_types"].items():
                print(f"  {et:24s}  {count}")
        print()
        print("Efficacy bands:")
        for band, count in s["efficacy_bands"].items():
            print(f"  {band:10s}  {count}")
        return 0

    if sub_cmd == "export":
        corpus = export_sanitized(mem.store, min_total_seen=args.min_seen)
        payload = json.dumps(corpus, indent=2)
        if args.out:
            from pathlib import Path
            Path(args.out).write_text(payload, encoding="utf-8")
            print(f"Wrote {len(corpus)} fingerprints to {args.out}")
        else:
            print(payload)
        return 0

    return 1


def _cmd_ingest(args: argparse.Namespace, mem: LoreMemory) -> int:
    from .ingest import ingest_transcript, find_latest_transcript
    from pathlib import Path

    sub_cmd = args.ingest_command
    dry = bool(getattr(args, "dry_run", False))

    if sub_cmd == "transcript":
        path = Path(args.path).expanduser().resolve()
        if not path.exists():
            print(f"Transcript not found: {path}", file=sys.stderr)
            return 1
    elif sub_cmd == "last-session":
        path = find_latest_transcript()
        if path is None:
            print("No Claude Code transcripts found under ~/.claude/projects/", file=sys.stderr)
            return 1
        print(f"Using transcript: {path}")
    else:
        return 1

    report = ingest_transcript(mem.store, path, dry_run=dry)
    print(f"Messages scanned  : {report['total_messages']}")
    print(f"Recipes extracted : {report['recipes_extracted']}")
    if dry:
        print("(dry run — nothing stored)")
    else:
        print(f"Recipes stored    : {report['recipes_stored']}")
    if report["recipes"]:
        print()
        print("Top recipes:")
        for i, r in enumerate(report["recipes"][:5], 1):
            sig = r["error_signature"][:80]
            print(f"  [{i}] {sig}")
            for step in r["solution_steps"][:3]:
                print(f"      → {step}")
            if len(r["solution_steps"]) > 3:
                print(f"      → (+{len(r['solution_steps']) - 3} more)")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor, format_report
    from .config import LoreConfig

    cfg = LoreConfig(config_path=getattr(args, "config", None))
    db_path = getattr(args, "db", None) or cfg.db_path

    report = run_doctor(db_path, fix=bool(getattr(args, "fix", False)))

    if getattr(args, "doctor_json", False):
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report))
    return 0 if report.healthy else 2


def _cmd_hook(args: argparse.Namespace) -> int:
    from .hooks import install_claude_hooks
    from pathlib import Path

    if args.hook_command == "install":
        project_dir = Path(args.project_dir).expanduser().resolve()
        result = install_claude_hooks(project_dir)
        print(f"Settings: {result['path']}")
        if result["hooks_added"]:
            print(f"Added   : {', '.join(result['hooks_added'])}")
        if result["already_present"]:
            print(f"Present : {', '.join(result['already_present'])}")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # hook and doctor commands do not need the memory store open
    if args.command == "hook":
        return _cmd_hook(args)
    if args.command == "doctor":
        return _cmd_doctor(args)

    with _open_mem(args) as mem:
        cmd = args.command
        if cmd == "remember":
            return _cmd_remember(args, mem)
        if cmd == "recall":
            return _cmd_recall(args, mem)
        if cmd == "stats":
            return _cmd_stats(mem)
        if cmd == "identity":
            return _cmd_identity(args, mem)
        if cmd == "teach":
            return _cmd_teach(args, mem)
        if cmd == "fix":
            return _cmd_fix(args, mem)
        if cmd == "sync":
            return _cmd_sync(args, mem)
        if cmd == "ingest-wiki":
            return _cmd_ingest_wiki(args, mem)
        if cmd == "ingest":
            return _cmd_ingest(args, mem)
        if cmd == "darwin":
            return _cmd_darwin(args, mem)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
