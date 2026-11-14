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
from collections.abc import Callable
from contextlib import ExitStack, suppress
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Annotated, Any, Literal, NoReturn, cast
from unicodedata import combining, normalize

import typer
import yaml
from pydantic import ValidationError
from typer.completion import completion_init
from typer.core import TyperCommand, TyperGroup, TyperOption

from bf import __version__, collect, config, health, index, jobs, pages, records, retrieve, storage, watch
from bf import models as _m
from bf.evaluate import evaluate, load_baseline
from bf.guide import read_guide, render_index
from bf.install import install as install_skill
from bf.markdown import lines, parse
from bf.runtime import Queue
from bf.schemas import document
from bf.update import update as run_update
from bf.validate import validate, validate_action_markdown

_Opt, _Arg = typer.Option, typer.Argument


def _diagnose(message: str) -> None:
    typer.echo("bf: " + _m.printable(message), err=True)


def _bad(message: str, hint: str | Any = None) -> NoReturn:
    raise typer.BadParameter(message, param_hint=hint) from None


def _param_hint(param: Any) -> str:
    if param is None or isinstance(param, (str, list, tuple)):
        return ("" if param is None else param if isinstance(param, str) else " / ".join(param)).replace("'", "")
    if getattr(param, "param_type_name", "") == "option":
        return " / ".join([*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())]).replace("'", "")
    return str(getattr(param, "metavar", None) or (getattr(param, "name", None) or "").upper()).replace("'", "")


