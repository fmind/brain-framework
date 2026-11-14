"""One compact CLI; stdout is JSON and failures remain on stderr."""

from __future__ import annotations

import io
import os
import re
import selectors
import signal
import stat
import sys
import time
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack, suppress
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from string import Template
from types import FrameType
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn, cast

import typer
from pydantic import ValidationError
from typer.completion import completion_init
from typer.core import TyperCommand, TyperGroup, TyperOption

from bf import __version__
from bf.models import (
    ACTION_SLUG,
    FAILURES,
    FORMAT,
    MAX_NOTE,
    MAX_OFFSET,
    NAME,
    SLUG,
    Bounds,
    Error,
    InputError,
    NotFoundError,
    SchemaKind,
    clean,
    decode,
    explain,
    failure,
    moment,
    parse_duration,
    present,
    printable,
    suggest,
    terminal,
)

if TYPE_CHECKING:
    from bf.storage import Store

_Opt, _Arg = typer.Option, typer.Argument


def _diagnose(message: str) -> None:
    """Print one `bf:` line on stderr, escaping controls so brain data cannot forge another line."""
    typer.echo("bf: " + printable(message), err=True)


def _usage(error: typer.TyperException) -> str:
    """Invalid input as one diagnostic naming the argument, reason and help path."""
    ctx: Any = getattr(error, "ctx", None)
    hint, message = "", error.format_message()
    if isinstance(error, typer.BadParameter) and error.message:
        raw = error.param_hint or _param_hint(error.param)
        hint = (raw if isinstance(raw, str) else " / ".join(raw)).replace("'", "")
        message = error.message.removeprefix("Value error, ")
    if not hint and message[1:2].islower():
        message = message[:1].lower() + message[1:]
    reason = f"{hint}: {message}" if hint else message
    return f"invalid input: {reason.rstrip('.')}" + (f" (see {ctx.command_path} -h)" if ctx is not None else "")


def _param_hint(param: Any) -> str:
    if param is None:
        return ""
    if getattr(param, "param_type_name", "") == "option":
        raw = [*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())]
    else:
        raw = getattr(param, "metavar", None) or (getattr(param, "name", None) or "").upper()
    return (raw if isinstance(raw, str) else " / ".join(raw)).replace("'", "")


class _Group(TyperGroup):
    def _run(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except typer.TyperException as error:
            if error.exit_code != 2 or type(error).__name__ == "NoArgsIsHelpError":
                raise
            _diagnose(_usage(error))
            raise typer.Exit(2) from None

    def make_context(self, info_name: str | None, args: list[str], parent: Any = None, **extra: Any) -> Any:
        return self._run(lambda: super(_Group, self).make_context(info_name, args, parent, **extra))

    def invoke(self, ctx: Any) -> Any:
        return self._run(lambda: super(_Group, self).invoke(ctx))


completion_init()
_TYPER: dict[str, Any] = {
    "cls": _Group,
    "no_args_is_help": True,
    "add_completion": False,
    "context_settings": {"help_option_names": ["-h", "--help"]},
    "pretty_exceptions_show_locals": False,
}
app = typer.Typer(
    **_TYPER,
    invoke_without_command=True,
    help="🧠 Brain Framework: from information to informed actions.",
    epilog="Start: bf init ~/brain, then bf read. Results are JSON; diagnostics go to stderr. See bf COMMAND -h.",
)
BrainOpt = Annotated[str, _Opt("--brain", help="Brain name/path; else BF_BRAIN, cwd, or default.")]
RunOpt = Annotated[str, _Opt("--brain", help="Registered or enclosing brain name or path (no default).")]
JobOpt = Annotated[list[str] | None, _Opt("--job", help="Select a job; repeat for more.")]
DryOpt = Annotated[bool, _Opt("--dry-run", help="Preview changes without writing.")]
SinceOpt = Annotated[str, _Opt(help="Window start ({{start}}): 7d, yesterday, YYYY-MM-DD or ISO.")]
UntilOpt = Annotated[str, _Opt(help="Window end ({{end}}).")]
RemovalOpt = Annotated[bool, _Opt("--allow-removal", help="Allow empty or mostly-removed snapshot.")]
ReconcileOpt = Annotated[bool, _Opt("--reconcile", help="Backfill over job's reconcile window.")]
RunIdOpt = Annotated[str, _Opt("--run", help="Active run ID from {{run}}.")]
KeyOpt = Annotated[str, _Opt("--key", help="Explicit idempotency key.")]
OffsetOpt = Annotated[int, _Opt(min=0, max=MAX_OFFSET, help="Continue at next_offset.")]
FOLDERS, OPTIONAL = ("projects", "actions"), ("memories", "assets", "sensors", "routines", "skills")
STARTER = {
    "AGENTS.md": "AGENTS.md",
    ".gitignore": "gitignore",
    "concepts/index.md": "concepts/index.md",
    "concepts/welcome.md": "concepts/welcome.md",
    "evals/retrieval.yaml": "evals/retrieval.yaml",
}
_ARGUMENTS = {
    "query": "QUERY",
    "ref": "REF",
    "rel": "--rel",
    "scope": "--scope",
    "offset": "--offset",
    "topic": "TOPIC",
}
SCHEMAS = f"https://raw.githubusercontent.com/fmind/brain-framework/v{__version__}/docs/"


def emit(value: object, *, reply: bool = True) -> None:
    """Write UTF-8 JSON to stdout, sanitizing collected controls for replies."""
    _write(terminal(present(value) if reply else value))


def _finish(value: object, *, failed: bool) -> None:
    emit(value)
    if failed:
        raise typer.Exit(1)


def _finish_indexed(store: Store, result: dict[str, object], *, failed: bool = False) -> None:
    from bf import index

    refreshed = index.refresh(store, recover=True)
    result["index"] = refreshed
    _finish(result, failed=failed or bool(refreshed.get("skipped")))


def _write(data: bytes, *, flush: bool = True) -> None:
    try:
        sys.stdout.buffer.write(data)
        if flush:
            sys.stdout.flush()
    except BrokenPipeError:
        raise
    except OSError as error:
        suffix = f" ({error.strerror})" if error.strerror else ""
        raise Error(f"cannot write the reply to standard output{suffix}") from error


def _option[T](name: str, parse: Callable[[str], T], value: str) -> T:
    try:
        return parse(value)
    except Error as error:
        raise typer.BadParameter(str(error), param_hint=name) from None


def _has(store: Store, path: str, *, directory: bool = False) -> bool:
    try:
        mode = store.mode(path)
    except Error, OSError:
        return False
    return stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)


