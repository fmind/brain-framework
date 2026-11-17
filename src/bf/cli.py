"""Command-line interface for Brain Framework (`bf`)."""

from __future__ import annotations

import argparse
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


def _sub(sub: argparse._SubParsersAction[_Parser], name: str, help_text: str, *, brain: bool = True) -> _Parser:
    p = sub.add_parser(name, help=help_text, allow_abbrev=False, suggest_on_error=True)
    if brain:
        p.add_argument("--brain", action="append", default=None, metavar="PATH", help="Brain root directory.")
    return p


def _build_parser() -> _Parser:
    desc = "Compile OKF notes to JSON, validate JSON Schemas, query via rg/jq, and orchestrate jobs."
    parser = _Parser(prog="bf", description=desc, allow_abbrev=False, suggest_on_error=True)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--brain", dest="root_brain", action="append", default=None, metavar="PATH", help="Brain root.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    sub.required = True

    p_init = _sub(sub, "init", "Create a new brain with flat folders, default schemas, and /bf skill.", brain=False)
    p_init.add_argument("path", type=Path, metavar="PATH", help="New or empty directory for the brain.")
    p_init.add_argument("--name", default="", help="Explicit brain name (defaults to directory slug).")
    _sub(sub, "build", "Compile OKF Markdown notes into memories/{concepts,projects,actions}/*.json.")

    p_ingest = _sub(sub, "ingest", "Validate and save JSON records into memories/<source>/<sha256>.json.")
    p_ingest.add_argument("source", metavar="SOURCE", help="Lowercase source name for memories/<source>/.")
    p_ingest.add_argument("--input", default="-", metavar="FILE", help="Input JSON/JSONL file path, or '-' for stdin.")
    p_ingest.add_argument("--mode", choices=("upsert", "snapshot"), default="upsert", help="Merge or snapshot mode.")
    p_ingest.add_argument("--allow-removal", action="store_true", help="Allow snapshot to drop >50%% of >=10 records.")
    p_ingest.add_argument("--dry-run", action="store_true", help="Validate and report counts without writing files.")

    p_read = _sub(sub, "read", "Read an OKF note, note#anchor section, source:id record, JSON memory, or bf:// URI.")
    p_read.add_argument("target", metavar="TARGET", help="Note path, source:id, JSON memory path, or bf:// URI.")

    p_query = _sub(sub, "query", "Query pure JSON memories via rg or jq.")
    p_query.add_argument("pattern", nargs="?", default=None, metavar="QUERY", help="Lexical pattern for rg.")
    p_query.add_argument("--jq", default=None, metavar="FILTER", help="jq filter expression over JSON memories.")
    p_query.add_argument("-s", "--scope", default="", metavar="SCOPE", help="Optional folder or source name.")
    p_query.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="Max items returned (default 20).")

    _sub(sub, "validate", "Validate bf.yaml, schemas/, OKF notes, JSON memories, evals, and links.")
    _sub(sub, "review", "List stale notes, newer linked evidence, draft actions, and open tasks.")

    p_eval = _sub(sub, "eval", "Run deterministic evaluations in evals/.")
    p_eval.add_argument("target", nargs="?", default="", metavar="SUITE", help="Optional eval file path or stem.")

    p_run = _sub(sub, "run", "Execute jobs declared in bf.yaml (named, due, or Git hooks).")
    p_run.add_argument("jobs", nargs="*", metavar="JOB", help="Specific job names to execute.")
    sel = p_run.add_mutually_exclusive_group()
    sel.add_argument("--due", action="store_true", help="Rebuild dirty notes and run due scheduled/event jobs.")
    sel.add_argument("--hook", choices=("pre-commit", "pre-push"), default="", help="Run jobs matching Git hook.")
    p_run.add_argument("--dry-run", action="store_true", help="Validate job selection without executing commands.")

    p_status = _sub(sub, "status", "Inspect folder counts, attention, unit states, and event sequence.")
    p_status.add_argument("--check", action="store_true", help="Exit 1 if required binaries fail or a unit failed.")

    p_watch = _sub(sub, "watch", "Supervise constant services, file changes, events, and scheduled jobs.")
    p_watch.add_argument("--json", dest="json_stream", action="store_true", help="Emit compact JSON lines.")

    p_mcp = _sub(sub, "mcp", "Run the read-only Model Context Protocol server.")
    p_mcp.add_argument("--transport", choices=("stdio", "http"), default="stdio", help="MCP transport (default stdio).")
    p_mcp.add_argument("--port", type=int, default=None, help="Localhost port for HTTP transport (default 8765).")
    return parser