class _Group(TyperGroup):
    def _run(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except typer.TyperException as error:
            if error.exit_code != 2 or type(error).__name__ == "NoArgsIsHelpError":
                raise
            ctx, hint, msg = getattr(error, "ctx", None), "", error.format_message()
            if isinstance(error, typer.BadParameter) and error.message:
                hint, msg = _param_hint(error.param_hint or error.param), error.message.removeprefix("Value error, ")
            msg = msg[:1].lower() + msg[1:] if not hint and msg[1:2].islower() else msg
            reason = f"{hint}: {msg}" if hint else msg
            _diagnose(f"invalid input: {reason.rstrip('.')}" + (f" (see {ctx.command_path} -h)" if ctx else ""))
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
SinceOpt = Annotated[str, _Opt(help="Window start: 7d, yesterday, YYYY-MM-DD or ISO.")]
UntilOpt = Annotated[str, _Opt(help="Window end.")]
RemovalOpt = Annotated[bool, _Opt("--allow-removal", help="Allow empty or shrunk snapshot.")]
ReconcileOpt = Annotated[bool, _Opt("--reconcile", help="Backfill reconcile window.")]
RunIdOpt = Annotated[str, _Opt("--run", help="Active run ID from {{run}}.")]
KeyOpt = Annotated[str, _Opt("--key", help="Idempotency key.")]
OffsetOpt = Annotated[int, _Opt(min=0, max=_m.MAX_OFFSET, help="Continue at next_offset.")]
ScopeOpt = Annotated[str, _Opt(help="Prefix, period, identity, or tag.")]
TopicArg = Annotated[str | None, _Arg(metavar="[TOPIC]", show_default=False, help="Guide topic.")]
QueryArg = Annotated[str, _Arg(metavar="QUERY", help="Words or identity.")]
LimitOpt = Annotated[int, _Opt(min=1, max=50, help="Max results.")]
RefArg = Annotated[str, _Arg(metavar="REF", help="Page, note, #section, source:id, or identity.")]
RelOpt = Annotated[str, _Opt("--rel", help="Backlinks via relation.")]
ExpOpt = Annotated[Literal["edges", "identities", "items"], _Opt(help="edges, identities, or items JSONL.")]
ObsOpt = Annotated[str, _Opt("--observed-since", help="Items cutoff time.")]
RefsArg = Annotated[list[str], _Arg(metavar="REF...", help="SOURCE:ID or record paths.")]
StdinOpt = Annotated[bool, _Opt("--stdin", help="Read UTF-8 text or job input from stdin.")]
SlugActArg = Annotated[str, _Arg(metavar="SLUG", help="Slug, folder, or ACTION.md path.")]
StatusArg = Annotated[Literal["draft", "stable", "deprecated"], _Arg(metavar="STATUS", help="Note status.")]
EvalOpt = Annotated[str, _Opt(help="Suite path inside brain.")]
BaseOpt = Annotated[Path | None, _Opt(dir_okay=False, help="Saved baseline JSON.")]
ReprojOpt = Annotated[str, _Opt(metavar="JOB", help="Re-validate job/source records.")]
CheckExitOpt = Annotated[bool, _Opt("--check", help="Exit 1 on problems.")]
WatchOpt = Annotated[bool, _Opt("--watch", help="Observe offline dashboard.")]
FOLDERS, OPTIONAL = ("projects", "actions"), ("memories", "assets", "sensors", "routines", "skills")
STARTER = ("AGENTS.md", ".gitignore", "concepts/index.md", "concepts/welcome.md", "evals/retrieval.yaml")
SCHEMAS = f"https://raw.githubusercontent.com/fmind/brain-framework/v{__version__}/docs/"
_P_SETUP, _P_READ, _P_AUTO, _P_CHECK = "Set up", "Find and read", "Collect and automate", "Check and repair"


def emit(value: object, *, reply: bool = True) -> None:
    _write(_m.terminal(_m.present(value) if reply else value))


def _finish(value: dict[str, Any], *, failed: bool = False, store: storage.Store | None = None) -> None:
    if store is not None:
        value["index"] = (refreshed := index.refresh(store, recover=True))
        failed = failed or bool(refreshed.get("skipped"))
    emit(value)
    if failed:
        raise typer.Exit(1)


def _store(brain: str = "", mode: str = "select") -> storage.Store:
    return cast("storage.Store", getattr(config, mode)(brain))


def _write(data: bytes, *, flush: bool = True) -> None:
    try:
        sys.stdout.buffer.write(data)
        if flush:
            sys.stdout.flush()
    except BrokenPipeError:
        raise
    except OSError as error:
        tail = f" ({error.strerror})" if error.strerror else ""
        raise _m.Error(f"cannot write the reply to standard output{tail}") from error


def _option[T](name: str, parse: Callable[[str], T], value: str) -> T:
    try:
        return parse(value)
    except (_m.Error, ValueError) as error:
        _bad(str(error), name)


def _has(store: storage.Store, path: str, *, directory: bool = False) -> bool:
    with suppress(_m.Error, OSError):
        mode = store.mode(path)
        return stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    return False


def _name(value: str) -> str:
    if not re.fullmatch(_m.NAME, value):
        _bad("choose a brain name of 1-64 chars: lowercase letter, then lowercase letters, digits or hyphens", "--name")
    return value


def _window(since: str, until: str) -> tuple[datetime, str, str]:
    now = datetime.now(UTC)
    start = _option("--since", lambda v: _m.moment(v, now), since) if since else ""
    end = _option("--until", lambda v: _m.moment(v, now), until) if until else ""
    if start and end and start >= end:
        _bad("must be earlier than --until", "--since")
    return now, start, end


@app.callback()
def root(version: Annotated[bool, _Opt("--version", is_eager=True, help="Show version and exit.")] = False) -> None:
    if version:
        _write(f"{__version__}\n".encode())
        raise typer.Exit


@app.command("init", rich_help_panel=_P_SETUP)
def initialize(
    path: Annotated[Path, _Arg(file_okay=False, metavar="PATH", help="Empty brain directory.")],
    name: Annotated[str | None, _Opt(help="Brain link namespace.")] = None,
    full: Annotated[bool, _Opt("--full", help="Create optional folders.")] = False,
) -> None:
    """Create a brain at PATH (recommended: ~/brain); no global registration is needed."""
    path = storage.expand(path)
    if name is None:
        folded = "".join(c for c in normalize("NFKD", path.resolve().name.lower()) if not combining(c))
        name = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
        if any(c.isalnum() and not c.isascii() for c in folded) or not re.fullmatch(_m.NAME, name):
            _bad("the directory name cannot form a brain name; pass --name NAME", "PATH")
    else:
        name = _name(name)
    cfg_obj, vacant_err = _m.starter_config(name), "initialization requires a new, empty or freshly cloned directory"
    if path.exists() and any(e.name != ".git" for e in path.iterdir()):
        raise _m.Error(vacant_err)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = storage.Store(path)
    with storage.writer(store):
        if any(e.name not in {".git", ".bf"} for e in store.root.iterdir()):
            raise _m.Error(vacant_err)
        hdr = f"# https://fmind.github.io/brain-framework/\n# yaml-language-server: $schema={SCHEMAS}bf.schema.json\n"
        body = yaml.safe_dump(cfg_obj.model_dump(by_alias=True, exclude_defaults=True), sort_keys=False, width=1000)
        store.write("bf.yaml", (hdr + body).encode())
        for target in STARTER:
            text = (files("bf") / "starter" / target.lstrip(".")).read_text(encoding="utf-8")
            if target.endswith(".yaml"):
                text = Template(text).substitute(name=name, version=_m.FORMAT, schemas=SCHEMAS)
            store.write(target, text.encode())
        for directory in FOLDERS + (OPTIONAL if full else ()):
            store.write(f"{directory}/.gitkeep", b"")
    emit({"created": str(store.root), "brain": name})


@app.command("register", rich_help_panel=_P_SETUP)
def enroll(
    path: Annotated[Path, _Arg(metavar="PATH", show_default=False, help="Brain directory; defaults to cwd.")] = Path(),
    name: Annotated[str | None, _Opt("--name", help="Registry alias.")] = None,
    default: Annotated[bool, _Opt("--default", help="Select by default.")] = False,
) -> None:
    """Register an existing brain to select it by name outside its folder; never runs its jobs."""
    alias, path = "" if name is None else _name(name), storage.expand(path)
    if not path.is_dir():
        raise _m.Error("PATH is not an existing directory; pass the brain's directory")
    if not (path / "bf.yaml").is_file():
        raise _m.Error("PATH has no bf.yaml; run bf init PATH first, or pass the brain's root directory")
    emit(config.register(storage.Store(path), name=alias, default=default))


@app.command("unregister", rich_help_panel=_P_SETUP)
def unenroll(target: Annotated[str, _Arg(metavar="TARGET", help="Registered brain name or path.")]) -> None:
    """Remove a brain registration from the user registry; changes no files inside the brain."""
    emit(config.unregister(target))


@app.command("mcp", rich_help_panel=_P_SETUP)
def serve(brain: BrainOpt = "") -> None:
    """Serve search and read over MCP stdio for agents that prefer tools to the CLI; needs the mcp extra."""
    try:
        from bf.mcp import server
    except ModuleNotFoundError as error:
        msg = "bf mcp needs the mcp extra; install it with uv tool install --python 3.14 'brain-framework[mcp]'"
        raise _m.Error(msg) from error
    if sys.stdin is None:
        raise _m.Error("standard input is closed; an MCP host sends its requests through it")
    tools = server(lambda: _store(brain))
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        if signum != signal.SIGHUP or signal.getsignal(signum) is not signal.SIG_IGN:
            signal.signal(signum, lambda _s, _f: os._exit(130))
    tools.run()


skill_app = typer.Typer(**_TYPER, help="Install and verify the packaged bf skill.")
app.add_typer(skill_app, name="skill", rich_help_panel=_P_SETUP)


@skill_app.command("install")
def skill_install(
    brain: BrainOpt = "",
    global_: Annotated[bool, _Opt("--global", help="Install in ~/.agents/skills/bf.")] = False,
    check: Annotated[bool, _Opt("--check", help="Check status only.")] = False,
    force: Annotated[bool, _Opt("--force", help="Replace edited files.")] = False,
) -> None:
    """Install the packaged bf skill into one brain (and link .agents/.claude) or globally with --global."""
    if global_ and brain:
        _bad("cannot be combined with --global", "--brain")
    result = install_skill(None if global_ else _store(brain, "one"), global_=global_, check=check, force=force)
    _finish(result, failed=not result["ok"])


@app.command("guide", rich_help_panel=_P_SETUP)
def guide_command(topic: TopicArg = None) -> None:
    """Print a Brain Framework guide or the guide topic directory as Markdown."""
    _write((render_index() if topic is None else read_guide(topic)).encode("utf-8"))


@app.command("schema", rich_help_panel=_P_SETUP)
def schema(kind: Annotated[_m.SchemaKind, _Opt(help="JSON Schema kind.")] = "brain") -> None:
    """Print a configuration or reply JSON Schema; defaults to bf.yaml. No brain selection or network access."""
    emit(document(kind), reply=False)


@app.command("search", rich_help_panel=_P_READ)
def find(
    query: QueryArg, brain: BrainOpt = "", scope: ScopeOpt = "", limit: LimitOpt = 10, offset: OffsetOpt = 0
) -> None:
    """Search notes and records; results carry refs for bf read."""
    request = pages.query(query, scope, limit=limit, offset=offset)
    emit(retrieve.search(_store(brain), request))


@app.command("read", rich_help_panel=_P_READ)
def exact(ref: RefArg = "", brain: BrainOpt = "", rel: RelOpt = "", offset: OffsetOpt = 0) -> None:
    """Read the home page, another page, a note, a note section, a record or an identity with its backlinks."""
    pages.readable(ref, rel)
    emit(retrieve.read(_store(brain), ref, rel=rel, offset=offset))


@app.command("export", rich_help_panel=_P_READ)
def export(kind: ExpOpt = "edges", brain: BrainOpt = "", scope: ScopeOpt = "", observed_since: ObsOpt = "") -> None:
    """Print the graph of the selected brain as JSON Lines from its cache; exit 1 when evidence was skipped."""
    if observed_since and kind != "items":
        _bad("applies only to --kind items", "--observed-since")
    obs = _option("--observed-since", _m.moment, observed_since) if observed_since else ""
    p = pages.bounds(scope)
    b = _m.Bounds(since=p.since, until=p.until, observed_since=obs, undated=p.undated, prefix=p.prefix, target=p.target)
    with ExitStack() as stack:
        stream, extra = getattr(retrieve, kind)(_store(brain), stack, b)
        for row in stream:
            _write(_m.terminal(_m.present(row)), flush=False)
        _write(b"")
    for name in cast("list[str]", extra.get("stale", [])):
        _diagnose(f"{name}: a writer kept the cache from refreshing; retry for newer claims")
    msg = "text omitted: the file changed or became unreadable since the cache refreshed; run bf build"
    for item in cast("list[dict[str, object]]", extra.get("omitted", [])):
        _diagnose(f"{item['brain']}: {item['file']}: {msg}")
    for problem in (problems := cast("list[dict[str, object]]", extra.get("problems", []))):
        _diagnose(": ".join(str(problem[k]) for k in ("brain", "file", "error") if k in problem))
    if problems:
        raise typer.Exit(1)


def _read_fixture(job_cfg: Any, path: str, limit_msg: str, *, check_tty: bool = False) -> bytes:
    limit = job_cfg.max_bytes if job_cfg else 64 << 20
    try:
        if path == "-":
            if check_tty and (sys.stdin is None or sys.stdin.isatty()):
                _bad("give --input FILE or pipe data on stdin", "--input")
            data = _stdin_bytes(limit)
        else:
            with Path(path).expanduser().open("rb") as handle:
                data = handle.read(limit + 1)
    except OSError as error:
        _bad(f"cannot read the file ({error.strerror})", "--input")
    if len(data) > limit:
        _bad(limit_msg, "--input")
    return data


@app.command("collect", rich_help_panel=_P_AUTO)
def capture(
    job: Annotated[str, _Arg(metavar="JOB", help="Record-emitting job from bf.yaml.")],
    brain: RunOpt = "",
    since: SinceOpt = "",
    until: UntilOpt = "now",
    dry_run: DryOpt = False,
    allow_removal: RemovalOpt = False,
    reconcile: ReconcileOpt = False,
    input_file: Annotated[str, _Opt("--input", metavar="FILE", help="Validate FILE or - stdin offline.")] = "",
) -> None:
    """Run one record-emitting job now, for a backfill or to debug a collector."""
    now, start, end = _window(since, until)
    store = _store(brain, "execution")
    settings = config.load(store).jobs.get(job)
    start = start or _m.moment((now - timedelta(seconds=settings.lookback if settings else 86_400)).isoformat())
    if start >= end:
        _bad("must be later than start; set --since for a historical window", "--until")
    fixture = _read_fixture(settings, input_file, "exceeds the job's max_bytes") if input_file else None
    result = collect.collect(
        store,
        job,
        start=start,
        end=end,
        dry_run=dry_run,
        allow_removal=allow_removal,
        reconcile=reconcile,
        clock=lambda: now,
        fixture=fixture,
        live=True,
    )
    emit(result)


@app.command("ingest", rich_help_panel=_P_AUTO)
def ingest_cmd(
    source: Annotated[str, _Arg(metavar="SOURCE", help="Record source name.")],
    input_file: Annotated[str, _Opt("--input", metavar="FILE", help="JSON/JSONL file or - stdin.")] = "-",
    snapshot: Annotated[bool, _Opt("--snapshot", help="Complete catalog snapshot.")] = False,
    allow_removal: RemovalOpt = False,
    dry_run: DryOpt = False,
    brain: RunOpt = "",
) -> None:
    """Ingest JSON/JSONL records from a file or stdin into memories/ without touching runtime queues."""
    if not re.fullmatch(_m.NAME, source):
        _bad("choose 1-64 chars: lowercase letter, then lowercase letters, digits or hyphens", "SOURCE")
    store = _store(brain, "execution")
    raw = _read_fixture(config.load(store).jobs.get(source), input_file, "exceeds max_bytes", check_tty=True)
    emit(collect.ingest(store, source, raw, snapshot=snapshot, allow_removal=allow_removal, dry_run=dry_run))


@app.command("forget", rich_help_panel=_P_CHECK)
def forget_records(refs: RefsArg, brain: BrainOpt = "", dry_run: DryOpt = False) -> None:
    """Remove collected records and keep tombstones so re-collection does not restore them."""
    targets: dict[str, dict[str, str]] = {}
    for ref in refs:
        if _surrogates(ref):
            _bad("expected UTF-8 text", "REF")
        source, entry = _option("REF", records.target, ref)
        targets.setdefault(source, {})[ref] = entry
    store = _store(brain, "one")
    sources = config.load(store).record_sources()
    for source in targets:
        if source not in sources and not _has(store, f"memories/{source}", directory=True):
            raise _m.Error(f"unknown source {source}; check jobs in bf.yaml{_m.suggest(source, sorted(sources))}")
    with storage.writer(store, wait=120):
        removed = [r for src, named in targets.items() for r in records.forget(store, src, named, dry_run=dry_run)]
    result: dict[str, object] = {"ok": True, "dry_run": dry_run, "records": removed}
    emit(result) if dry_run else _finish(result, store=store)


@app.command("run", rich_help_panel=_P_AUTO)
def perform(
    arguments: Annotated[list[str] | None, _Arg(metavar="[JOB] [ARGS]...", help="Job name plus optional args.")] = None,
    hook: Annotated[str, _Opt(help="Run enabled hook EVENT jobs.")] = "",
    brain: RunOpt = "",
    dry_run: DryOpt = False,
    forward: StdinOpt = False,
    allow_removal: RemovalOpt = False,
    reconcile: ReconcileOpt = False,
    since: SinceOpt = "",
    until: UntilOpt = "",
) -> None:
    """Run one configured job (records, prompt, script, or send) or every job matching --hook EVENT."""
    values = tuple(arguments or ())
    if hook:
        if not re.fullmatch(_m.SLUG, hook) or len(hook) > 64:
            _bad("use lowercase words joined by hyphens, e.g. pre-push", "--hook")
        if since or until or allow_removal or reconcile:
            _bad("cannot mix with --since/--until/--allow-removal/--reconcile", "--hook")
        store = _store(brain, "execution")
        has_hook = any(j.enabled and any(t.hook == hook for t in j.triggers) for j in config.load(store).jobs.values())
        stdin = _input() if forward or has_hook else b""
        result = jobs.run_hook(store, hook, args=values, stdin=stdin, dry_run=dry_run)
        _finish(result, failed=not result["ok"])
        return
    if not values or not values[0].strip():
        _bad("name a job, or pass --hook EVENT" if not values else "give a job name; an empty value names none", "JOB")
    if _surrogates(values[0]):
        _bad("expected UTF-8 text", "JOB")
    _, start, end = _window(since, until)
    store = _store(brain, "execution")
    job_cfg = jobs.enabled(config.load(store).jobs, values[0])
    if values[1:] and (job_cfg.prompt or job_cfg.emit == "records" or (job_cfg.send and not job_cfg.run)):
        _bad("only script run: jobs accept extra positional arguments", "ARGS")
    result = jobs.run_job(
        store,
        values[0],
        args=values[1:],
        stdin=_input() if forward else b"",
        dry_run=dry_run,
        allow_removal=allow_removal,
        reconcile=reconcile,
        since=start,
        until=end,
    )
    delivery = result.get("delivery")
    bad = result.get("ok") is False or result.get("status") == "failed"
    _finish(result, failed=bad or (isinstance(delivery, dict) and not delivery.get("ok")))


@app.command("wake", rich_help_panel=_P_AUTO)
def schedule_wake(
    job: Annotated[str, _Opt("--job", help="Enabled job to wake.")] = "",
    in_delay: Annotated[str, _Opt("--in", metavar="DURATION", help="Delay e.g. 10m or 600.")] = "",
    at: Annotated[str, _Opt("--at", metavar="ISO", help="Explicit UTC/offset wake instant.")] = "",
    reason: Annotated[str, _Opt("--reason", help="Wake reason.")] = "",
    run_id: RunIdOpt = "",
    key: KeyOpt = "",
    brain: RunOpt = "",
) -> None:
    """Queue a bounded follow-up wake for an enabled job."""
    if in_delay and at:
        _bad("pass either --in or --at, not both", "--in")
    secs = _option("--in", lambda v: int(v) if v.isdigit() else _m.parse_duration(v), in_delay) if in_delay else 0
    if in_delay and secs <= 0:
        _bad("delay must be positive", "--in")
    due_at = _option("--at", lambda v: _m.moment(v, datetime.now(UTC)), at) if at else ""
    if not job and not run_id:
        _bad("name a --job or pass --run RUN_ID", "--job")
    res = jobs.wake_command(
        _store(brain, "execution"),
        job=job,
        in_seconds=secs,
        at=due_at,
        reason_text=reason,
        run_id=run_id,
        idempotency_key=key,
    )
    emit(res)


@app.command("post", rich_help_panel=_P_AUTO)
def post_message(
    text: Annotated[str, _Arg(metavar="[TEXT]", help="Message text.")] = "",
    to: Annotated[str, _Opt("--to", metavar="JOB", help="send: job.")] = "",
    thread: Annotated[str, _Opt("--thread", help="Conversation ID.")] = "",
    target: Annotated[str, _Opt("--target", help="Fallback conversation target.")] = "",
    reply_to: Annotated[str, _Opt("--reply-to", help="Reply message ID.")] = "",
    run_id: RunIdOpt = "",
    key: KeyOpt = "",
    forward: StdinOpt = False,
    brain: RunOpt = "",
) -> None:
    """Queue and deliver one outbound message through a `send:` job."""
    if bool(text) == forward:
        _bad("pass either TEXT or --stdin", "--stdin")
    try:
        body = _input().decode("utf-8") if forward else text
    except UnicodeError as error:
        raise _m.Error("post input must be UTF-8 text") from error
    result = jobs.post_command(
        _store(brain, "execution"),
        body,
        to=to,
        target=target,
        thread=thread,
        reply_to=reply_to,
        run_id=run_id,
        idempotency_key=key,
    )
    _finish(result, failed=not result["ok"])


action_app = typer.Typer(**_TYPER, help="Create and update authored action notes.")
app.add_typer(action_app, name="action", rich_help_panel=_P_AUTO)


def _resolve_action(store: storage.Store, target: str) -> str:
    dated = rf"\d{{4}}-\d{{2}}-\d{{2}}_{_m.ACTION_SLUG.lstrip('^')}"
    if target.endswith("/ACTION.md") and re.fullmatch(r"actions/[^/]+/ACTION\.md", target):
        storage.relative(candidate := target)
    elif target.startswith("actions/") and "/" not in target.removeprefix("actions/"):
        storage.relative(candidate := f"{target}/ACTION.md")
    elif re.fullmatch(dated, target):
        candidate = f"actions/{target}/ACTION.md"
    elif re.fullmatch(_m.ACTION_SLUG, target) and len(target) <= 64 and _has(store, "actions", directory=True):
        pat = re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}}_{re.escape(target)}(?:-[0-9a-f]{{8,12}})?")
        paths = [f"actions/{e}/ACTION.md" for e in store.entries("actions") if pat.fullmatch(e)]
        matches = sorted(p for p in paths if _has(store, p))
        if not matches:
            raise _m.NotFoundError(f"no action matches {target}")
        if len(matches) > 1:
            raise _m.Error(f"multiple actions match {target}; pass the dated folder name: {', '.join(matches[:5])}")
        return matches[0]
    else:
        _bad("expected an action slug, folder name or actions/FOLDER/ACTION.md", "SLUG")
    if not _has(store, candidate):
        raise _m.NotFoundError(f"{candidate} does not exist")
    return candidate