def _name(value: str) -> str:
    if not re.fullmatch(NAME, value):
        raise typer.BadParameter(
            "choose a brain name of 1-64 chars: lowercase letter, then lowercase letters, digits or hyphens",
            param_hint="--name",
        )
    return value


def _single_line(name: str, value: str) -> str:
    stripped = value.strip()
    if not stripped or any(c in stripped for c in "\r\n"):
        raise typer.BadParameter("expected a non-empty single line", param_hint=name)
    try:
        return clean(stripped)
    except ValueError:
        raise typer.BadParameter("control and format characters are forbidden", param_hint=name) from None


def _window(since: str, until: str) -> tuple[datetime, str, str]:
    now = datetime.now(UTC)
    start = _option("--since", lambda v: moment(v, now), since) if since else ""
    end = _option("--until", lambda v: moment(v, now), until) if until else ""
    if start and end and start >= end:
        raise typer.BadParameter("must be earlier than --until", param_hint="--since")
    return now, start, end


@app.callback()
def root(version: Annotated[bool, _Opt("--version", is_eager=True, help="Show version and exit.")] = False) -> None:
    if version:
        _write(f"{__version__}\n".encode())
        raise typer.Exit


def _vacant(directory: Path, *, locked: bool = False) -> None:
    allowed = {".git", ".bf"} if locked else {".git"}
    if any(entry.name not in allowed for entry in directory.iterdir()):
        raise Error("initialization requires a new, empty or freshly cloned directory")


