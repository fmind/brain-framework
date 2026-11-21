"""Command-line interface for Brain Framework (`bf`)."""

from __future__ import annotations

import argparse
import json
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
        "Brain: --brain > BF_BRAIN > enclosing bf.yaml > DEFAULT_BRAIN_PATH. "
        "Exit codes: 0=ok, 1=error, 2=invalid input, 130=interrupted."
    )
    p = _Parser(
        prog="bf", description="Brain Framework CLI.", epilog=p_epilog, allow_abbrev=False, suggest_on_error=True
    )
    p.add_argument("--version", action="version", version=__version__)
    _add_brain_opt(p, dest="root_brain")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    sub.required = True

    def cmd(name: str, help_text: str, *, with_brain: bool = True) -> _Parser:
        sp = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False, suggest_on_error=True)
        if with_brain:
            _add_brain_opt(sp)
        return sp

    sp = cmd("init", "Initialize a new brain directory.", with_brain=False)
    sp.add_argument("path", type=Path, help="Target directory (new or empty).")
    sp.add_argument("--name", default=None, help="Brain identifier (lowercase letters, digits, hyphens).")
    sp.add_argument("--description", default="", help="Short brain description written to bf.yaml.")

    sp = cmd("build", "Compile OKF Markdown notes into memories/**/*.json.")
    sp.add_argument("--force", action="store_true", help="Recompile every note, ignoring incremental cache.")

    sp = cmd("ingest", "Validate and save JSON records into memories/<source>/.")
    sp.add_argument("source", help="Sensor source name (lowercase letters, digits, hyphens).")
    sp.add_argument("--input", default="-", metavar="FILE", help="Input JSON/JSONL file path or '-' for stdin.")
    sp.add_argument("--mode", choices=("upsert", "snapshot"), default="upsert", help="Write mode (default: upsert).")
    sp.add_argument("--allow-removal", action="store_true", help="Allow snapshot to empty/drop >50%% of 10+ records.")
    sp.add_argument("--dry-run", action="store_true", help="Validate and preview counts without writing files.")

    sp = cmd("read", "Read an OKF note, #anchor section, source:id record, or bf:// URI.")
    sp.add_argument("target", help="Note path, note#anchor, source:id, memory path, alias, or bf:// URI.")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--outline", action="store_true", help="Return headings, open task count, and link count only.")
    grp.add_argument("--lines", default=None, metavar="START[:COUNT]", help="1-based line slice (START: reads to EOF).")
    grp.add_argument("--raw", action="store_true", help="Return raw Markdown source text.")
    grp.add_argument("--full", action="store_true", help="Return full compiled JSON record.")
    sp.add_argument("--fields", default=None, metavar="F,...", help="Comma-separated top-level fields to return.")
    sp.add_argument("--max-bytes", type=int, default=None, metavar="N", help="Maximum UTF-8 JSON response size.")

    sp = cmd("query", "Search and filter memories/**/*.json via rg and jq.")
    sp.add_argument("pattern", nargs="?", default=None, help="ripgrep regex pattern (optional with --jq or filters).")
    sp.add_argument("--jq", default=None, metavar="EXPR", help="Isolated jq filter expression.")
    sp.add_argument("--kind", choices=("note", "memory"), default="", help="Filter by record kind.")
    sp.add_argument("-s", "--scope", default="", metavar="PATH", help="Restrict to a note, source, or memories/ path.")
    sp.add_argument("--fields", default=None, metavar="F,...", help="Comma-separated item fields to project.")
    sp.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="Max results (1..5000, default: 20).")
    sp.add_argument("--format", choices=("json", "jsonl", "tsv"), default="json", help="Output format (default: json).")

    sp = cmd("review", "Review stale notes, newer linked evidence, draft actions, and open tasks.")
    sp.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="Max stale/action items (default: 20).")
    sp.add_argument("--all", dest="all_items", action="store_true", help="Return all stale notes and open actions.")
    sp.add_argument("--fields", default=None, metavar="F,...", help="Comma-separated item fields to project.")
    sp.add_argument("--format", choices=("json", "jsonl", "tsv"), default="json", help="Output format (default: json).")

    sp = cmd("check", "Run incremental build, schema/link validation, and deterministic evals.")
    sp.add_argument("target", nargs="?", default=None, help="Optional eval filename or stem.")
    cgrp = sp.add_mutually_exclusive_group()
    cgrp.add_argument("--validate", dest="validate_only", action="store_true", help="Validate only (read-only).")
    cgrp.add_argument("--eval", dest="eval_only", action="store_true", help="Run eval suites only.")
    sp.add_argument("-n", "--limit", type=int, default=None, metavar="N", help="Max validation problems (default: 50).")
    sp.add_argument("--all", dest="all_problems", action="store_true", help="Return all problems without truncation.")
    sp.add_argument("--strict", action="store_true", help="Fail validation on warnings (snapshot, forgotten, skill).")
    sp.add_argument("--force", action="store_true", help="Recompile every note and bypass validation cache.")

    sp = cmd("new", "Scaffold a new OKF Markdown note and compile it.")
    sp.add_argument("kind", choices=("concept", "project", "action"), help="Note category.")
    sp.add_argument("slug", help="Lowercase hyphenated filename slug.")
    sp.add_argument("--title", default="", help="Note H1 title (defaults to title-cased slug).")
    sp.add_argument("--type", dest="note_type", default="", help="Frontmatter type (default: guide/project/action).")
    sp.add_argument("--status", default="", help="Frontmatter status (default: draft for action, else stable).")

    sp = cmd("schema", "Print bundled Draft 2020-12 JSON Schemas.", with_brain=False)
    sp.add_argument("name", nargs="?", default=None, help="Schema name: bf, record, concepts, or evals.")

    sp = cmd("run", "Execute named, due, or Git-hook loops from bf.yaml (use '-- ARGS...' to pass arguments).")
    sp.add_argument("loops", nargs="*", help="Cycle loop names to run sequentially (or reset with --reset).")
    rgrp = sp.add_mutually_exclusive_group()
    rgrp.add_argument("--due", action="store_true", help="Rebuild notes and run scheduled/event-triggered loops.")
    rgrp.add_argument("--hook", choices=("pre-commit", "pre-push"), default="", help="Run loops for a Git hook.")
    rgrp.add_argument("--reset", action="store_true", help="Reset .bf/<loop>.json state for named loops to idle.")
    sp.add_argument("--dry-run", action="store_true", help="Validate triggers and preview loops without executing.")

    sp = cmd("emit", "Append a custom event to .bf/events.jsonl and logs/events.log.")
    sp.add_argument("kind", help="Event kind identifier (e.g. task.ready).")
    sp.add_argument("--source", default="bf", help="Event source name (default: bf).")
    sp.add_argument("--data", default="", metavar="JSON", help="Optional JSON object payload.")
    sp.add_argument("--dedupe", action="store_true", help="Deduplicate on kind, source, and data.id.")

    sp = cmd("status", "Inspect brain counts, failing loop attention, and orphaned states.")
    sp.add_argument("--all", dest="all_units", action="store_true", help="Include full per-loop status map.")
    sp.add_argument("--fields", default=None, metavar="F,...", help="Comma-separated top-level fields to return.")
    sp.add_argument("--check", action="store_true", help="Verify requires: commands and exit 1 on any problem.")
    sp.add_argument("--prune", action="store_true", help="Remove orphaned .bf/ state files and stale cache shards.")

    sp = cmd("watch", "Supervise daemon loops, file rebuilds, and concurrent cycle loops.")
    sp.add_argument("--json", dest="json_stream", action="store_true", help="Stream watch.tick JSON lines (no TUI).")

    cmd("mcp", "Run the read-only stdio MCP server.")
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


