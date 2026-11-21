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
    sp = cmd("build", "Compile OKF Markdown notes into memories/**/*.json.")
    sp.add_argument("--force", action="store_true", help="Recompile every note, ignoring incremental cache.")
    sp = cmd("ingest", "Validate and save JSON records into memories/<source>/.")
    sp.add_argument("source", help="Sensor source name (lowercase letters, digits, hyphens).")
    sp.add_argument("--input", default="-", metavar="FILE", help="Input JSON/JSONL file path or '-' for stdin.")
    sp.add_argument("--mode", choices=("upsert", "snapshot"), default="upsert", help="Write mode (default: upsert).")
    sp.add_argument(
        "--allow-removal", action="store_true", help="Allow snapshot to empty or drop >50%% of 10+ records."
    )
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
    sp.add_argument("--source", default="", metavar="NAME", help="Restrict to one source directory.")
    sp.add_argument("--fields", default=None, metavar="F,...", help="Comma-separated item fields to project.")
    sp.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="Max results (1..5000, default: 20).")
    sp.add_argument("--format", choices=("json", "jsonl", "tsv"), default="json", help="Output format (default: json).")
    sp = cmd("validate", "Validate bf.yaml, schemas, notes, compiled sync, records, evals, and links.")
    sp.add_argument("--limit", type=int, default=50, metavar="N", help="Max problems returned (default: 50).")
    sp.add_argument("--all", dest="all_problems", action="store_true", help="Return all problems without truncation.")
    sp.add_argument("--strict", action="store_true", help="Fail on snapshot/forgotten link warnings.")
    sp.add_argument("--force", action="store_true", help="Bypass cached validation digests.")
    sp = cmd("review", "Review stale notes, newer linked evidence, draft actions, and open tasks.")
    sp.add_argument("--limit", type=int, default=20, metavar="N", help="Max stale/action items (default: 20).")
    sp.add_argument("--all", dest="all_items", action="store_true", help="Return all stale notes and open actions.")
    sp.add_argument("--fields", default=None, metavar="F,...", help="Comma-separated item fields to project.")
    sp.add_argument("--format", choices=("json", "jsonl", "tsv"), default="json", help="Output format (default: json).")
    sp = cmd("eval", "Run deterministic query evaluation suites in evals/.")
    sp.add_argument("target", nargs="?", default="", help="Optional eval filename or stem.")
    sp = cmd("check", "Run incremental build, validate, and eval in one pass.")
    sp.add_argument("--strict", action="store_true", help="Fail validation on snapshot/forgotten link warnings.")
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
    sp.add_argument("loops", nargs="*", help="Cycle loop names to run sequentially.")
    rgrp = sp.add_mutually_exclusive_group()
    rgrp.add_argument("--due", action="store_true", help="Rebuild notes and run scheduled/event-triggered loops.")
    rgrp.add_argument("--hook", choices=("pre-commit", "pre-push"), default="", help="Run loops for a Git hook.")
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
    sp = cmd("watch", "Supervise daemon loops, file rebuilds, and concurrent cycle loops.")
    sp.add_argument("--json", dest="json_stream", action="store_true", help="Stream watch.tick JSON lines (no TUI).")
    cmd("mcp", "Run the read-only stdio MCP server.")
    return p


def _split_run(items: list[str]) -> tuple[list[str], list[str]]:
    if "--" in items:
        sep, idx = items.index("--"), 0
        while idx < sep:
            if (t := items[idx]) == "--brain" or t.startswith("-"):
                idx += 2 if t == "--brain" else 1
                continue
            return (items[:sep], items[sep + 1 :]) if t == "run" else (items, [])
    return items, []


def _read_in(spec: str) -> bytes:
    try:
        if spec == "-":
            data = sys.stdin.buffer.read(MAX_BATCH + 1)
        else:
            with expand(Path(spec)).open("rb") as fh:
                data = fh.read(MAX_BATCH + 1)
    except OSError as e:
        raise Error(f"cannot read input file{f' ({e.strerror})' if e.strerror else ''}") from e
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
            *(
                {"section": s, **r} if tag else r
                for s, k in (("stale", "stale"), ("action_open", "actions_open"))
                for r in res[k]
            )
        ]
        if review
        else res["items"]
    )
    with suppress(BrokenPipeError):
        if fmt == "jsonl":
            sys.stdout.buffer.write(b"".join(terminal(r) for r in rows))
        elif rows:
            cols = raw_c or list(dict.fromkeys(k for r in rows if isinstance(r, dict) for k in r))

            def cell(v: object) -> str:
                s = (
                    json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(v, (dict, list))
                    else ("" if v is None else str(v))
                )
                return printable(s.replace("\t", " ").replace("\r", " ").replace("\n", " "))

            lines = ["\t".join(cell(r.get(k)) for k in cols) if isinstance(r, dict) else cell(r) for r in rows]
            sys.stdout.buffer.write(("\n".join(lines) + "\n").encode())
        sys.stdout.flush()
    return None