@app.command("init", rich_help_panel="Set up")
def initialize(
    path: Annotated[Path, _Arg(file_okay=False, metavar="PATH", help="New, empty or freshly cloned brain directory.")],
    name: Annotated[str | None, _Opt(help="Stable BF link namespace; defaults to folder name.")] = None,
    full: Annotated[bool, _Opt("--full", help="Create optional memories/, sensors/, routines/ folders.")] = False,
) -> None:
    """Create a brain at PATH (recommended: ~/brain); no global registration is needed."""
    import yaml

    from bf.models import starter_config
    from bf.storage import Store, expand, writer

    path = expand(path)
    if name is None:
        raw_name = path.resolve().name.lower()
        folded = "".join(c for c in unicodedata.normalize("NFKD", raw_name) if not unicodedata.combining(c))
        name = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
        if any(c.isalnum() and not c.isascii() for c in folded) or not re.fullmatch(NAME, name):
            raise typer.BadParameter("the directory name cannot form a brain name; pass --name NAME", param_hint="PATH")
    else:
        name = _name(name)
    cfg_obj = starter_config(name)
    if path.exists():
        _vacant(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(path)
    with writer(store):
        _vacant(store.root, locked=True)
        hdr = f"# https://fmind.github.io/brain-framework/\n# yaml-language-server: $schema={SCHEMAS}bf.schema.json\n"
        body = yaml.safe_dump(cfg_obj.model_dump(by_alias=True, exclude_defaults=True), sort_keys=False, width=1000)
        store.write("bf.yaml", (hdr + body).encode())
        for target, source in STARTER.items():
            text = (files("bf") / "starter" / source).read_text(encoding="utf-8")
            if target.endswith(".yaml"):
                text = Template(text).substitute(name=name, version=FORMAT, schemas=SCHEMAS)
            store.write(target, text.encode())
        for directory in FOLDERS + (OPTIONAL if full else ()):
            store.write(directory + "/.gitkeep", b"")
    emit({"created": str(store.root), "brain": name})


@app.command("register", rich_help_panel="Set up")
def enroll(
    path: Annotated[Path, _Arg(metavar="PATH", show_default=False, help="Brain directory; defaults to cwd.")] = Path(),
    name: Annotated[str | None, _Opt("--name", help="Registry alias; defaults to bf.yaml's brain name.")] = None,
    default: Annotated[bool, _Opt("--default", help="Select this brain by default outside any brain.")] = False,
) -> None:
    """Register an existing brain to select it by name outside its folder; never runs its jobs."""
    from bf.config import register
    from bf.storage import Store, expand

    alias, path = "" if name is None else _name(name), expand(path)
    if not path.is_dir():
        raise Error("PATH is not an existing directory; pass the brain's directory")
    if not (path / "bf.yaml").is_file():
        raise Error("PATH has no bf.yaml; run bf init PATH first, or pass the brain's root directory")
    emit(register(Store(path), name=alias, default=default))


@app.command("unregister", rich_help_panel="Set up")
def unenroll(target: Annotated[str, _Arg(metavar="TARGET", help="Registered brain name or path to remove.")]) -> None:
    """Remove a brain registration from the user registry; changes no files inside the brain."""
    from bf.config import unregister

    emit(unregister(target))


@app.command("mcp", rich_help_panel="Set up")
def serve(brain: BrainOpt = "") -> None:
    """Serve search and read over MCP stdio for agents that prefer tools to the CLI; needs the mcp extra."""
    from bf.config import select

    try:
        from bf.mcp import server
    except ModuleNotFoundError as error:
        msg = "bf mcp needs the mcp extra; install it with uv tool install --python 3.14 'brain-framework[mcp]'"
        raise Error(msg) from error
    if sys.stdin is None:
        raise Error("standard input is closed; an MCP host sends its requests through it")
    tools = server(lambda: select(brain))
    stop = lambda _signum, _frame: os._exit(130)  # noqa: E731
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        if signum != signal.SIGHUP or signal.getsignal(signum) is not signal.SIG_IGN:
            signal.signal(signum, stop)
    tools.run()


skill_app = typer.Typer(**_TYPER, help="Install and verify the packaged bf skill.")
app.add_typer(skill_app, name="skill", rich_help_panel="Set up")


@skill_app.command("install")
def skill_install(
    brain: BrainOpt = "",
    global_: Annotated[bool, _Opt("--global", help="Install into ~/.agents/skills/bf and ~/.claude.")] = False,
    check: Annotated[bool, _Opt("--check", help="Report status without writing files.")] = False,
    force: Annotated[bool, _Opt("--force", help="Replace edited, foreign or newer bf skill files.")] = False,
) -> None:
    """Install the packaged bf skill into one brain (and link .agents/.claude) or globally with --global."""
    from bf.config import one
    from bf.install import install as install_skill

    if global_ and brain:
        raise typer.BadParameter("cannot be combined with --global", param_hint="--brain")
    result = install_skill(None if global_ else one(brain), global_=global_, check=check, force=force)
    _finish(result, failed=not result["ok"])


@app.command("guide", rich_help_panel="Set up")
def guide_command(
    topic: Annotated[str | None, _Arg(metavar="[TOPIC]", show_default=False, help="Topic; omit for index.")] = None,
) -> None:
    """Print a Brain Framework guide or the guide topic directory as Markdown."""
    from bf.guide import read_guide, render_index

    _write((render_index() if topic is None else read_guide(topic)).encode("utf-8"))


@app.command("schema", rich_help_panel="Set up")
def schema(kind: Annotated[SchemaKind, _Opt(help="Config or reply JSON Schema kind.")] = "brain") -> None:
    """Print a configuration or reply JSON Schema; defaults to bf.yaml. No brain selection or network access."""
    from bf.schemas import document

    emit(document(kind), reply=False)


@app.command("search", rich_help_panel="Find and read")
def find(
    query: Annotated[str, _Arg(metavar="QUERY", help="Words or exact identity (repo:owner/name).")],
    brain: BrainOpt = "",
    scope: Annotated[str, _Opt(help="Prefix, period (today, 7d, 2026-09), identity, or tag.")] = "",
    limit: Annotated[int, _Opt(min=1, max=50, help="Maximum number of results.")] = 10,
    offset: OffsetOpt = 0,
) -> None:
    """Search notes and records; results carry refs for bf read."""
    from bf import config, pages, retrieve

    spec = pages.query(query, scope, limit=limit, offset=offset)
    emit(retrieve.search(config.select(brain), spec))


@app.command("read", rich_help_panel="Find and read")
def exact(
    ref: Annotated[str, _Arg(metavar="REF", help="Page, note, path#section, source:id or identity.")] = "",
    brain: BrainOpt = "",
    rel: Annotated[str, _Opt("--rel", help="List items linking to REF via this relation.")] = "",
    offset: OffsetOpt = 0,
) -> None:
    """Read the home page, another page, a note, a note section, a record or an identity with its backlinks."""
    from bf import config, pages, retrieve

    pages.readable(ref, rel)
    emit(retrieve.read(config.select(brain), ref, rel=rel, offset=offset))


@app.command("export", rich_help_panel="Find and read")
def export(
    kind: Annotated[Literal["edges", "identities", "items"], _Opt(help="edges, identities, or items JSONL.")] = "edges",
    brain: BrainOpt = "",
    scope: Annotated[str, _Opt(help="Export only items matching a search scope.")] = "",
    observed_since: Annotated[str, _Opt("--observed-since", help="With items, cutoff time or period.")] = "",
) -> None:
    """Print the graph of the selected brain as JSON Lines from its cache; exit 1 when evidence was skipped."""
    from bf import config, pages, retrieve

    if observed_since and kind != "items":
        raise typer.BadParameter("applies only to --kind items", param_hint="--observed-since")
    cutoff = (
        ""
        if not observed_since
        else _option("--observed-since", lambda v: moment(v, datetime.now(UTC)), observed_since)
    )
    p = pages.bounds(scope)
    bounds = Bounds(
        since=p.since, until=p.until, observed_since=cutoff, undated=p.undated, prefix=p.prefix, target=p.target
    )
    with ExitStack() as stack:
        stream, extra = getattr(retrieve, kind)(config.select(brain), stack, bounds)
        for row in stream:
            _write(terminal(present(row)), flush=False)
        _write(b"")
    for name in cast("list[str]", extra.get("stale", [])):
        _diagnose(f"{name}: a writer kept the cache from refreshing; retry for newer claims")
    for item in cast("list[dict[str, object]]", extra.get("omitted", [])):
        msg = "text omitted: the file changed or became unreadable since the cache refreshed; run bf build"
        _diagnose(f"{item['brain']}: {item['file']}: {msg}")
    problems = cast("list[dict[str, object]]", extra.get("problems", []))
    for problem in problems:
        _diagnose(": ".join(str(problem[k]) for k in ("brain", "file", "error") if k in problem))
    if problems:
        raise typer.Exit(1)


@app.command("collect", rich_help_panel="Collect and automate")
def capture(
    job: Annotated[str, _Arg(metavar="JOB", help="Record-emitting job (emit: records) from bf.yaml.")],
    brain: RunOpt = "",
    since: SinceOpt = "",
    until: UntilOpt = "now",
    dry_run: DryOpt = False,
    allow_removal: RemovalOpt = False,
    reconcile: ReconcileOpt = False,
    input_file: Annotated[str, _Opt("--input", metavar="FILE", help="Validate FILE (or - for stdin) offline.")] = "",
) -> None:
    """Run one record-emitting job now, for a backfill or to debug a collector."""
    from bf import collect, config

    now, start, end = _window(since, until)
    store = config.execution(brain)
    settings = config.load(store).jobs.get(job)
    start = start or moment((now - timedelta(seconds=settings.lookback if settings else 86_400)).isoformat())
    if start >= end:
        raise typer.BadParameter("must be later than start; set --since for a historical window", param_hint="--until")
    fixture = None
    if input_file:
        limit = (settings.max_bytes if settings else 64 << 20) + 1
        try:
            if input_file == "-":
                fixture = sys.stdin.buffer.read(limit)
            else:
                with Path(input_file).expanduser().open("rb") as handle:
                    fixture = handle.read(limit)
        except OSError as error:
            raise typer.BadParameter(f"cannot read the file ({error.strerror})", param_hint="--input") from None
        if len(fixture) >= limit:
            raise typer.BadParameter("exceeds the job's max_bytes", param_hint="--input")
    kw: dict[str, Any] = {"start": start, "end": end, "dry_run": dry_run, "allow_removal": allow_removal}
    emit(collect.collect(store, job, **kw, reconcile=reconcile, clock=lambda: now, fixture=fixture, live=True))


@app.command("forget", rich_help_panel="Check and repair")
def forget_records(
    refs: Annotated[list[str], _Arg(metavar="REF...", help="SOURCE:ID refs or memories/SOURCE/SHA256.json.")],
    brain: BrainOpt = "",
    dry_run: DryOpt = False,
) -> None:
    """Remove collected records and keep tombstones so re-collection does not restore them."""
    from bf import config, records, storage

    targets: dict[str, dict[str, str]] = {}
    for ref in refs:
        if _surrogates(ref):
            raise typer.BadParameter("expected UTF-8 text", param_hint="REF")
        try:
            source, entry = records.target(ref)
        except Error as error:
            raise typer.BadParameter(str(error), param_hint="REF") from None
        targets.setdefault(source, {})[ref] = entry
    store = config.one(brain)
    sources = config.load(store).record_sources()
    for source in targets:
        if source not in sources and not _has(store, f"memories/{source}", directory=True):
            raise Error(f"unknown source {source}; check jobs in bf.yaml{suggest(source, sorted(sources))}")
    removed: list[dict[str, object]] = []
    with storage.writer(store, wait=120):
        for source, named in targets.items():
            removed.extend(records.forget(store, source, named, dry_run=dry_run))
    result: dict[str, object] = {"ok": True, "dry_run": dry_run, "records": removed}
    emit(result) if dry_run else _finish_indexed(store, result)


@app.command("run", rich_help_panel="Collect and automate")
def perform(
    arguments: Annotated[list[str] | None, _Arg(metavar="[JOB] [ARGS]...", help="Job name plus optional args.")] = None,
    hook: Annotated[str, _Opt(help="Run all enabled jobs triggered by hook EVENT.")] = "",
    brain: RunOpt = "",
    dry_run: DryOpt = False,
    forward: Annotated[bool, _Opt("--stdin", help="Pass stdin to job or prompt {{input}}.")] = False,
    allow_removal: RemovalOpt = False,
    reconcile: ReconcileOpt = False,
    since: SinceOpt = "",
    until: UntilOpt = "",
) -> None:
    """Run one configured job (records, prompt, script, or send) or every job matching --hook EVENT."""
    from bf import config, jobs

    values = tuple(arguments or ())
    if hook:
        if not re.fullmatch(SLUG, hook) or len(hook) > 64:
            raise typer.BadParameter("use lowercase words joined by hyphens, e.g. pre-push", param_hint="--hook")
        if since or until or allow_removal or reconcile:
            raise typer.BadParameter("cannot mix with --since/--until/--allow-removal/--reconcile", param_hint="--hook")
        names, args, start, end = (), values, "", ""
    elif not values:
        raise typer.BadParameter("name a job, or pass --hook EVENT", param_hint="JOB")
    elif not values[0].strip():
        raise typer.BadParameter("give a job name; an empty value names none", param_hint="JOB")
    elif _surrogates(values[0]):
        raise typer.BadParameter("expected UTF-8 text", param_hint="JOB")
    else:
        names, args = values[:1], values[1:]
        _, start, end = _window(since, until)
    store = config.execution(brain)
    cfg_obj = config.load(store)
    if hook:
        has_hook = any(j.enabled and any(t.hook == hook for t in j.triggers) for j in cfg_obj.jobs.values())
        result = jobs.run_hook(store, hook, args=args, stdin=_input() if forward or has_hook else b"", dry_run=dry_run)
        _finish(result, failed=not result["ok"])
        return
    job_cfg = jobs.enabled(cfg_obj.jobs, names[0])
    if args and (job_cfg.prompt or job_cfg.emit == "records" or (job_cfg.send and not job_cfg.run)):
        raise typer.BadParameter("only script run: jobs accept extra positional arguments", param_hint="ARGS")
    kw: dict[str, Any] = {"args": args, "dry_run": dry_run, "allow_removal": allow_removal, "reconcile": reconcile}
    result = jobs.run_job(store, names[0], stdin=_input() if forward else b"", **kw, since=start, until=end)
    delivery = result.get("delivery")
    failed = (
        result.get("ok") is False
        or result.get("status") == "failed"
        or (isinstance(delivery, dict) and not delivery.get("ok"))
    )
    _finish(result, failed=failed)


@app.command("wake", rich_help_panel="Collect and automate")
def schedule_wake(
    job: Annotated[str, _Opt("--job", help="Enabled job to wake; optional with --run.")] = "",
    in_delay: Annotated[str, _Opt("--in", metavar="DURATION", help="Delay before wake, e.g. 10m or 600.")] = "",
    at: Annotated[str, _Opt("--at", metavar="ISO", help="Explicit UTC/offset wake instant.")] = "",
    reason: Annotated[str, _Opt("--reason", help="Why this wake was requested.")] = "",
    run_id: RunIdOpt = "",
    key: KeyOpt = "",
    brain: RunOpt = "",
) -> None:
    """Queue a bounded follow-up wake for an enabled job."""
    from bf import config, jobs

    if in_delay and at:
        raise typer.BadParameter("pass either --in or --at, not both", param_hint="--in")
    secs = 0
    if in_delay:
        try:
            secs = int(in_delay) if in_delay.isdigit() else parse_duration(in_delay)
        except ValueError as error:
            raise typer.BadParameter(str(error), param_hint="--in") from None
        if secs <= 0:
            raise typer.BadParameter("delay must be positive", param_hint="--in")
    due_at = _option("--at", lambda v: moment(v, datetime.now(UTC)), at) if at else ""
    if not job and not run_id:
        raise typer.BadParameter("name a --job or pass --run RUN_ID", param_hint="--job")
    kw: dict[str, Any] = {"job": job, "in_seconds": secs, "at": due_at, "reason_text": reason, "run_id": run_id}
    emit(jobs.wake_command(config.execution(brain), **kw, idempotency_key=key))


@app.command("post", rich_help_panel="Collect and automate")
def post_message(
    text: Annotated[str, _Arg(metavar="[TEXT]", help="Message text; omit and pass --stdin for stdin.")] = "",
    to: Annotated[str, _Opt("--to", metavar="JOB", help="send: job; defaults to active/defaults post.")] = "",
    thread: Annotated[str, _Opt("--thread", help="Conversation or thread ID.")] = "",
    target: Annotated[str, _Opt("--target", help="Target/conversation fallback when --thread omitted.")] = "",
    reply_to: Annotated[str, _Opt("--reply-to", help="Provider message ID being replied to.")] = "",
    run_id: RunIdOpt = "",
    key: KeyOpt = "",
    forward: Annotated[bool, _Opt("--stdin", help="Read UTF-8 message text from stdin.")] = False,
    brain: RunOpt = "",
) -> None:
    """Queue and deliver one outbound message through a `send:` job."""
    from bf import config, jobs

    if bool(text) == forward:
        raise typer.BadParameter("pass either TEXT or --stdin", param_hint="--stdin")
    try:
        body = _input().decode("utf-8") if forward else text
    except UnicodeError as error:
        raise Error("post input must be UTF-8 text") from error
    kw: dict[str, Any] = {"to": to, "target": target, "thread": thread, "reply_to": reply_to, "run_id": run_id}
    result = jobs.post_command(config.execution(brain), body, **kw, idempotency_key=key)
    _finish(result, failed=not result["ok"])


action_app = typer.Typer(**_TYPER, help="Create and update authored action notes.")
app.add_typer(action_app, name="action", rich_help_panel="Collect and automate")


def _resolve_action(store: Store, target: str) -> str:
    from bf.storage import relative

    slug_body = ACTION_SLUG.lstrip("^")
    dated = rf"\d{{4}}-\d{{2}}-\d{{2}}_{slug_body}"
    if target.endswith("/ACTION.md") and re.fullmatch(r"actions/[^/]+/ACTION\.md", target):
        relative(target)
        candidate = target
    elif target.startswith("actions/") and "/" not in target.removeprefix("actions/"):
        candidate = f"{target}/ACTION.md"
        relative(candidate)
    elif re.fullmatch(dated, target):
        candidate = f"actions/{target}/ACTION.md"
    elif re.fullmatch(ACTION_SLUG, target) and len(target) <= 64 and _has(store, "actions", directory=True):
        slug_pat = re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}}_{re.escape(target)}(?:-[0-9a-f]{{8,12}})?")
        matches = sorted(
            f"actions/{entry}/ACTION.md"
            for entry in store.entries("actions")
            if slug_pat.fullmatch(entry) and _has(store, f"actions/{entry}/ACTION.md")
        )
        if not matches:
            raise NotFoundError(f"no action matches {target}")
        if len(matches) > 1:
            raise Error(f"multiple actions match {target}; pass the dated folder name: {', '.join(matches[:5])}")
        return matches[0]
    else:
        raise typer.BadParameter("expected an action slug, folder name or actions/FOLDER/ACTION.md", param_hint="SLUG")
    if not _has(store, candidate):
        raise NotFoundError(f"{candidate} does not exist")
    return candidate