def _emit_fmt(res: dict[str, Any], fmt: str, fields: str | None, *, review: bool = False) -> None:
    if fmt == "json":
        return _emit(res)
    raw_c = [f.strip() for f in (fields or "").split(",") if f.strip()]
    tag = review and (not raw_c or "section" in raw_c)
    rows = (
        [
            {"section": sec, **row} if tag else row
            for sec, key in (("stale", "stale"), ("action_open", "actions_open"))
            for row in res[key]
        ]
        if review
        else res["items"]
    )
    with suppress(BrokenPipeError):
        if fmt == "jsonl":
            sys.stdout.buffer.write(b"".join(terminal(r) for r in rows))
        elif rows:
            cols = raw_c or list(dict.fromkeys(k for r in rows if isinstance(r, dict) for k in r))

            def cell(val: object) -> str:
                raw = (
                    json.dumps(val, ensure_ascii=False, separators=(",", ":")) if isinstance(val, (dict, list)) else val
                )
                return printable(
                    ("" if raw is None else str(raw)).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                )

            lines = ["\t".join(cell(r.get(k)) for k in cols) if isinstance(r, dict) else cell(r) for r in rows]
            sys.stdout.buffer.write(("\n".join(lines) + "\n").encode())
        sys.stdout.flush()
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
    if cmd == "mcp":
        from bf import mcp

        mcp.serve(brain_arg)
        return 0
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

        return 0 if watch.serve(store, json_stream=args.json_stream)["ok"] else 1
    if cmd == "status":
        from bf import engine, loops

        wanted = engine.parse_fields(args.fields)
        res = loops.status_summary(store, check=args.check, all_units=args.all_units, prune=args.prune)
        ok = bool(res.get("ok", True))
        if wanted is not None:
            res = {k: res[k] for k in wanted if k in res}
        _emit(res)
        return 0 if (not args.check or ok) else 1
    if cmd == "emit":
        from bf import models, records

        brain_name = config.load(store).name
        if not models.EVENT_KIND_RE.fullmatch(args.kind):
            raise InputError("event kind must match ^[a-z][a-z0-9._-]{0,127}$")
        if not models.NAME_RE.fullmatch(args.source):
            raise InputError("event source must use lowercase letters, digits, and hyphens")
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
        from bf import query

        if args.target is not None and not args.target.strip():
            raise InputError("eval target must be nonempty")
        if args.eval_only and (args.limit is not None or args.all_problems or args.strict or args.force):
            raise InputError("--limit, --all, --strict, and --force cannot be combined with --eval")
        v_on, e_on = not args.eval_only, not args.validate_only
        res = query.check_brain(
            store,
            target=args.target or "",
            build_notes=v_on and e_on,
            run_validate=v_on,
            run_eval=e_on,
            limit=50 if args.limit is None else args.limit,
            all_problems=args.all_problems,
            strict=args.strict,
            force=args.force,
        )
        _emit(res)
        return 0 if res["ok"] else 1
    if cmd == "run":
        from bf import loops

        if args.reset:
            if args.dry_run or extra:
                raise InputError("--reset cannot be combined with --dry-run or passthrough -- arguments")
            _emit(loops.reset_loops(store, args.loops))
            return 0
        stdin_b = _read_in("-") if (args.hook == "pre-push" and not args.dry_run and not sys.stdin.isatty()) else b""
        res = loops.run_loops(
            store,
            args.loops,
            due_only=args.due,
            hook=args.hook,
            extra_args=extra,
            stdin_bytes=stdin_b,
            dry_run=args.dry_run,
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
                mode=args.mode,
                allow_removal=args.allow_removal,
                dry_run=args.dry_run,
            )
        )
        return 0
    if cmd == "read":
        from bf import query

        _emit(
            query.read_target(
                store,
                args.target,
                outline=args.outline,
                lines=args.lines,
                fields=args.fields,
                max_bytes=args.max_bytes,
                raw=args.raw,
                full=args.full,
            )
        )
        return 0
    if cmd == "query":
        from bf import query

        res = query.query(
            store, args.pattern, jq_expr=args.jq, scope=args.scope, kind=args.kind, fields=args.fields, limit=args.limit
        )
        _emit_fmt(res, args.format, args.fields, review=False)
        return 0
    if cmd == "build":
        from bf import engine

        res = engine.build(store, force=args.force)
        _emit(res)
        return 0 if res["ok"] else 1
    if cmd == "new":
        from bf import engine

        res = engine.new_note(
            store, None, args.kind, args.slug, title=args.title, status=args.status, note_type=args.note_type
        )
        _emit(res)
        return 0 if res["ok"] else 1
    from bf import engine

    res = engine.review(store, limit=args.limit, all_items=args.all_items, fields=args.fields)
    _emit_fmt(res, args.format, args.fields, review=True)
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
        return 0
    except InputError as err:
        sys.stderr.write(f"bf: invalid input: {printable(str(err))}\n")
        return 2
    except (Error, OSError, UnicodeError) as err:
        sys.stderr.write(f"bf: {printable(failure(err))}\n")
        return 1


def main() -> None:
    raise SystemExit(run())