@action_app.command("new")
def action_new_cmd(
    slug: Annotated[str, _Arg(metavar="SLUG", help="Lowercase slug.")],
    title: Annotated[str, _Opt("--title", help="Single-line heading.")] = "",
    summary: Annotated[str, _Opt("--summary", help="One-line summary.")] = "",
    unique: Annotated[bool, _Opt("--unique", help="Append random 12-hex suffix.")] = False,
    run_id: RunIdOpt = "",
    brain: BrainOpt = "",
) -> None:
    """Create a validated draft action note at actions/YYYY-MM-DD_SLUG/ACTION.md and refresh the cache."""
    if not re.fullmatch(_m.ACTION_SLUG, slug) or len(slug) > 64:
        _bad("use lowercase letters or digits joined by single hyphens (<=64 chars)", "SLUG")
    cleaned: list[str] = []
    for opt, raw in (("--title", title), ("--summary", summary)):
        if raw:
            if not (stripped := raw.strip()) or any(c in stripped for c in "\r\n"):
                _bad("expected a non-empty single line", opt)
            try:
                raw = _m.clean(stripped)
            except ValueError:
                _bad("control and format characters are forbidden", opt)
        cleaned.append(raw)
    store = _store(brain, "one")
    result = jobs.action_new(store, slug, title=cleaned[0], summary=cleaned[1], unique=unique, run_id=run_id)
    emit(result) if result.get("reused") else _finish(result, store=store)


