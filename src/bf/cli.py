"""Command-line interface for Brain Framework (`bf`)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

from bf import __version__
from bf.models import MAX_BATCH, Error, InputError, failure, printable, terminal
from bf.storage import expand


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InputError(message)


def _add_brain_opt(parser: argparse.ArgumentParser, dest: str = "brain") -> None:
    parser.add_argument(
        "--brain", dest=dest, action="append", default=None, metavar="PATH", help="Brain directory path."
    )


def _build_parser() -> _Parser:
    p_epilog = (
        "Brain: --brain > enclosing bf.yaml > BF_BRAIN > BRAIN_LOCATION > ~/brain. "
        "Exit codes: 0=ok, 1=error, 2=invalid input, 130=interrupted."
    )
    p = _Parser(
        prog="bf", description="Brain Framework CLI.", epilog=p_epilog, allow_abbrev=False, suggest_on_error=True
    )
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
            suggest_on_error=True,
        )
        if with_brain:
            _add_brain_opt(sp)
        return sp

    sp = cmd("init", "Initialize a new brain directory.", with_brain=False, epilog="Example: bf init ~/brain")
    sp.add_argument("path", type=Path, help="Target directory (new or empty).")
    sp.add_argument("--name", default=None, help="Brain identifier (lowercase letters, digits, hyphens).")
    sp.add_argument("--description", default="", help="Short brain description written to bf.yaml.")

    cmd("build", "Compile Markdown notes into memories/<dir>.jsonl.")

    sp = cmd(
        "ingest",
        "Validate and save JSON/JSONL records into memories/<source>.jsonl or usages/<source>.jsonl.",
        epilog="Example: sensors/github.sh | bf ingest github-issues --mode upsert",
    )
    sp.add_argument("source", help="Source name (requires schemas/<source>.json or schemas/usages.json).")
    sp.add_argument(
        "--to", dest="target", choices=("memories", "usages"), default="memories", help="Target record dir."
    )
    sp.add_argument("--input", default="-", metavar="FILE", help="Input JSON/JSONL file path or '-' for stdin.")
    sp.add_argument("--mode", choices=("upsert", "snapshot"), default="upsert", help="Write mode (default: upsert).")
    sp.add_argument("--allow-removal", action="store_true", help="Allow snapshot to empty/drop >50%% of 10+ records.")
    sp.add_argument("--dry-run", action="store_true", help="Validate and preview counts without writing files.")

    sp = cmd(
        "read",
        "Read a note, #anchor section, source:id record, alias, or bf:// URI (read-only).",
        epilog="Example: bf read concepts/welcome.md#conventions",
    )
    sp.add_argument("target", help="Note path, note#anchor, source:id, alias, or bf:// URI.")
    sp.add_argument("--raw", action="store_true", help="Return raw Markdown or file text wrapped in JSON .text.")
    sp.add_argument("--max-bytes", type=int, default=None, metavar="N", help="Maximum UTF-8 JSON response size.")

    sp = cmd(
        "query",
        "Search and filter memories/*.jsonl (or usages/*.jsonl) via rg and jq (read-only).",
        epilog="Example: bf query 'retention' --kind note --format jsonl",
    )
    sp.add_argument("pattern", nargs="?", default=None, help="ripgrep regex pattern (optional with --jq or filters).")
    sp.add_argument("--jq", default=None, metavar="EXPR", help="Isolated jq filter expression.")
    sp.add_argument("--kind", choices=("note", "memory", "usage"), default="", help="Filter by record kind.")
    sp.add_argument(
        "-s", "--scope", default="", metavar="PATH", help="Restrict to a note, source, memories/, or usages/."
    )
    sp.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="Max results (1..5000, default: 20).")
    sp.add_argument("--format", choices=("json", "jsonl"), default="json", help="Output format (default: json).")

    sp = cmd("review", "Review stale notes, newer linked evidence, and open tasks (read-only).")
    rv_lim = sp.add_mutually_exclusive_group()
    rv_lim.add_argument("-n", "--limit", type=int, default=None, metavar="N", help="Max stale items (default: 20).")
    rv_lim.add_argument("--all", dest="all_items", action="store_true", help="Return all stale notes.")
    sp.add_argument("--format", choices=("json", "jsonl"), default="json", help="Output format (default: json).")

    sp = cmd("check", "Validate bf.yaml, schemas, notes, JSONL records, loops, and links (read-only).")
    ck_lim = sp.add_mutually_exclusive_group()
    ck_lim.add_argument("-n", "--limit", type=int, default=None, metavar="N", help="Max problems (default: 50).")
    ck_lim.add_argument(
        "--all", dest="all_problems", action="store_true", help="Return all problems without truncation."
    )
    sp.add_argument(
        "--strict", action="store_true", help="Fail validation on warnings (snapshot or forgotten targets)."
    )

    sp = cmd("schema", "Print bundled Draft 2020-12 JSON Schemas.", with_brain=False)
    sp.add_argument("name", nargs="?", default=None, help="Schema name: bf or record.")

    sp = cmd(
        "run",
        "Execute named/glob, --all, or --due loops, reset loop state, or prune orphaned .bf/ state.",
        epilog="Pass extra args to one loop with `--`: bf run my-loop -- --verbose",
    )
    sp.add_argument("loops", nargs="*", help="Loop names or glob patterns to run sequentially (or reset with --reset).")
    rgrp = sp.add_mutually_exclusive_group()
    rgrp.add_argument("--due", action="store_true", help="Rebuild notes and run scheduled/event-triggered loops.")
    rgrp.add_argument("--all", dest="all_loops", action="store_true", help="Run all enabled cycle loops.")
    rgrp.add_argument("--reset", action="store_true", help="Reset .bf/<loop>.json state for named loops to idle.")
    rgrp.add_argument("--prune", action="store_true", help="Remove orphaned .bf/<loop>.json state files.")
    sp.add_argument(
        "--dry-run", action="store_true", help="Validate and preview actions without executing or deleting."
    )

    sp = cmd(
        "emit",
        "Append a custom event to logs/events.jsonl.",
        epilog='Example: echo \'{"id":"1"}\' | bf emit task.ready --data -',
    )
    sp.add_argument("kind", help="Event kind identifier (e.g. task.ready).")
    sp.add_argument("--source", default="bf", help="Event source name (default: bf).")
    sp.add_argument("--data", default="", metavar="JSON", help="Optional JSON object payload, or '-' to read stdin.")
    sp.add_argument("--dedupe", action="store_true", help="Deduplicate on kind, source, and data.id.")

    sp = cmd("status", "Inspect brain counts, failing loop attention, and loop health (read-only).")
    sp.add_argument("--all", dest="all_units", action="store_true", help="Include full per-loop status map.")
    sp.add_argument(
        "--format", choices=("json", "text"), default="json", help="Output format (json or bird's-eye text)."
    )
    sp.add_argument("--check", action="store_true", help="Exit 1 when any enabled loop requires attention.")

    sp = cmd("watch", "Supervise daemon loops, note rebuilds, and concurrent cycle loops.")
    sp.add_argument(
        "--format",
        choices=("auto", "text", "jsonl"),
        default="auto",
        help="Output mode: auto (text on TTY, jsonl on pipe), text, or jsonl.",
    )
    return p


def _split_run(items: list[str]) -> tuple[list[str], list[str]]:
    if "--" in items:
        sep, idx = items.index("--"), 0
        while idx < sep:
            if (tok := items[idx]) == "--brain" or tok.startswith("-"):
                idx += 2 if tok == "--brain" else 1
                continue
            return (items[:sep], items[sep + 1 :]) if tok == "run" else (items, [])
    return items, []


def _read_in(spec: str) -> bytes:
    try:
        if spec == "-":
            data = sys.stdin.buffer.read(MAX_BATCH + 1)
        else:
            with expand(Path(spec)).open("rb") as fh:
                data = fh.read(MAX_BATCH + 1)
    except OSError as err:
        raise Error(f"cannot read input file{f' ({err.strerror})' if err.strerror else ''}") from err
    if len(data) > MAX_BATCH:
        raise Error(f"input exceeds {MAX_BATCH} bytes; split the batch")
    return data


def _emit(payload: object) -> None:
    with suppress(BrokenPipeError):
        sys.stdout.buffer.write(terminal(payload))
        sys.stdout.flush()


def _emit_fmt(res: dict[str, Any], fmt: str, *, review: bool = False) -> None:
    if fmt == "json":
        return _emit(res)
    rows = res["stale"] if review else res["items"]
    with suppress(BrokenPipeError):
        for row in rows:
            sys.stdout.buffer.write(terminal(row))
        sys.stdout.flush()
    if res.get("truncated"):
        flag = "--all" if review else "-n/--limit"
        sys.stderr.write(f"bf: warning: truncated to {len(rows)} items; pass {flag} for more\n")
    return None


def _resolve_brain_arg(args: argparse.Namespace) -> str:
    cmd = args.command
    root_vals = getattr(args, "root_brain", None) or []
    sub_vals = getattr(args, "brain", None) or []
    if cmd in {"init", "schema"} and root_vals:
        raise InputError(f"bf {cmd} does not accept --brain")
    vals = [*root_vals, *sub_vals]
    if any(not v.strip() for v in vals):
        raise InputError("--brain must be nonempty")
    if len(set(vals)) > 1:
        raise InputError("conflicting --brain values")
    return vals[0] if vals else ""


def _dispatch(args: argparse.Namespace, extra: list[str]) -> int:
    cmd = args.command
    brain_arg = _resolve_brain_arg(args)
    if cmd == "schema":
        from bf import schemas

        _emit(schemas.get_schema(args.name) if args.name is not None else schemas.export_schemas())
        return 0
    if cmd == "init":
        from bf import engine

        if args.name is not None and not args.name.strip():
            raise InputError("--name must be nonempty")
        _emit(engine.init_brain(args.path, name=args.name or "", description=args.description))
        return 0

    from bf import config

    store = config.select(brain_arg)
    if cmd == "watch":
        from bf import watch

        return 0 if watch.serve(store, fmt=args.format)["ok"] else 1
    if cmd == "status":
        from bf import status

        res = status.status_summary(store, all_units=args.all_units or args.format == "text")
        if args.format == "text":
            use_color = (
                sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb" and not os.environ.get("NO_COLOR", "")
            )
            with suppress(BrokenPipeError):
                sys.stdout.buffer.write(status.format_status_text(res, color=use_color).encode("utf-8"))
                sys.stdout.flush()
        else:
            _emit(res)
        return 0 if (not args.check or res.get("ok", True)) else 1
    if cmd == "emit":
        from bf import models, records

        brain_name = config.load(store).name
        if not models.EVENT_KIND_RE.fullmatch(args.kind):
            raise InputError("event kind must match ^[a-z][a-z0-9._-]{0,127}$")
        if not models.NAME_RE.fullmatch(args.source):
            raise InputError("event source must use lowercase letters, digits, and hyphens")
        if args.data == "-":
            if sys.stdin.isatty():
                raise InputError("stdin is a terminal; pipe JSON object or pass --data JSON")
            raw_in = _read_in("-")
            try:
                raw_data = models.decode(raw_in, "--data") if raw_in.strip() else {}
            except Error as err:
                raise InputError(str(err)) from err
        else:
            try:
                raw_data = models.decode(args.data, "--data") if args.data else {}
            except Error as err:
                raise InputError(str(err)) from err
        if not isinstance(raw_data, dict):
            raise InputError("--data must be a JSON object")
        ev = records.append_event(store, args.kind, args.source, raw_data, dedupe=args.dedupe)
        _emit({"ok": True, "brain": brain_name, "event": ev})
        return 0
    if cmd == "check":
        from bf import engine

        res = engine.validate(
            store,
            limit=50 if args.limit is None else args.limit,
            all_problems=args.all_problems,
            strict=args.strict,
        )
        _emit(res)
        return 0 if res["ok"] else 1
    if cmd == "run":
        from bf import loops

        if args.prune:
            if args.loops or extra:
                raise InputError("--prune cannot be combined with loop names or passthrough -- arguments")
            _emit(loops.prune_state(store, dry_run=args.dry_run))
            return 0
        if args.reset:
            if extra:
                raise InputError("--reset cannot be combined with passthrough -- arguments")
            _emit(loops.reset_loops(store, args.loops, dry_run=args.dry_run))
            return 0
        res = loops.run_loops(
            store, args.loops, due_only=args.due, all_loops=args.all_loops, extra_args=extra, dry_run=args.dry_run
        )
        _emit(res)
        return 0 if res["ok"] else 1
    if cmd == "ingest":
        from bf import records

        if args.input == "-" and sys.stdin.isatty():
            raise InputError("stdin is a terminal; pipe JSON records or pass --input FILE")
        _emit(
            records.ingest(
                store,
                args.source,
                _read_in(args.input),
                target=args.target,
                mode=args.mode,
                allow_removal=args.allow_removal,
                dry_run=args.dry_run,
            )
        )
        return 0
    if cmd == "read":
        from bf import query

        _emit(query.read_target(store, args.target, raw=args.raw, max_bytes=args.max_bytes))
        return 0
    if cmd == "query":
        from bf import query

        res = query.query(store, args.pattern, jq_expr=args.jq, scope=args.scope, kind=args.kind, limit=args.limit)
        _emit_fmt(res, args.format, review=False)
        return 0
    if cmd == "build":
        from bf import engine

        res = engine.build(store)
        _emit(res)
        return 0 if res["ok"] else 1

    from bf import engine

    res = engine.review(store, limit=20 if args.limit is None else args.limit, all_items=args.all_items)
    _emit_fmt(res, args.format, review=True)
    if args.format != "json" and not res["ok"] and res.get("problems"):
        sys.stderr.write(f"bf: {printable('; '.join(str(p) for p in res['problems']))}\n")
    return 0 if res["ok"] else 1


def run(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI invocation and return its POSIX exit code (`0`, `1`, `2`, `130`)."""
    try:
        head, extra = _split_run(list(sys.argv[1:] if argv is None else argv))
        args = _build_parser().parse_args(head)
        return _dispatch(args, extra)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        with suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.close(devnull)
        return 0
    except InputError as err:
        sys.stderr.write(f"bf: invalid input: {printable(str(err))}\n")
        return 2
    except (Error, OSError, UnicodeError) as err:
        sys.stderr.write(f"bf: {printable(failure(err))}\n")
        return 1


def main() -> None:
    raise SystemExit(run())
