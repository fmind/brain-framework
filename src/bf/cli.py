"""Command-line interface for Brain Framework (`bf`)."""

from __future__ import annotations

import argparse
import os
import signal
import stat
import sys
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

from bf import __version__, models
from bf.models import MAX_BATCH, Error, InputError, failure, printable, redact_secrets, terminal
from bf.storage import expand

_PARSER_EXTRA: dict[str, Any] = {"suggest_on_error": True} if sys.version_info >= (3, 14) else {}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        missing_sub = (
            "the following arguments are required: COMMAND" in message
            or "the following arguments are required: ACTION" in message
        )
        hint = f"{message}; run {self.prog} --help" if missing_sub else message
        raise InputError(hint)

    def _print_message(self, message: str, file: Any = None) -> None:
        super()._print_message(message, file)
        if message and (target := file or sys.stderr) is not None:
            target.flush()


def _add_brain_opt(parser: argparse.ArgumentParser, dest: str = "brain") -> None:
    parser.add_argument(
        "--brain", dest=dest, action="append", default=None, metavar="PATH", help="Brain directory path."
    )


def _build_parser() -> _Parser:
    p_epilog = (
        "Brain: --brain > BF_BRAIN > enclosing bf.yaml > ~/brain. "
        "Exit codes: 0=ok, 1=error, 2=invalid input, 130=SIGINT, 143=SIGTERM."
    )
    p = _Parser(prog="bf", description="Brain Framework CLI.", epilog=p_epilog, allow_abbrev=False, **_PARSER_EXTRA)
    p.add_argument("--version", action="version", version=__version__)
    _add_brain_opt(p, dest="root_brain")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    sub.required = True

    def cmd(name: str, help_text: str, *, with_brain: bool = True, epilog: str = "") -> _Parser:
        sp = sub.add_parser(
            name,
            help=help_text,
            description=help_text,
            epilog=epilog or None,
            allow_abbrev=False,
            **_PARSER_EXTRA,
        )
        if with_brain:
            _add_brain_opt(sp)
        return sp

    sp = cmd("init", "Initialize a new brain directory.", with_brain=False, epilog="Example: bf init ~/brain")
    sp.add_argument("path", help="Target directory (new or empty).")
    sp.add_argument("--name", default=None, help="Brain identifier (lowercase letters, digits, hyphens).")
    sp.add_argument("--description", default="", help="Short brain description written to bf.yaml.")

    sp = cmd(
        "build",
        "Incrementally compile notes to memories/_notes.jsonl and sync .bf/cache/index.sqlite3.",
        epilog="Example: bf build --dry-run",
    )
    sp.add_argument("--dry-run", action="store_true", help="Compile and validate notes without writing files.")

    ing_h = "Validate and save JSON/JSONL records into memories/<source>.jsonl and sync .bf/cache/index.sqlite3."
    sp = cmd("ingest", ing_h, epilog="Example: sensors/github.py | bf ingest github-issues --mode upsert")
    a = sp.add_argument
    a("source", help="Source name (memories/<source>.jsonl).")
    a("--input", default="-", metavar="FILE", help="Regular JSON/JSONL file path or '-' for stdin (default: -).")
    a("--mode", choices=("upsert", "snapshot"), default="upsert", help="Write mode (default: upsert).")
    a("--schema", default=None, metavar="NAME", help="Optional schemas/<NAME>.json override (for partitioned sources).")
    a("--allow-removal", action="store_true", help="Allow --mode snapshot to empty/drop >50%% of 10+ records.")
    a("--dry-run", action="store_true", help="Validate and preview counts without writing files.")

    read_h = "Read a note, #anchor section, source:id record, alias, or declared bf:// URI (read-only)."
    sp = cmd("read", read_h, epilog="Example: bf read concepts/welcome.md#conventions --format raw")
    a = sp.add_argument
    a("target", help="Note path, note#anchor, source:id, alias, or bf:// URI.")
    a("--format", choices=("json", "raw"), default="json", help="Output format: json (default) or raw UTF-8 text.")
    a("--max-bytes", type=int, default=None, metavar="N", help="Maximum UTF-8 response size in bytes.")

    def add_lim(sp: _Parser, dest: str, default_n: int, what: str) -> None:
        grp = sp.add_mutually_exclusive_group()
        grp.add_argument("-n", "--limit", type=int, default=None, metavar="N", help=f"Max {what} ({default_n}).")
        grp.add_argument("--all", dest=dest, action="store_true", help=f"Return all {what}.")

    q_h = "Search and filter notes and memories via disposable SQLite FTS5 BM25 cache (read-only)."
    sp = cmd("query", q_h, epilog="Example: bf query 'retention policy' --kind note --format text")
    a = sp.add_argument
    a("query", nargs="?", default=None, help="Free-text BM25 query (multi-term, accent-folded; optional with filters).")
    a("--kind", choices=("note", "memory"), default="", help="Filter by record kind.")
    a("-s", "--scope", default=None, metavar="SCOPE", help="Restrict to a note folder/path or memory source.")
    a("--type", dest="rtype", default=None, metavar="TYPE", help="Filter by exact record type.")
    a("--status", default=None, metavar="STATUS", help="Filter by exact record status.")
    a("--tag", default=None, metavar="TAG", help="Filter by exact tag.")
    a("--since", default=None, metavar="DATE", help="Filter records updated/created on or after ISO date/timestamp.")
    a("--links-to", default=None, metavar="TARGET", help="Filter records linking to a note path, source:id, or URL.")
    a("--exclude", action="append", default=[], metavar="SOURCE", help="Exclude a noisy source (repeatable).")
    a(
        "--all-sources",
        action="store_true",
        help="Include sources excluded by bf.yaml search.exclude.",
    )
    add_lim(sp, "all_items", 20, "results")
    a("--format", choices=("json", "jsonl", "text"), default="json", help="Output format (default: json).")

    sp = cmd(
        "review",
        "Review stale notes, newer linked evidence, and open tasks (read-only).",
        epilog="Example: bf review --format text",
    )
    add_lim(sp, "all_items", 20, "stale items")
    sp.add_argument(
        "--format", choices=("json", "jsonl", "text"), default="json", help="Output format (default: json)."
    )

    sp = cmd(
        "check",
        "Validate bf.yaml, schemas, notes, JSONL memories, loops, and links (read-only).",
        epilog="Example: bf check --notes --format text",
    )
    add_lim(sp, "all_problems", 50, "problems")
    a = sp.add_argument
    a("--notes", action="store_true", help="Validate config, schemas, loops, notes, and note links only.")
    a(
        "--strict",
        action="store_true",
        help="Fail validation on warnings (snapshot/forgotten targets, brains, or scale).",
    )
    a("--format", choices=("json", "text"), default="json", help="Output format (default: json).")

    sp = cmd("schema", "Print bundled Draft 2020-12 JSON Schemas.", with_brain=False, epilog="Example: bf schema bf")
    sp.add_argument("name", nargs="?", default=None, help="Schema name: bf or record.")

    run_h = "Execute named/glob, --all, or --due cycle loops."
    sp = cmd("run", run_h, epilog="Pass extra args to one loop with `--`: bf run my-loop -- --verbose")
    sp.add_argument("loops", nargs="*", help="Enabled cycle loop names or glob patterns to run.")
    rgrp = sp.add_mutually_exclusive_group()
    rgrp.add_argument("--due", action="store_true", help="Run scheduled and event-triggered loops due now.")
    rgrp.add_argument("--all", dest="all_loops", action="store_true", help="Run all enabled cycle loops.")
    sp.add_argument("--dry-run", action="store_true", help="Validate and preview actions without running.")

    sp = cmd(
        "pause",
        "Pause named/glob loops or --all indefinitely (or --for DURATION) without editing bf.yaml.",
        epilog="Example: bf pause dev --for 2h",
    )
    sp.add_argument("loops", nargs="*", help="Loop names or glob patterns to pause.")
    sp.add_argument("--all", dest="all_loops", action="store_true", help="Pause all loops in bf.yaml.")
    sp.add_argument("--for", dest="pause_for", default=None, metavar="DURATION", help="Optional auto-resume duration.")
    sp.add_argument("--dry-run", action="store_true", help="Preview paused loops without writing.")

    sp = cmd(
        "resume",
        "Resume paused named/glob loops or --all.",
        epilog="Example: bf resume dev",
    )
    sp.add_argument("loops", nargs="*", help="Loop names or glob patterns to resume.")
    sp.add_argument("--all", dest="all_loops", action="store_true", help="Resume all loops in bf.yaml.")
    sp.add_argument("--dry-run", action="store_true", help="Preview resumed loops without writing.")

    st_sp = cmd(
        "state",
        "Reset loop state cursors or prune orphaned .bf/state/, .bf/locks/, and logs/ artifacts.",
        with_brain=False,
        epilog="Example: bf state reset --all --dry-run",
    )
    _add_brain_opt(st_sp, dest="state_brain")
    st_sub = st_sp.add_subparsers(dest="state_action", metavar="ACTION", parser_class=_Parser)
    st_sub.required = True
    st_reset = st_sub.add_parser(
        "reset",
        help="Reset .bf/state/<loop>.json state to idle at current event_seq.",
        description="Reset .bf/state/<loop>.json state to idle anchored at current event_seq.",
        epilog="Example: bf state reset my-loop --dry-run",
        allow_abbrev=False,
        **_PARSER_EXTRA,
    )
    _add_brain_opt(st_reset)
    st_reset.add_argument("loops", nargs="*", help="Loop names or glob patterns to reset.")
    st_reset.add_argument("--all", dest="all_loops", action="store_true", help="Reset all loops in bf.yaml.")
    st_reset.add_argument("--dry-run", action="store_true", help="Preview resets without writing.")
    st_prune = st_sub.add_parser(
        "prune",
        help="Delete orphaned .bf/state/, unlocked .bf/locks/, and logs/ artifacts.",
        description="Delete orphaned .bf/state/, unlocked .bf/locks/, and logs/ artifacts for removed loops.",
        epilog="Example: bf state prune --dry-run",
        allow_abbrev=False,
        **_PARSER_EXTRA,
    )
    _add_brain_opt(st_prune)
    st_prune.add_argument("--dry-run", action="store_true", help="Preview orphans without deleting.")

    emit_e = 'Example: bf emit loop.wake --in 15m --data \'{"id":"ci-1"}\' --dedupe'
    sp = cmd("emit", "Append a custom event to .bf/state/events.jsonl (or schedule via --in/--at).", epilog=emit_e)
    a = sp.add_argument
    a("kind", help="Event kind identifier (e.g. task.ready).")
    a("--source", default="bf", help="Event source name (default: bf).")
    a("--data", default=None, metavar="JSON", help="Optional JSON object payload, or '-' to read stdin.")
    dgrp = sp.add_mutually_exclusive_group()
    dgrp.add_argument("--in", dest="delay_in", default=None, metavar="DURATION", help="Delay delivery (e.g. 15m, 2h).")
    dgrp.add_argument("--at", dest="deliver_at", default=None, metavar="ISO", help="Deliver at ISO timestamp with TZ.")
    a("--dedupe", action="store_true", help="Deduplicate on kind, source, and string data.id.")
    a("--dry-run", action="store_true", help="Validate event without appending to .bf/state/.")

    sp = cmd(
        "events",
        "Inspect recent events in .bf/state/events.jsonl or pending delayed --timers (read-only).",
        epilog="Example: bf events --kind loop.message --format text",
    )
    a = sp.add_argument
    egrp = sp.add_mutually_exclusive_group()
    egrp.add_argument("--since", type=int, default=None, metavar="SEQ", help="Return events with seq > SEQ (forward).")
    egrp.add_argument("--timers", action="store_true", help="List pending delayed events in .bf/state/timers.jsonl.")
    a("--kind", default=None, metavar="KIND", help="Filter by exact event kind.")
    a("--source", default=None, metavar="NAME", help="Filter by exact event source.")
    add_lim(sp, "all_items", 20, "events")
    a("--format", choices=("json", "jsonl", "text"), default="json", help="Output format (default: json).")

    sp = cmd(
        "status",
        "Inspect brain health, counts, and attention loops (read-only; use --check to exit 1 on attention).",
        epilog="Example: watch -n 2 bf status --format text",
    )
    a = sp.add_argument
    a("--loops", action="store_true", help="Include full per-loop status map in JSON output.")
    a("--check", action="store_true", help="Exit 1 when brain health reports attention or config problems.")
    a(
        "--format",
        choices=("json", "text"),
        default=None,
        help="Output format (default: json; text for overview table).",
    )

    sp = cmd(
        "watch",
        "Headless supervisor for daemon loops, note rebuilds, and concurrent cycle loops.",
        epilog="Example: bf watch --format jsonl",
    )
    w_h = "Output mode: auto (text on non-dumb TTY, jsonl on pipe/TERM=dumb), text (scrolling log), or jsonl."
    sp.add_argument("--format", choices=("auto", "text", "jsonl"), default="auto", help=w_h)
    return p