@action_app.command("new")
def action_new_cmd(
    slug: Annotated[str, _Arg(metavar="SLUG", help="Lowercase slug (e.g. rotate-keys).")],
    title: Annotated[str, _Opt("--title", help="Single-line heading; defaults to slug.")] = "",
    summary: Annotated[str, _Opt("--summary", help="Optional one-line OKF summary.")] = "",
    unique: Annotated[bool, _Opt("--unique", help="Append random 12-hex suffix to avoid collisions.")] = False,
    run_id: RunIdOpt = "",
    brain: BrainOpt = "",
) -> None:
    """Create a validated draft action note at actions/YYYY-MM-DD_SLUG/ACTION.md and refresh the cache."""
    from bf import config, jobs

    if not re.fullmatch(ACTION_SLUG, slug) or len(slug) > 64:
        raise typer.BadParameter(
            "use lowercase letters or digits joined by single hyphens (<=64 chars)", param_hint="SLUG"
        )
    store = config.one(brain)
    t_line, s_line = (
        _single_line("--title", title) if title else "",
        _single_line("--summary", summary) if summary else "",
    )
    result = jobs.action_new(store, slug, title=t_line, summary=s_line, unique=unique, run_id=run_id)
    emit(result) if result.get("reused") else _finish_indexed(store, result)