def _mod(name: str) -> Any:
    return __import__(f"bf.{name}", fromlist=[name])


def run(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI invocation and return its POSIX exit code (`0`, `1`, `2`, `130`)."""
    try:
        head, extra = _split_run(list(sys.argv[1:] if argv is None else argv))
        a = _build_parser().parse_args(head)
        c, rv, sv = a.command, getattr(a, "root_brain", None) or [], getattr(a, "brain", None) or []
        if c in {"init", "schema"} and rv:
            raise InputError(f"bf {c} does not accept --brain")
        vals = [*rv, *sv]
        if any(not v.strip() for v in vals) or len(set(vals)) > 1:
            raise InputError(
                "--brain must be nonempty" if any(not v.strip() for v in vals) else "conflicting --brain values"
            )
        b_arg = vals[0] if vals else ""
        if c == "mcp":
            _mod("mcp").serve(b_arg)
            return 0
        if c == "schema":
            _emit(_mod("schemas").get_schema(a.name) if a.name is not None else _mod("schemas").export_schemas())
            return 0
        if c == "init":
            if a.name is not None and not a.name.strip():
                raise InputError("--name must be nonempty")
            _emit(_mod("engine").init_brain(a.path, name=a.name or ""))
            return 0
        st = _mod("config").select(b_arg)
        if c == "watch":
            return 0 if _mod("watch").serve(st, json_stream=a.json_stream)["ok"] else 1
        if c == "status":
            res = _mod("loops").status_summary(st, check=a.check, all_units=a.all_units)
            ok = bool(res.get("ok", True))
            if (wanted := _mod("engine").parse_fields(a.fields)) is not None:
                res = {k: res[k] for k in wanted if k in res}
            _emit(res)
            return 0 if (not a.check or ok) else 1
        if c == "emit":
            brain_name, mod_m = _mod("config").load(st).name, _mod("models")
            if not mod_m.EVENT_KIND_RE.fullmatch(a.kind):
                raise InputError("event kind must match ^[a-z][a-z0-9._-]{0,127}$")
            if not mod_m.NAME_RE.fullmatch(a.source):
                raise InputError("event source must use lowercase letters, digits, and hyphens")
            try:
                raw_d = mod_m.decode(a.data, "--data") if a.data else {}
            except Error as err:
                raise InputError(str(err)) from err
            if not isinstance(raw_d, dict):
                raise InputError("--data must be a JSON object")
            ev = _mod("records").append_event(st, a.kind, a.source, raw_d, dedupe=a.dedupe)
            _emit({"ok": True, "brain": brain_name, "event": ev})
            return 0
        if c == "ingest" and a.input == "-" and sys.stdin.isatty():
            raise InputError("stdin is a terminal; pipe JSON records or pass --input FILE")
        b = _read_in("-") if (c == "run" and a.hook == "pre-push" and not a.dry_run and not sys.stdin.isatty()) else b""
        pick = lambda *ks: {k: getattr(a, k) for k in ks}  # noqa: E731
        ops = {
            "ingest": lambda: _mod("records").ingest(
                st, a.source, _read_in(a.input), **pick("mode", "allow_removal", "dry_run")
            ),
            "run": lambda: _mod("loops").run_loops(
                st, a.loops, due_only=a.due, extra_args=extra, stdin_bytes=b, **pick("hook", "dry_run")
            ),
            "read": lambda: _mod("query").read_target(
                st, a.target, **pick("outline", "lines", "fields", "max_bytes", "raw", "full")
            ),
            "query": lambda: _mod("query").query(
                st, a.pattern, jq_expr=a.jq, **pick("scope", "kind", "source", "fields", "limit")
            ),
            "eval": lambda: _mod("query").evaluate(st, a.target),
            "build": lambda: _mod("engine").build(st, force=a.force),
            "check": lambda: _mod("engine").check_brain(st, strict=a.strict, force=a.force),
            "new": lambda: _mod("engine").new_note(st, None, a.kind, a.slug, **pick("title", "status", "note_type")),
            "review": lambda: _mod("engine").review(st, **pick("limit", "all_items", "fields")),
            "validate": lambda: _mod("engine").validate(st, **pick("limit", "all_problems", "strict", "force")),
        }
        res = ops[c]()
        if c in {"query", "review"}:
            _emit_fmt(res, a.format, a.fields, review=c == "review")
            if c == "review" and a.format != "json" and not res["ok"] and res.get("problems"):
                sys.stderr.write(f"bf: {printable('; '.join(str(p) for p in res['problems']))}\n")
            return 0 if (c == "query" or res["ok"]) else 1
        _emit(res)
        return 0 if (c in {"ingest", "read"} or res["ok"]) else 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except InputError as e:
        sys.stderr.write(f"bf: invalid input: {printable(str(e))}\n")
        return 2
    except (Error, OSError, UnicodeError) as e:
        sys.stderr.write(f"bf: {printable(failure(e))}\n")
        return 1


def main() -> None:
    raise SystemExit(run())