def _split_run(items: list[str]) -> tuple[list[str], list[str], bool]:
    if "--" in items:
        sep, idx = items.index("--"), 0
        while idx < sep:
            if (tok := items[idx]) == "--brain" or tok.startswith("-"):
                idx += 2 if tok == "--brain" else 1
                continue
            return (items[:sep], items[sep + 1 :], True) if tok == "run" else (items, [], False)
    return items, [], False


def _check_piped_stdin(hint: str) -> None:
    if sys.stdin is None or getattr(sys.stdin, "closed", False):
        raise InputError(f"stdin is closed; {hint}")
    try:
        if sys.stdin.isatty():
            raise InputError(f"stdin is a terminal; {hint}")
    except ValueError as err:
        raise InputError(f"stdin is closed; {hint}") from err
    if getattr(sys.stdin, "buffer", None) is None:
        raise InputError(f"stdin is closed; {hint}")


def _read_in(spec: str) -> bytes:
    try:
        if spec == "-":
            if sys.stdin is None or getattr(sys.stdin, "closed", False) or getattr(sys.stdin, "buffer", None) is None:
                raise InputError("stdin is closed; pipe input or pass a file path")
            data = sys.stdin.buffer.read(MAX_BATCH + 1)
        else:
            path = expand(Path(spec))
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise InputError("--input must be a regular file (use '-' for pipes/stdin)")
                data = fh.read(MAX_BATCH + 1)
    except InputError:
        raise
    except (OSError, ValueError) as err:
        detail = getattr(err, "strerror", None)
        raise Error(f"cannot read input file{f' ({detail})' if detail else ''}") from err
    if len(data) > MAX_BATCH:
        raise Error(f"input exceeds {MAX_BATCH} bytes; split the batch")
    return data


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM", "") not in {"", "dumb"} and not os.environ.get("NO_COLOR", "")