@action_app.command("status")
def action_status(slug: SlugActArg, status: StatusArg, brain: BrainOpt = "") -> None:
    """Update an existing action note's OKF status and updated date atomically, then refresh the search cache."""
    store = _store(brain, "one")
    cfg_obj, today = config.load(store), datetime.now(UTC).astimezone().date().isoformat()
    with storage.writer(store, wait=120):
        path = _resolve_action(store, slug)
        parsed = parse(path, store.read(path, _m.MAX_NOTE))
        attrs = {"type": "action", **parsed.attributes, "status": status, "updated": today}
        body = "".join(lines(parsed.text)[parsed.offset :])
        raw = f"---\n{yaml.safe_dump(attrs, sort_keys=False, allow_unicode=True, width=1000)}---\n{body}".encode()
        validate_action_markdown(store, cfg_obj, path, raw)
        store.write(path, raw)
    _finish({"ok": True, "action": path, "status": status}, store=store)


@app.command("queue", rich_help_panel=_P_AUTO)
def queue_command(
    key: Annotated[str, _Arg(metavar="[ID]", help="Entry ID.")] = "",
    brain: RunOpt = "",
    retry: Annotated[bool, _Opt("--retry", help="Retry unresolved work.")] = False,
    cancel: Annotated[bool, _Opt("--cancel", help="Cancel pending/unresolved work.")] = False,
    limit: Annotated[int, _Opt(min=1, max=10000, help="Max listed entries.")] = 50,
) -> None:
    """Inspect durable work or explicitly resolve an uncertain attempt; listing contains no message bodies."""
    if (retry and cancel) or ((retry or cancel) and not key):
        _bad("give an ID and exactly one of --retry or --cancel", "ID")
    queue = Queue(_store(brain, "execution"))
    if retry or cancel:
        queue.retry(key, cancel=cancel)
        is_delivery = "send_job" in (saved := queue.inspect(key))
        if retry and is_delivery:
            jobs.deliver_due(queue.brain, key=key)
            saved = queue.inspect(key)
        _finish({"id": key, "status": saved["state"]}, failed=retry and is_delivery and saved["state"] != "delivered")
    elif key:
        row = queue.inspect(key)
        row.update({k: _m.decode(str(row[k])) for k in ("payload", "command", "receipt") if row.get(k)})
        emit(row)
    else:
        emit(queue.report(limit=limit))