@action_app.command("status")
def action_status(
    slug: Annotated[str, _Arg(metavar="SLUG", help="Slug, folder name, or actions/.../ACTION.md.")],
    status: Annotated[
        Literal["draft", "stable", "deprecated"], _Arg(metavar="STATUS", help="draft|stable|deprecated.")
    ],
    brain: BrainOpt = "",
) -> None:
    """Update an existing action note's OKF status and updated date atomically, then refresh the search cache."""
    import yaml

    from bf import config, markdown, storage, validate

    store = config.one(brain)
    cfg_obj, today = config.load(store), datetime.now(UTC).astimezone().date().isoformat()
    with storage.writer(store, wait=120):
        path = _resolve_action(store, slug)
        parsed = markdown.parse(path, store.read(path, MAX_NOTE))
        attributes = {"type": "action", **parsed.attributes, "status": status, "updated": today}
        body = "".join(markdown.lines(parsed.text)[parsed.offset :])
        front = yaml.safe_dump(attributes, sort_keys=False, allow_unicode=True, width=1000)
        raw = f"---\n{front}---\n{body}".encode()
        validate.validate_action_markdown(store, cfg_obj, path, raw)
        store.write(path, raw)
    _finish_indexed(store, {"ok": True, "action": path, "status": status})


@app.command("queue", rich_help_panel="Collect and automate")
def queue_command(
    key: Annotated[str, _Arg(metavar="[ID]", help="Inspect, retry, or cancel one entry.")] = "",
    brain: RunOpt = "",
    retry: Annotated[bool, _Opt("--retry", help="Retry unresolved work; may repeat effects.")] = False,
    cancel: Annotated[bool, _Opt("--cancel", help="Cancel pending or unresolved work.")] = False,
    limit: Annotated[int, _Opt(min=1, max=10000, help="Maximum listed wakes, runs and deliveries.")] = 50,
) -> None:
    """Inspect durable work or explicitly resolve an uncertain attempt; listing contains no message bodies."""
    from bf import config, jobs, runtime

    if (retry and cancel) or ((retry or cancel) and not key):
        raise typer.BadParameter("give an ID and exactly one of --retry or --cancel", param_hint="ID")
    queue = runtime.Queue(config.execution(brain))
    if retry or cancel:
        queue.retry(key, cancel=cancel)
        saved = queue.inspect(key)
        is_delivery = "send_job" in saved
        if retry and is_delivery:
            jobs.deliver_due(queue.brain, key=key)
            saved = queue.inspect(key)
        _finish({"id": key, "status": saved["state"]}, failed=retry and is_delivery and saved["state"] != "delivered")
    elif key:
        row = queue.inspect(key)
        for name in ("payload", "command", "receipt"):
            if row.get(name):
                row[name] = decode(str(row[name]))
        emit(row)
    else:
        emit(queue.report(limit=limit))


