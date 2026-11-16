"""Command-line interface for Brain Framework (`bf`)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

from bf import __version__
from bf.models import MAX_BATCH, Error, InputError, failure, printable, terminal


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InputError(message)


def _sub(sub: argparse._SubParsersAction[_Parser], name: str, help_text: str, *, brain: bool = True) -> _Parser:
    p = sub.add_parser(name, help=help_text)
    if brain:
        p.add_argument("--brain", default="", metavar="PATH", help="Brain root directory.")
    return p


def _build_parser() -> _Parser:
    desc = "Compile OKF notes to JSON, validate JSON Schemas, query via rg/jq/DuckDB, and orchestrate jobs."
    parser = _Parser(prog="bf", description=desc, allow_abbrev=False, suggest_on_error=True)
    parser.add_argument("--version", action="version", version=__version__)
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

    p_action = _sub(sub, "action", "Create or update actions/YYYY-MM-DD_<slug>/ACTION.md.")
    p_action.add_argument("topic", metavar="TOPIC", help="Action title or YYYY-MM-DD_slug.")
    p_action.add_argument("--status", choices=("draft", "stable", "deprecated"), default="draft", help="Action status.")
    p_action.add_argument("--summary", default="", help="Optional one-line action summary.")

    p_read = _sub(sub, "read", "Read brain overview, note, #anchor, #jq selector, source:id, or bf:// URI.")
    p_read.add_argument("target", nargs="?", default="", metavar="TARGET", help="Target path, source:id, or bf:// URI.")

    p_query = _sub(sub, "query", "Query pure JSON memories via rg, jq, or sandboxed DuckDB.")
    p_query.add_argument("pattern", nargs="?", default="", metavar="QUERY", help="Lexical pattern for rg.")
    mode_grp = p_query.add_mutually_exclusive_group()
    mode_grp.add_argument("--jq", default="", metavar="FILTER", help="jq filter expression over JSON memories.")
    mode_grp.add_argument("--sql", default="", metavar="SQL", help="DuckDB SQL query over `records` table.")
    p_query.add_argument("--scope", default="", metavar="SCOPE", help="Optional folder or source name.")
    p_query.add_argument("--limit", type=int, default=20, metavar="N", help="Max items for rg/jq (default 20).")

    _sub(sub, "validate", "Validate bf.yaml, schemas/, OKF notes, JSON memories, logs, and links.")
    _sub(sub, "review", "List stale notes, newer linked evidence, draft actions, and open tasks.")
    _sub(sub, "eval", "Run deterministic evaluations in evals/.").add_argument("target", nargs="?", default="")

    p_run = _sub(sub, "run", "Execute jobs declared in bf.yaml (scheduled, event-driven, or Git hooks).")
    p_run.add_argument("jobs", nargs="*", metavar="JOB", help="Specific job names to execute.")
    p_run.add_argument("--due", action="store_true", help="Run only due scheduled or event-matched jobs.")
    p_run.add_argument("--hook", choices=("pre-commit", "pre-push"), default="", help="Run jobs matching Git hook.")
    p_run.add_argument("--dry-run", action="store_true", help="Validate job selection without executing commands.")

    p_status = _sub(sub, "status", "Inspect job/channel/server run logs and event sequence.")
    p_status.add_argument("--check", action="store_true", help="Exit 1 if required binaries fail or a unit failed.")

    p_post = _sub(sub, "post", "Send an outbound message through a configured channel.")
    p_post.add_argument("text", nargs="?", default="", metavar="TEXT", help="Message body (or pass --stdin).")
    p_post.add_argument("--to", default="", metavar="CHANNEL", help="Target channel name.")
    p_post.add_argument("--thread", default="", metavar="ID", help="Conversation or thread identifier.")
    p_post.add_argument("--reply-to", default="", metavar="ID", help="Inbound message ID being answered.")
    p_post.add_argument("--run", dest="run_id", default="", metavar="ID", help="Originating job run ID.")
    p_post.add_argument("--stdin", action="store_true", help="Read message text from stdin.")
    p_post.add_argument("--dry-run", action="store_true", help="Validate without invoking send command.")

    p_watch = _sub(sub, "watch", "Supervise constant servers, file changes, events, and scheduled jobs.")
    p_watch.add_argument("--once", action="store_true", help="Run one tick and exit.")
    p_watch.add_argument("--json", dest="json_stream", action="store_true", help="Emit compact JSON lines.")

    p_mcp = _sub(sub, "mcp", "Run the read-only Model Context Protocol server.")
    p_mcp.add_argument("--transport", choices=("stdio", "http"), default="stdio", help="MCP transport (default stdio).")
    p_mcp.add_argument("--port", type=int, default=8765, help="Localhost port for streamable HTTP transport.")
    return parser


def _read_input(spec: str) -> bytes:
    if spec == "-":
        data = sys.stdin.buffer.read(MAX_BATCH + 1)
    else:
        try:
            with Path(spec).expanduser().open("rb") as handle:
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
        head_argv, extra = (
            (items[: items.index("--")], items[items.index("--") + 1 :]) if "--" in items else (items, [])
        )
        args = parser.parse_args(head_argv)
        if extra and args.command != "run":
            raise InputError(f"unexpected passthrough arguments after -- for bf {args.command}")
        if args.command == "init":
            from bf import engine

            _emit(engine.init_brain(args.path, name=args.name))
            return 0
        if args.command == "mcp":
            from bf import mcp

            mcp.serve(args.brain, transport=args.transport, port=args.port)
            return 0

        from bf import config

        store = config.select(args.brain)
        if args.command == "build":
            from bf import engine

            res = engine.build(store)
            _emit(res)
            return 1 if res["problems"] else 0
        if args.command == "ingest":
            from bf import records

            _emit(
                records.ingest(
                    store,
                    args.source,
                    _read_input(args.input),
                    mode=args.mode,
                    allow_removal=args.allow_removal,
                    dry_run=args.dry_run,
                )
            )
            return 0
        if args.command == "action":
            from bf import engine

            _emit(engine.action(store, args.topic, status=args.status, summary=args.summary))
            return 0
        if args.command == "read":
            from bf import query

            _emit(query.read_target(store, args.target))
            return 0
        if args.command == "query":
            from bf import query

            if (args.jq or args.sql) and args.pattern:
                raise InputError("pass either positional QUERY (rg) or --jq/--sql, not both")
            if args.jq:
                _emit(query.query_jq(store, args.jq, scope=args.scope, limit=args.limit))
            elif args.sql:
                _emit(query.query_sql(store, args.sql, scope=args.scope))
            elif args.pattern:
                _emit(query.search_rg(store, args.pattern, scope=args.scope, limit=args.limit))
            else:
                raise InputError("pass a QUERY pattern for rg, --jq FILTER, or --sql SQL")
            return 0
        if args.command == "validate":
            from bf import engine

            res = engine.validate(store)
            _emit(res)
            return 0 if res["valid"] else 1
        if args.command == "review":
            from bf import engine

            _emit(engine.review(store))
            return 0
        if args.command == "eval":
            from bf import query

            res = query.evaluate(store, args.target)
            _emit(res)
            return 0 if res["ok"] else 1
        if args.command == "run":
            from bf import jobs

            stdin_data = _read_input("-") if args.hook == "pre-push" and not sys.stdin.isatty() else b""
            res = jobs.run_jobs(
                store,
                args.jobs,
                due_only=args.due,
                hook=args.hook,
                extra_args=extra,
                stdin_bytes=stdin_data,
                dry_run=args.dry_run,
            )
            _emit(res)
            return 0 if res["ok"] else 1
        if args.command == "status":
            from bf import jobs

            res = jobs.status_summary(store, check=args.check)
            _emit(res)
            return 0 if (not args.check or res["ok"]) else 1
        if args.command == "post":
            from bf import jobs

            msg = _read_input("-").decode("utf-8") if args.stdin else args.text
            _emit(
                jobs.post_message(
                    store,
                    msg,
                    to=args.to,
                    thread=args.thread,
                    reply_to=args.reply_to,
                    run_id=args.run_id,
                    dry_run=args.dry_run,
                )
            )
            return 0
        from bf import watch

        res = watch.serve(store, once=args.once, json_stream=args.json_stream)
        if args.once:
            _emit(res)
        return 0 if res["ok"] else 1
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