@app.command("update", rich_help_panel=_P_AUTO)
def refresh(brain: RunOpt = "", job: JobOpt = None, dry_run: DryOpt = False) -> None:
    """Run due record jobs, scheduled/event jobs, wakes and deliveries of one brain, then refresh its cache."""
    result = run_update(_store(brain, "execution"), dry_run=dry_run, jobs=tuple(job or ()))
    _finish(result, failed=not result["ok"])


@app.command("watch", rich_help_panel=_P_AUTO)
def monitor(
    brain: RunOpt = "",
    job: JobOpt = None,
    interval: Annotated[int | None, _Opt(min=5, max=86400, help="Cycle seconds (default 60).")] = None,
    poll_interval: Annotated[float | None, _Opt(min=0.2, max=60, help="Read poll seconds (default 2).")] = None,
    notify: Annotated[Literal["off", "failure", "success", "all"] | None, _Opt(help="Desktop notifications.")] = None,
    json_output: Annotated[bool, _Opt("--json", help="Stream JSON snapshots.")] = False,
) -> None:
    """Watch one brain, run daemon streams and due jobs until you quit; this can contact providers."""
    watch.watch(
        _store(brain, "execution"),
        jobs=tuple(job or ()),
        interval=interval,
        poll_interval=poll_interval,
        notifications=notify,
        json_output=json_output,
    )