@app.command("update", rich_help_panel="Collect and automate")
def refresh(brain: RunOpt = "", job: JobOpt = None, dry_run: DryOpt = False) -> None:
    """Run due record jobs, scheduled/event jobs, wakes and deliveries of one brain, then refresh its cache."""
    from bf import config, update

    result = update.update(config.execution(brain), dry_run=dry_run, jobs=tuple(job or ()))
    _finish(result, failed=not result["ok"])


@app.command("watch", rich_help_panel="Collect and automate")
def monitor(
    brain: RunOpt = "",
    job: JobOpt = None,
    interval: Annotated[int | None, _Opt(min=5, max=86400, help="Seconds between cycles (default 60).")] = None,
    poll_interval: Annotated[float | None, _Opt(min=0.2, max=60, help="Seconds between reads (default 2).")] = None,
    notify: Annotated[Literal["off", "failure", "success", "all"] | None, _Opt(help="Desktop notifications.")] = None,
    json_output: Annotated[bool, _Opt("--json", help="Stream JSON snapshots instead of dashboard.")] = False,
) -> None:
    """Watch one brain, run daemon streams and due jobs until you quit; this can contact providers."""
    from bf import config, watch

    kw: dict[str, Any] = {"jobs": tuple(job or ()), "interval": interval, "poll_interval": poll_interval}
    watch.watch(config.execution(brain), **kw, notifications=notify, json_output=json_output)