def _write_text(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def _emit(payload: object) -> None:
    sys.stdout.buffer.write(terminal(payload))
    sys.stdout.flush()


def _emit_fmt(res: dict[str, Any], fmt: str, *, review: bool = False) -> None:
    if fmt == "json":
        return _emit(res)
    rows = res["stale"] if review else res["items"]
    for row in rows:
        sys.stdout.buffer.write(terminal(row))
    sys.stdout.flush()
    if res.get("truncated"):
        sys.stderr.write(f"bf: warning: truncated to {len(rows)} items; pass -n/--limit or --all for more\n")
    return None


def _resolve_brain_arg(args: argparse.Namespace) -> str:
    cmd = args.command
    root_vals = getattr(args, "root_brain", None) or []
    state_vals = getattr(args, "state_brain", None) or []
    sub_vals = getattr(args, "brain", None) or []
    if cmd in {"init", "schema"} and root_vals:
        raise InputError(f"bf {cmd} does not accept --brain")
    vals = [*root_vals, *state_vals, *sub_vals]
    if any(not v.strip() for v in vals):
        raise InputError("--brain must be nonempty")
    normed = {expand(Path(v)).resolve() for v in vals}
    if len(normed) > 1:
        raise InputError("conflicting --brain values")
    return vals[0] if vals else ""


def _dispatch(args: argparse.Namespace, extra: list[str], has_passthrough: bool) -> int:
    cmd, brain_arg = args.command, _resolve_brain_arg(args)
    if cmd == "schema":
        from bf import schemas

        _emit(schemas.get_schema(args.name) if args.name is not None else schemas.export_schemas())
        return 0
    if cmd == "init":
        from bf import engine

        if not args.path.strip():
            raise InputError("path must be nonempty")
        if args.name is not None and not args.name.strip():
            raise InputError("--name must be nonempty")
        _emit(engine.init_brain(Path(args.path), name=args.name or "", description=args.description))
        return 0

    from bf import config

    if cmd == "watch":
        from bf import watch

        return int(watch.serve(config.select(brain_arg), fmt=args.format).get("exit_code", 0))
    if cmd == "status":
        from bf import status

        eff_fmt = args.format or "json"
        res = status.status_summary(config.select(brain_arg), include_loops=args.loops or eff_fmt == "text")
        if eff_fmt == "text":
            from bf import render

            _write_text(render.format_status_text(res, color=_use_color()))
        else:
            _emit(res)
        return (0 if res.get("ok", True) else 1) if args.check else 0
    if cmd == "pause":
        from bf import loops

        if bool(args.loops) == bool(args.all_loops):
            raise InputError("specify loop names/patterns or --all (mutually exclusive)")
        dur_s: int | None = None
        if args.pause_for is not None:
            try:
                dur_s = models.parse_duration(args.pause_for, "--for", maximum=31_557_600)
            except Error as err:
                raise InputError(str(err)) from err
        store = config.select(brain_arg)
        _emit(loops.pause_loops(store, args.loops, all_loops=args.all_loops, duration_s=dur_s, dry_run=args.dry_run))
        return 0
    if cmd == "resume":
        from bf import loops

        if bool(args.loops) == bool(args.all_loops):
            raise InputError("specify loop names/patterns or --all (mutually exclusive)")
        store = config.select(brain_arg)
        _emit(loops.resume_loops(store, args.loops, all_loops=args.all_loops, dry_run=args.dry_run))
        return 0
    if cmd == "events":
        from bf import state

        for flag, val in (("--kind", args.kind), ("--source", args.source)):
            if val is not None and not val.strip():
                raise InputError(f"{flag} must be nonempty")
        lim = 20 if args.limit is None else args.limit
        state.validate_list_events_args(
            since=args.since, kind=args.kind or "", source=args.source or "", limit=lim, all_items=args.all_items
        )
        store = config.select(brain_arg)
        res = state.list_events(
            store,
            config.load(store).name,
            since=args.since,
            kind=args.kind or "",
            source=args.source or "",
            timers_only=args.timers,
            limit=lim,
            all_items=args.all_items,
        )
        if args.format == "text":
            from bf import render

            _write_text(render.format_events_text(res, color=_use_color()))
        else:
            _emit_fmt(res, args.format, review=False)
        return 0
    if cmd == "state":
        from bf import loops

        if args.state_action == "prune":
            _emit(loops.prune_state(config.select(brain_arg), dry_run=args.dry_run))
            return 0
        if bool(args.loops) == bool(args.all_loops):
            raise InputError("specify loop names/patterns or --all (mutually exclusive)")
        _emit(loops.reset_loops(config.select(brain_arg), args.loops, all_loops=args.all_loops, dry_run=args.dry_run))
        return 0
    if cmd == "emit":
        from datetime import UTC, datetime, timedelta

        from bf import state

        if not models.EVENT_KIND_RE.fullmatch(args.kind):
            raise InputError("event kind must match ^[a-z][a-z0-9._-]{0,127}$")
        if not models.NAME_RE.fullmatch(args.source):
            raise InputError("event source must use lowercase letters, digits, and hyphens")
        delay_s: int | None = None
        target_dt: datetime | None = None
        if args.delay_in is not None or args.deliver_at is not None:
            try:
                if args.delay_in is not None:
                    delay_s = models.parse_duration(args.delay_in, "--in", maximum=31_557_600)
                else:
                    target_dt = datetime.fromisoformat(models.normalize_time(args.deliver_at, "--at"))
            except Error as err:
                raise InputError(str(err)) from err
        raw_data: object = {}
        if args.data is not None and args.data != "-":
            if not args.data.strip():
                raise InputError("--data must be a JSON object or '-'")
            if len(args.data.encode("utf-8")) > state.MAX_EVENT_LINE:
                raise Error(f"--data exceeds {state.MAX_EVENT_LINE} bytes")
            try:
                raw_data = models.decode(args.data, "--data")
            except Error as err:
                raise InputError(str(err)) from err
            if not isinstance(raw_data, dict):
                raise InputError("--data must be a JSON object")
        if (
            args.data != "-"
            and args.dedupe
            and (
                not isinstance(raw_data, dict)
                or not isinstance(raw_data.get("id"), str)
                or not str(raw_data["id"]).strip()
            )
        ):
            raise InputError("--dedupe requires a non-empty string 'id' in --data")
        store = config.select(brain_arg)
        brain_name = config.load(store).name
        if args.data == "-":
            _check_piped_stdin("pipe JSON object or pass --data JSON")
            raw_in = sys.stdin.buffer.read(state.MAX_EVENT_LINE + 1)
            if len(raw_in) > state.MAX_EVENT_LINE:
                raise Error(f"--data exceeds {state.MAX_EVENT_LINE} bytes")
            if not raw_in.strip():
                raise InputError("--data - received empty stdin")
            try:
                raw_data = models.decode(raw_in, "--data")
            except Error as err:
                raise InputError(str(err)) from err
            if not isinstance(raw_data, dict):
                raise InputError("--data must be a JSON object")
            if args.dedupe and (not isinstance(raw_data.get("id"), str) or not str(raw_data["id"]).strip()):
                raise InputError("--dedupe requires a non-empty string 'id' in --data")
        assert isinstance(raw_data, dict)  # noqa: S101
        if delay_s is not None or target_dt is not None:
            now_dt = datetime.now(UTC)
            eff_target = now_dt + timedelta(seconds=delay_s) if delay_s is not None else target_dt
            assert eff_target is not None  # noqa: S101
            tmr, deduped = state.schedule_event(
                store,
                args.kind,
                args.source,
                raw_data,
                deliver_at=eff_target,
                dedupe=args.dedupe,
                dry_run=args.dry_run,
                now=now_dt,
            )
            _emit(
                {
                    "ok": True,
                    "brain": brain_name,
                    "scheduled": True,
                    "timer": tmr,
                    "deduped": deduped,
                    "dry_run": args.dry_run,
                }
            )
            return 0
        ev, deduped = state.append_event(
            store, args.kind, args.source, raw_data, dedupe=args.dedupe, dry_run=args.dry_run
        )
        _emit({"ok": True, "brain": brain_name, "event": ev, "deduped": deduped, "dry_run": args.dry_run})
        return 0
    if cmd == "check":
        from bf import engine

        lim = 50 if args.limit is None else args.limit
        if not args.all_problems and (lim < 1 or lim > 5000):
            raise InputError("limit must be between 1 and 5000")
        res = engine.validate(
            config.select(brain_arg),
            limit=lim,
            all_problems=args.all_problems,
            strict=args.strict,
            notes_only=args.notes,
        )
        if args.format == "text":
            from bf import render

            _write_text(render.format_check_text(res, color=_use_color()))
        else:
            _emit(res)
        return 0 if res["ok"] else 1
    if cmd == "run":
        from bf import loops

        if bool(args.loops) + bool(args.due) + bool(args.all_loops) != 1:
            raise InputError("specify loop names/patterns, --due, or --all (mutually exclusive)")
        if (extra or has_passthrough) and (args.due or args.all_loops):
            raise InputError("passthrough -- arguments require a single loop")
        store = config.select(brain_arg)

        class _SigFlag:
            __slots__ = ("_val",)

            def __init__(self) -> None:
                self._val = False

            def is_set(self) -> bool:
                return self._val

            def set(self) -> None:
                self._val = True

        stop_ev = _SigFlag()
        term_ev = _SigFlag()
        on_main = threading.current_thread() is threading.main_thread()

        def _on_term(*_a: object) -> None:
            term_ev.set()
            stop_ev.set()

        prev_term = signal.signal(signal.SIGTERM, _on_term) if on_main else None
        try:
            res = loops.run_loops(
                store,
                args.loops,
                due_only=args.due,
                all_loops=args.all_loops,
                extra_args=extra,
                has_passthrough=has_passthrough,
                dry_run=args.dry_run,
                stop_event=stop_ev,
                term_event=term_ev,
            )
        finally:
            if on_main and prev_term is not None:
                with suppress(OSError, ValueError):
                    signal.signal(signal.SIGTERM, prev_term)
        _emit(res)
        return 143 if term_ev.is_set() else (0 if res["ok"] else 1)
    if cmd == "ingest":
        from bf import records

        if not args.input.strip():
            raise InputError("--input must be nonempty")
        if args.schema is not None and not args.schema.strip():
            raise InputError("--schema must be nonempty")
        records.validate_ingest_args(
            args.source, mode=args.mode, schema_name=args.schema or "", allow_removal=args.allow_removal
        )
        store = config.select(brain_arg)
        if args.input == "-":
            _check_piped_stdin("pipe JSON records or pass --input FILE")
        res = records.ingest(
            store,
            args.source,
            _read_in(args.input),
            mode=args.mode,
            schema_name=args.schema or "",
            allow_removal=args.allow_removal,
            dry_run=args.dry_run,
        )
        _emit(res)
        if warn := res.get("index_warning"):
            sys.stderr.write(f"bf: warning: {warn}\n")
        return 0
    if cmd == "read":
        from bf import query

        query.validate_read_args(args.target, fmt=args.format, max_bytes=args.max_bytes)
        res_r = query.read_target(
            config.select(brain_arg),
            args.target,
            fmt=args.format,
            max_bytes=args.max_bytes,
            warn_raw_trunc=args.format == "raw",
        )
        if isinstance(res_r, str):
            _write_text(res_r)
        else:
            _emit(res_r)
        return 0
    if cmd == "query":
        from bf import query

        for flag, val in (
            ("--scope", args.scope),
            ("--type", args.rtype),
            ("--status", args.status),
            ("--tag", args.tag),
            ("--since", args.since),
            ("--links-to", args.links_to),
        ):
            if val is not None and not val.strip():
                raise InputError(f"{flag} must be nonempty")
        if any(not e.strip() for e in args.exclude):
            raise InputError("--exclude must be nonempty")
        lim = 20 if args.limit is None else args.limit
        query.validate_query_args(
            args.query,
            kind=args.kind,
            scope=args.scope or "",
            rtype=args.rtype or "",
            status=args.status or "",
            tag=args.tag or "",
            since=args.since or "",
            links_to=args.links_to or "",
            exclude=args.exclude,
            limit=lim,
            all_items=args.all_items,
        )
        res = query.query(
            config.select(brain_arg),
            args.query,
            kind=args.kind,
            scope=args.scope or "",
            rtype=args.rtype or "",
            status=args.status or "",
            tag=args.tag or "",
            since=args.since or "",
            links_to=args.links_to or "",
            exclude=args.exclude,
            all_sources=args.all_sources,
            limit=lim,
            all_items=args.all_items,
        )
        if args.format == "text":
            from bf import render

            _write_text(render.format_query_text(res, color=_use_color()))
        else:
            _emit_fmt(res, args.format, review=False)
            if args.format == "jsonl":
                if excl := res.get("excluded_sources"):
                    sys.stderr.write(f"bf: hint: excluded by bf.yaml: {', '.join(excl)}; pass --all-sources\n")
                if unbuilt := res.get("unbuilt_notes"):
                    sys.stderr.write(f"bf: hint: unbuilt notes in {', '.join(unbuilt)}; run bf build\n")
                if res.get("index_stale"):
                    sys.stderr.write("bf: hint: search index stale; run bf build\n")
        return 0
    if cmd == "build":
        from bf import engine

        res = engine.build(config.select(brain_arg), dry_run=args.dry_run)
        _emit(res)
        if warn := res.get("index_warning"):
            sys.stderr.write(f"bf: warning: {warn}\n")
        return 0 if res["ok"] else 1

    from bf import engine

    lim_rev = 20 if args.limit is None else args.limit
    if not args.all_items and (lim_rev < 1 or lim_rev > 5000):
        raise InputError("limit must be between 1 and 5000")
    res = engine.review(config.select(brain_arg), limit=lim_rev, all_items=args.all_items)
    if args.format == "text":
        from bf import render

        _write_text(render.format_review_text(res, color=_use_color()))
    else:
        _emit_fmt(res, args.format, review=True)
        if args.format == "jsonl":
            if unbuilt := res.get("unbuilt_notes"):
                sys.stderr.write(f"bf: hint: unbuilt notes in {', '.join(unbuilt)}; run bf build\n")
            if res.get("index_stale"):
                sys.stderr.write("bf: hint: search index stale; run bf build\n")
            if not res["ok"] and res.get("problems"):
                sys.stderr.write(f"bf: {redact_secrets(printable('; '.join(str(p) for p in res['problems'])))}\n")
    return 0 if res["ok"] else 1


def run(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI invocation and return its POSIX exit code (`0`, `1`, `2`, `130`, `143`)."""
    try:
        head, extra, has_passthrough = _split_run(list(sys.argv[1:] if argv is None else argv))
        args = _build_parser().parse_args(head)
        return _dispatch(args, extra, has_passthrough)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        models.silence_broken_pipe()
        return 0
    except InputError as err:
        sys.stderr.write(f"bf: invalid input: {redact_secrets(printable(str(err)))}\n")
        return 2
    except (Error, OSError, UnicodeError) as err:
        sys.stderr.write(f"bf: {redact_secrets(printable(failure(err)))}\n")
        return 1


def main() -> None:
    raise SystemExit(run())