@app.command("status", rich_help_panel=_P_CHECK)
def report(brain: BrainOpt = "", check: CheckExitOpt = False, watch_mode: WatchOpt = False) -> None:
    """Show the brain's cache, notes, jobs, record sources, queue summary, errors and usage."""
    if watch_mode:
        if check:
            _bad("cannot be combined with --check", "--watch")
        watch.watch(_store(brain, "one"), observe=True)
        return
    result = health.report(_store(brain))
    _finish(result, failed=check and not result["healthy"])


@app.command("doctor", rich_help_panel=_P_CHECK)
def doctor_command(brain: BrainOpt = "") -> None:
    """Verify configured job executables, required binaries, env vars (presence only), scripts and prompts."""
    result = health.doctor(_store(brain, "one"))
    _finish(result, failed=not result["healthy"])


@app.command("validate", rich_help_panel=_P_CHECK)
def check(brain: BrainOpt = "") -> None:
    """Check OKF projects, concepts and ACTION.md notes, links, record files and job configs; exit 1 on problems."""
    result = validate(_store(brain, "one"))
    _finish(result, failed=not result["valid"])


@app.command("eval", rich_help_panel=_P_CHECK)
def acceptance(brain: BrainOpt = "", path: EvalOpt = "evals", baseline: BaseOpt = None) -> None:
    """Run the brain's retrieval cases; exit 1 when one fails."""
    path = path.rstrip("/") or path
    _option("--path", storage.relative, path)
    previous = _option("--baseline", load_baseline, str(baseline)) if baseline else None
    result = evaluate(_store(brain, "one"), path, previous)
    _finish(result, failed=not result["passed"])