@app.command("status", rich_help_panel="Check and repair")
def report(
    brain: BrainOpt = "",
    check: Annotated[bool, _Opt("--check", help="Exit 1 on problems or unhealthy jobs/queue.")] = False,
    watch: Annotated[bool, _Opt("--watch", help="Observe local history in a dashboard offline.")] = False,
) -> None:
    """Show the brain's cache, notes, jobs, record sources, queue summary, errors and usage."""
    from bf import config, health

    if watch:
        if check:
            raise typer.BadParameter("cannot be combined with --check", param_hint="--watch")
        from bf.watch import watch as observe

        observe(config.one(brain), observe=True)
        return
    result = health.report(config.select(brain))
    _finish(result, failed=check and not result["healthy"])


@app.command("doctor", rich_help_panel="Check and repair")
def doctor_command(brain: BrainOpt = "") -> None:
    """Verify configured job executables, required binaries, env vars (presence only), scripts and prompts."""
    from bf import config, health

    result = health.doctor(config.one(brain))
    _finish(result, failed=not result["healthy"])


@app.command("validate", rich_help_panel="Check and repair")
def check(brain: BrainOpt = "") -> None:
    """Check OKF projects, concepts and ACTION.md notes, links, record files and job configs; exit 1 on problems."""
    from bf import config, validate

    result = validate.validate(config.one(brain))
    _finish(result, failed=not result["valid"])


@app.command("eval", rich_help_panel="Check and repair")
def acceptance(
    brain: BrainOpt = "",
    path: Annotated[str, _Opt(help="Suite file or directory relative to brain.")] = "evals",
    baseline: Annotated[Path | None, _Opt(dir_okay=False, help="Saved bf eval reply for comparison.")] = None,
) -> None:
    """Run the brain's retrieval cases; exit 1 when one fails."""
    from bf import config, evaluate, storage

    path = path.rstrip("/") or path
    _option("--path", storage.relative, path)
    previous = _option("--baseline", evaluate.load_baseline, str(baseline)) if baseline else None
    result = evaluate.evaluate(config.one(brain), path, previous)
    _finish(result, failed=not result["passed"])


@app.command("build", rich_help_panel="Check and repair")
def rebuild(
    brain: BrainOpt = "",
    reproject: Annotated[str, _Opt(metavar="JOB", help="Re-validate stored records for job or source.")] = "",
    dry_run: DryOpt = False,
) -> None:
    """Recover interrupted record writes and rebuild the disposable search cache; exit 1 when files were skipped."""
    from bf import collect, config, index

    if reproject and not re.fullmatch(NAME, reproject):
        raise typer.BadParameter("expected a job or source name from bf.yaml", param_hint="--reproject")
    if dry_run and not reproject:
        raise typer.BadParameter("previews a reprojection; add --reproject JOB", param_hint="--dry-run")
    store = config.one(brain)
    if not reproject:
        result = index.refresh(store, full=True)
        _finish(result, failed=bool(result["skipped"]))
        return
    result = collect.reproject(store, reproject, dry_run=dry_run)
    failed = bool(result["failed"])
    _finish(result, failed=failed) if dry_run else _finish_indexed(store, result, failed=failed)