def _split_run_passthrough(items: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in items:
        return items, []
    sep = items.index("--")
    idx = 0
    while idx < sep:
        tok = items[idx]
        if tok == "--brain" or tok.startswith("-"):
            idx += 2 if tok == "--brain" else 1
            continue
        return (items[:sep], items[sep + 1 :]) if tok == "run" else (items, [])
    return items, []


def _resolve_brain_arg(args: argparse.Namespace) -> str:
    root_vals: list[str] = getattr(args, "root_brain", None) or []
    sub_vals: list[str] = getattr(args, "brain", None) or []
    if args.command == "init" and root_vals:
        raise InputError("bf init takes PATH positionally and does not accept --brain")
    vals = [*root_vals, *sub_vals]
    if any(not v.strip() for v in vals):
        raise InputError("--brain must be nonempty")
    if len(set(vals)) > 1:
        raise InputError("conflicting --brain values")
    return vals[0] if vals else ""


def _read_input(spec: str) -> bytes:
    if spec == "-":
        data = sys.stdin.buffer.read(MAX_BATCH + 1)
    else:
        try:
            with expand(Path(spec)).open("rb") as handle:
                data = handle.read(MAX_BATCH + 1)
        except OSError as error:
            raise Error(f"cannot read input file{f' ({error.strerror})' if error.strerror else ''}") from error
    if len(data) > MAX_BATCH:
        raise Error(f"input exceeds {MAX_BATCH} bytes; split the batch")
    return data


def _emit(payload: object) -> None:
    with suppress(BrokenPipeError):
        sys.stdout.buffer.write(terminal(payload))
        sys.stdout.flush()


def run(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI invocation and return its POSIX exit code (`0`, `1`, `2`, `130`)."""
    parser = _build_parser()
    try:
        items = list(sys.argv[1:] if argv is None else argv)
        head_argv, extra = _split_run_passthrough(items)
        args = parser.parse_args(head_argv)
        brain_arg = _resolve_brain_arg(args)
        if args.command == "init":
            from bf import engine

            _emit(engine.init_brain(args.path, name=args.name))
            return 0
        if args.command == "mcp":
            from bf import mcp

            mcp.serve(brain_arg, transport=args.transport, port=args.port)
            return 0
        from bf import config

        store, c = config.select(brain_arg), args.command
        if c == "watch":
            from bf import watch

            return 0 if watch.serve(store, json_stream=args.json_stream)["ok"] else 1
        if c in {"build", "validate", "review"}:
            from bf import engine

            res = {"build": engine.build, "validate": engine.validate, "review": engine.review}[c](store)
        elif c == "ingest":
            from bf import records

            if args.input == "-" and sys.stdin.isatty():
                raise InputError("stdin is a terminal; pipe JSON records or pass --input FILE")
            kw_in: dict[str, Any] = {"mode": args.mode, "allow_removal": args.allow_removal, "dry_run": args.dry_run}
            res = records.ingest(store, args.source, _read_input(args.input), **kw_in)
        elif c in {"read", "query", "eval"}:
            from bf import query

            if c == "read":
                res = query.read_target(store, args.target)
            elif c == "eval":
                res = query.evaluate(store, args.target)
            elif (args.jq is not None) == (args.pattern is not None):
                raise InputError("pass either positional QUERY (rg) or --jq FILTER")
            else:
                qkw: dict[str, Any] = {"scope": args.scope, "limit": args.limit}
                fn = query.query_jq if args.jq is not None else query.search_rg
                res = fn(store, args.jq if args.jq is not None else args.pattern, **qkw)
        else:
            from bf import jobs

            if c == "run":
                is_push = args.hook == "pre-push" and not args.dry_run and not sys.stdin.isatty()
                kw_run: dict[str, Any] = {"due_only": args.due, "hook": args.hook, "extra_args": extra}
                kw_run["stdin_bytes"] = _read_input("-") if is_push else b""
                res = jobs.run_jobs(store, args.jobs, **kw_run, dry_run=args.dry_run)
            else:
                res = jobs.status_summary(store, check=args.check)
        _emit(res)
        return 0 if (c in {"ingest", "read", "query"} or (c == "status" and not args.check) or res["ok"]) else 1
    except KeyboardInterrupt:
        return 130
    except InputError as error:
        sys.stderr.write(f"bf: invalid input: {printable(str(error))}\n")
        return 2
    except (Error, OSError, UnicodeError) as error:
        sys.stderr.write(f"bf: {printable(failure(error))}\n")
        return 1


def main() -> None:
    raise SystemExit(run())