@app.command("build", rich_help_panel=_P_CHECK)
def rebuild(brain: BrainOpt = "", reproject: ReprojOpt = "", dry_run: DryOpt = False) -> None:
    """Recover interrupted record writes and rebuild the disposable search cache; exit 1 when files were skipped."""
    if reproject and not re.fullmatch(_m.NAME, reproject):
        _bad("expected a job or source name from bf.yaml", "--reproject")
    if dry_run and not reproject:
        _bad("previews a reprojection; add --reproject JOB", "--dry-run")
    store = _store(brain, "one")
    res = collect.reproject(store, reproject, dry_run=dry_run) if reproject else index.refresh(store, full=True)
    bad = bool(res["failed" if reproject else "skipped"])
    _finish(res, failed=bad, store=store if reproject and not dry_run else None)


_INPUT, _INPUT_WAIT = 1 << 20, 10.0


def _stdin_bytes(limit: int) -> bytes:
    if (stream := sys.stdin) is None or stream.isatty():
        return b""
    try:
        descriptor = stream.fileno()
    except AttributeError, OSError, io.UnsupportedOperation:
        return stream.buffer.read(limit + 1)
    mode = os.fstat(descriptor).st_mode
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        return os.read(descriptor, limit + 1) if stat.S_ISREG(mode) or stat.S_ISCHR(mode) else b""
    buffer, deadline = bytearray(), time.monotonic() + _INPUT_WAIT
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while len(buffer) <= limit and (rem := deadline - time.monotonic()) > 0 and selector.select(rem):
            if not (chunk := os.read(descriptor, 65536)):
                return bytes(buffer)
            buffer.extend(chunk)
        if len(buffer) <= limit:
            raise _m.Error("piped input did not end within 10 seconds; close it, or run the command from a terminal")
    return bytes(buffer)