_INPUT, _INPUT_WAIT = 1 << 20, 10.0


def _input() -> bytes:
    """Standard input when piped or redirected; a terminal gives none."""
    stream = sys.stdin
    if stream is None or stream.isatty():
        return b""
    try:
        descriptor = stream.fileno()
    except AttributeError, OSError, io.UnsupportedOperation:
        data = stream.buffer.read(_INPUT + 1)
    else:
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            data = os.read(descriptor, _INPUT + 1) if stat.S_ISREG(mode) or stat.S_ISCHR(mode) else b""
        else:
            buffer, deadline = bytearray(), time.monotonic() + _INPUT_WAIT
            with selectors.DefaultSelector() as selector:
                selector.register(descriptor, selectors.EVENT_READ)
                while len(buffer) <= _INPUT:
                    if (remaining := deadline - time.monotonic()) <= 0:
                        msg = "piped input did not end within 10 seconds; close it, or run the command from a terminal"
                        raise Error(msg)
                    if selector.select(remaining):
                        if not (chunk := os.read(descriptor, 65536)):
                            break
                        buffer.extend(chunk)
            data = bytes(buffer)
    if len(data) > _INPUT:
        raise Error("piped input exceeds 1 MiB")
    return data


_GIVEN = {
    "--at": "give a timestamp; an empty value names none",
    "--brain": "give a brain name or path; an empty value would select another brain",
    "--hook": "give an event name; an empty value would run the first argument as a job",
    "--in": "give a delay such as 10m or 600; an empty value names none",
    "--input": "give a file, or - for stdin; an empty value would run the job",
    "--job": "give a job name; an empty value names none",
    "--key": "give an idempotency key; an empty value names none",
    "--observed-since": "give a cutoff time or period; an empty value names none",
    "--reproject": "give a job or source name; an empty value would rebuild the cache instead",
    "--run": "give an active run ID; an empty value names none",
    "--since": "give a start time; an empty value names none",
    "--summary": "give a summary; an empty value names none",
    "--title": "give a title; an empty value names none",
    "--to": "give a send job name; an empty value names none",
    "JOB": "give a job name from bf.yaml",
    "PATH": "give a directory; an empty value would name the working directory",
    "SLUG": "give an action slug or path",
    "TARGET": "give a registered brain name or path",
}


class _Command(TyperCommand):
    """Single-valued options appear at most once and `_GIVEN` values are never empty."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        try:
            violation = None if ctx.resilient_parsing else self._violation(ctx, args)
            remaining = super().parse_args(ctx, args)
            if violation:
                raise violation
            return remaining
        except typer.TyperException as error:
            if getattr(error, "ctx", ctx) is not None:
                raise
            raise typer.BadParameter(error.format_message(), ctx) from None

    def _violation(self, ctx: Any, args: list[str]) -> typer.BadParameter | None:
        given, _, order = self.make_parser(ctx).parse_args(args=list(args))
        seen: set[str | None] = set()
        for param in order:
            if isinstance(param, TyperOption) and not (param.multiple or param.count or param.is_flag):
                if param.name in seen:
                    return typer.BadParameter("give it once; repeated values do not combine", ctx, param)
                seen.add(param.name)
        for param in self.params:
            value, reason = given.get(param.name), _GIVEN.get(_param_hint(param))
            values = value if isinstance(value, list) else [value]
            if any(isinstance(item, str) and not item.isascii() and _surrogates(item) for item in values):
                return typer.BadParameter("expected UTF-8 text", ctx, param_hint=_param_hint(param))
            if reason and any(isinstance(item, str) and not item.strip() for item in values):
                return typer.BadParameter(reason, ctx, param_hint=_param_hint(param))
        return None

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except InputError as error:
            raise typer.BadParameter(str(error), ctx, param_hint=_ARGUMENTS.get(error.argument)) from None


def _surrogates(value: str) -> bool:
    try:
        value.encode()
    except UnicodeEncodeError:
        return True
    return False


def _cancel(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _fail(message: str, code: int, error: Exception | None = None) -> NoReturn:
    try:
        sys.stdout.flush()
    except (OSError, ValueError) as refused:
        with suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        if isinstance(error, OSError) and isinstance(refused, OSError) and refused.errno == error.errno:
            suffix = f" ({error.strerror})" if error.strerror else ""
            message = f"cannot write the reply to standard output{suffix}"
    _diagnose(message)
    sys.exit(code)


def main() -> None:
    if sys.stdout is None:
        _diagnose("standard output is closed; redirect it to a file or /dev/null")
        sys.exit(1)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)}
    for signum, handler in previous.items():
        if signum == signal.SIGTERM or handler is not signal.SIG_IGN:
            signal.signal(signum, _cancel)
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
    except ValidationError as error:
        _fail("invalid input: " + explain(error), 2)
    except FAILURES as error:
        _fail(failure(error), 1, error)
    finally:
        for signum, handler in previous.items():
            if handler is not None:
                signal.signal(signum, handler)


for _command in (*app.registered_commands, *action_app.registered_commands, *skill_app.registered_commands):
    _command.cls = _Command
