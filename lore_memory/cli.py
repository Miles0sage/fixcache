"""
cli.py — Command-line interface for fixcache.

Usage:
    fixcache watch pytest tests/
    fixcache fix "ECONNREFUSED.*5432" --steps "docker compose up -d postgres" "pg_isready"
    fixcache darwin classify "ModuleNotFoundError: No module named 'torch'"
    fixcache darwin stats
    fixcache darwin report <pattern_id> success
    fixcache darwin export --out corpus.json
    fixcache stats
    fixcache doctor [--fix] [--json]
    fixcache hook install [--dir PATH]
    fixcache activate [error_text|-]
    fixcache pack export [--out mypack.toml] [--name slug] [--version 0.1.0]
    fixcache pack import <name-or-path> [--policy merge|replace|skip] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys

from . import LoreMemory
from .config import LoreConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fixcache",
        description="Memory that learns from forgetting.",
    )
    parser.add_argument(
        "--db", metavar="PATH", help="Path to SQLite database file"
    )
    parser.add_argument(
        "--config", metavar="PATH", help="Path to .lore-memory.yml config file"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # watch — run a command; on failure, classify and surface matching recipes
    watch = sub.add_parser(
        "watch",
        help="Run a command and surface matching fix recipes on failure",
    )
    watch.add_argument(
        "--cmd",
        dest="watch_cmd",
        help="Shell command string to run (e.g. 'pytest tests/')",
    )
    watch.add_argument(
        "watch_argv",
        nargs=argparse.REMAINDER,
        help="Command tokens (alternative to --cmd); supports flags like '-c'",
    )
    watch.add_argument(
        "--json",
        dest="watch_json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable output",
    )
    watch.add_argument(
        "--suggest-only",
        action="store_true",
        help="Show suggestions but do not interrupt or prompt",
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

    # darwin — Darwin Replay: classify/stats/export of failure fingerprints
    darwin = sub.add_parser("darwin", help="Darwin Replay: fingerprints + efficacy")
    darwin_sub = darwin.add_subparsers(dest="darwin_command", required=True)

    d_cls = darwin_sub.add_parser("classify", help="Classify an error → fingerprint + ranked recipes")
    d_cls.add_argument("error_text", help="Raw error text (or - for stdin)")
    d_cls.add_argument("--top-k", type=int, default=3, help="Max recipes to show")
    d_cls.add_argument("--json", dest="darwin_json", action="store_true")

    d_stats = darwin_sub.add_parser("stats", help="Corpus-wide Darwin stats")
    d_stats.add_argument("--json", dest="darwin_json", action="store_true")

    d_rep = darwin_sub.add_parser("report", help="Record fix outcome (closes the Darwin feedback loop)")
    d_rep.add_argument("pattern_id", help="Pattern ID from a 'watch' suggestion")
    d_rep.add_argument("outcome", choices=["success", "failure"], help="Outcome of applying the fix")

    d_exp = darwin_sub.add_parser("export", help="Export sanitized fingerprint corpus")
    d_exp.add_argument("--min-seen", type=int, default=1, help="Floor for inclusion")
    d_exp.add_argument("--out", help="Write JSON to this path (default: stdout)")

    # stats
    sub.add_parser("stats", help="Show memory statistics")

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
    hook_install = hook_sub.add_parser("install", help="Install fixcache hooks")
    hook_install.add_argument(
        "--dir",
        dest="project_dir",
        default=".",
        metavar="PATH",
        help="Project directory (default: current directory)",
    )

    # activate — stateless hook entry point: classify an error, return recipes
    act = sub.add_parser(
        "activate",
        help="Hook entry point: classify stderr/error and emit top recipes",
    )
    act.add_argument(
        "error_text",
        nargs="?",
        default="-",
        help="Error text or '-' to read from stdin",
    )
    act.add_argument(
        "--top-k", type=int, default=3, help="Max recipes to return"
    )
    act.add_argument(
        "--json", dest="activate_json", action="store_true",
        help="Emit machine-readable JSON",
    )

    # pack — Lore Registry: publish and consume fix recipe packs
    pack_parser = sub.add_parser("pack", help="Lore Registry: publish/consume fix recipe packs")
    pack_sub = pack_parser.add_subparsers(dest="pack_command", required=True)

    p_exp = pack_sub.add_parser("export", help="Export local recipes to a pack file")
    p_exp.add_argument("--out", metavar="PATH",
                       help="Output .toml path (default: stdout)")
    p_exp.add_argument("--name", default="my-pack",
                       help="Pack name slug (default: my-pack)")
    p_exp.add_argument("--version", default="0.1.0",
                       help="Pack version (default: 0.1.0)")
    p_exp.add_argument("--description", default="fixcache memory pack",
                       help="One-line description")
    p_exp.add_argument("--author", default="",
                       help="Author string")
    p_exp.add_argument("--license", default="MIT", dest="license_",
                       help="License identifier (default: MIT)")
    p_exp.add_argument("--ecosystem", metavar="ECO",
                       help="Filter to one ecosystem (python, node, rust, ...)")
    p_exp.add_argument("--min-seen", type=int, default=1, metavar="N",
                       help="Only export fingerprints seen >= N times (default: 1)")

    p_imp = pack_sub.add_parser("import", help="Import a pack file or install from registry",
                                aliases=["install"])
    p_imp.add_argument("source",
                       help="Pack name (registry) or local .toml file path")
    p_imp.add_argument("--policy", default="merge",
                       choices=["merge", "replace", "skip"],
                       help="Conflict resolution (default: merge)")
    p_imp.add_argument("--dry-run", action="store_true",
                       help="Preview without writing to DB")
    p_imp.add_argument("--pin", metavar="VERSION",
                       help="Require exact pack version")

    return parser


def _open_mem(args: argparse.Namespace) -> LoreMemory:
    cfg = LoreConfig(config_path=getattr(args, "config", None))
    db_path = getattr(args, "db", None) or cfg.db_path
    return LoreMemory(db_path=db_path, config=cfg)


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


def _cmd_fix(args: argparse.Namespace, mem: LoreMemory) -> int:
    from .mcp.server import handle_lore_fix
    import lore_memory.mcp.server as server_mod

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


def _cmd_watch(args: argparse.Namespace, mem: LoreMemory) -> int:
    import shlex
    from .watch import watch_command

    cmd_str = getattr(args, "watch_cmd", None)
    if cmd_str:
        cmd = shlex.split(cmd_str)
    else:
        cmd = list(getattr(args, "watch_argv", None) or [])
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]

    return watch_command(
        mem.store,
        cmd,
        suggest_only=bool(getattr(args, "suggest_only", False)),
        json_output=bool(getattr(args, "watch_json", False)),
    )


def _cmd_activate(args: argparse.Namespace, mem: LoreMemory) -> int:
    from .watch import activate

    text = args.error_text
    if text == "-":
        text = sys.stdin.read()
    result = activate(mem.store, text, top_k=args.top_k)
    if getattr(args, "activate_json", False):
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result["human_output"] or "No recipes found.")
    return 0 if result["suggestions"] else 3


def _cmd_darwin(args: argparse.Namespace, mem: LoreMemory) -> int:
    from .darwin_replay import classify, darwin_stats, export_sanitized, record_outcome

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

    if sub_cmd == "report":
        pat_row = mem.store.conn.execute(
            "SELECT metadata FROM darwin_patterns WHERE id = ?", (args.pattern_id,)
        ).fetchone()
        if pat_row is None:
            print(f"Error: unknown pattern_id: {args.pattern_id}", file=sys.stderr)
            return 1
        try:
            pat_meta = json.loads(pat_row[0]) if pat_row[0] else {}
            fp_hash = pat_meta.get("fingerprint_hash")
        except (json.JSONDecodeError, TypeError):
            fp_hash = None
        if not fp_hash:
            print(f"Error: pattern {args.pattern_id} has no fingerprint_hash in metadata", file=sys.stderr)
            return 1
        result = record_outcome(mem.store, fp_hash, args.outcome)
        if not result.get("success"):
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            return 1
        eff = result.get("efficacy")
        eff_str = f"{eff:.0%}" if eff is not None else "unrated"
        print(f"Recorded {args.outcome} for pattern {args.pattern_id}")
        print(f"  fingerprint : {fp_hash}")
        print(f"  successes   : {result['total_success']}")
        print(f"  failures    : {result['total_failure']}")
        print(f"  efficacy    : {eff_str}")
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


def _cmd_pack(args: argparse.Namespace) -> int:
    import sqlite3
    import importlib.util
    from pathlib import Path

    scripts_dir = Path(__file__).parent.parent / "scripts"

    def _load_script(name: str):
        path = scripts_dir / f"{name}.py"
        if not path.exists():
            print(f"Error: {path} not found. Is fixcache installed correctly?", file=sys.stderr)
            sys.exit(1)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    cfg = LoreConfig(config_path=getattr(args, "config", None))
    db_path = getattr(args, "db", None) or cfg.db_path

    sub = args.pack_command

    if sub == "export":
        mod = _load_script("pack_export")
        conn = sqlite3.connect(str(Path(db_path).expanduser().resolve()))
        conn.row_factory = sqlite3.Row
        try:
            recipes = mod.load_recipes(
                conn,
                ecosystem_filter=getattr(args, "ecosystem", None),
                min_seen=getattr(args, "min_seen", 1),
            )
        finally:
            conn.close()

        if not recipes:
            print("No recipes found. Run 'fixcache fix <error> --steps ...' first.", file=sys.stderr)
            return 1

        toml_out = mod.build_pack_toml(
            recipes=recipes,
            name=args.name,
            version=args.version,
            description=args.description,
            author=args.author,
            license_=args.license_,
        )
        out_path = getattr(args, "out", None)
        if out_path:
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(toml_out, encoding="utf-8")
            print(f"Written: {p}  ({len(recipes)} recipes)")
        else:
            print(toml_out)
        return 0

    if sub in ("import", "install"):
        mod = _load_script("pack_import")
        source = args.source

        if source.endswith(".toml") or Path(source).exists():
            pack_path = Path(source).expanduser().resolve()
            if not pack_path.exists():
                print(f"Error: file not found: {pack_path}", file=sys.stderr)
                return 1
        else:
            try:
                pack_path = mod.resolve_pack_from_registry(source, version=getattr(args, "pin", None))
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1

        resolved_db = str(Path(db_path).expanduser().resolve())
        try:
            result = mod.import_pack(
                db_path=resolved_db,
                pack_path=pack_path,
                policy=getattr(args, "policy", "merge"),
                dry_run=bool(getattr(args, "dry_run", False)),
                pin_version=getattr(args, "pin", None),
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        status = "dry-run" if getattr(args, "dry_run", False) else "done"
        print(f"Import {status}: {result.summary()}")
        if result.errors:
            for err in result.errors[:5]:
                print(f"  error: {err}", file=sys.stderr)
        return 0 if not result.errors else 2

    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "hook":
        return _cmd_hook(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "pack":
        return _cmd_pack(args)

    with _open_mem(args) as mem:
        cmd = args.command
        if cmd == "watch":
            return _cmd_watch(args, mem)
        if cmd == "fix":
            return _cmd_fix(args, mem)
        if cmd == "darwin":
            return _cmd_darwin(args, mem)
        if cmd == "stats":
            return _cmd_stats(mem)
        if cmd == "activate":
            return _cmd_activate(args, mem)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