def _input() -> bytes:
    if len(data := _stdin_bytes(_INPUT)) > _INPUT:
        raise _m.Error("piped input exceeds 1 MiB")
    return data


_EMPTY_HINTS = (
    "at:a timestamp|in:a delay such as 10m or 600|job:a job name|key:an idempotency key|"
    "observed-since:a cutoff time or period|run:an active run ID|since:a start time|"
    "summary:a summary|title:a title|to:a send job name"
)
_EXTRA_HINTS = (
    "--brain=a brain name or path; an empty value would select another brain|"
    "--hook=an event name; an empty value would run the first argument as a job|"
    "--input=a file, or - for stdin; an empty value would run the job|"
    "--reproject=a job or source name; an empty value would rebuild the cache instead|"
    "JOB=a job name from bf.yaml|PATH=a directory; an empty value would name the working directory|"
    "SLUG=an action slug or path|SOURCE=a source name|TARGET=a registered brain name or path"
)
_GIVEN = {
    f"--{k}": f"give {v}; an empty value names none" for k, _, v in (p.partition(":") for p in _EMPTY_HINTS.split("|"))
} | {k: f"give {v}" for k, _, v in (p.partition("=") for p in _EXTRA_HINTS.split("|"))}


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
            hint, value = _param_hint(param), given.get(param.name)
            strs = [item for item in (value if isinstance(value, list) else [value]) if isinstance(item, str)]
            if any(not item.isascii() and _surrogates(item) for item in strs):
                return typer.BadParameter("expected UTF-8 text", ctx, param_hint=hint)
            if (reason := _GIVEN.get(hint)) and any(not item.strip() for item in strs):
                return typer.BadParameter(reason, ctx, param_hint=hint)
        return None

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except _m.InputError as error:
            a = error.argument
            hint = f"--{a}" if a in {"rel", "scope", "offset"} else None
            hint = a.upper() if a in {"query", "ref", "topic", "source"} else hint
            raise typer.BadParameter(str(error), ctx, param_hint=hint) from None


def _surrogates(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _cancel(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def _fail(message: str, code: int, error: Exception | None = None) -> NoReturn:
    try:
        sys.stdout.flush()
    except (OSError, ValueError) as refused:
        with suppress(OSError, ValueError):
            os.dup2(devnull := os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            os.close(devnull)
        if isinstance(error, OSError) and isinstance(refused, OSError) and refused.errno == error.errno:
            message = f"cannot write the reply to standard output{f' ({error.strerror})' if error.strerror else ''}"
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
        _fail("invalid input: " + _m.explain(error), 2)
    except _m.FAILURES as error:
        _fail(_m.failure(error), 1, error)
    finally:
        for signum, handler in previous.items():
            if handler is not None:
                signal.signal(signum, handler)


for _command in (*app.registered_commands, *action_app.registered_commands, *skill_app.registered_commands):
    _command.cls = _Command
