"""Skills ship in the package: their helpers suit the agent's python3, and their commands, links and files exist."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest
import typer
import yaml
from markdown_it import MarkdownIt
from typer.core import TyperGroup

from bf import __version__, cli, retrieve
from bf.install import BLOCKED
from bf.install import SKILLS as INSTALLED
from bf.storage import Store
from bf.validate import validate

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src/bf/skills"
HELPERS = sorted(SKILLS.glob("*/scripts/*.py"))
ENTRIES = sorted(SKILLS.glob("*/SKILL.md"))
# The generated brain instructions teach the same commands and links as the skills.
TEXTS = {str(path.relative_to(ROOT)): path.read_text(encoding="utf-8") for path in sorted(SKILLS.rglob("*.md"))}
TEXTS["src/bf/cli.py:AGENTS"] = cli.AGENTS
DOCS = re.compile(r"https://fmind\.github\.io/brain-framework/docs/([a-z0-9-]*)/?(?:#([\w.-]+))?")
REPOSITORY = re.compile(r"https://github\.com/fmind/brain-framework/(?:blob|tree)/main/([\w./-]+)")
MARKDOWN = MarkdownIt("commonmark").enable("table")
# A bf invocation, optionally through exec or a brain's pinned runtime (uv run --project PATH --locked bf).
# An option's value never starts like an option, so each word parses one way and matching stays linear.
INVOCATION = re.compile(r"\s*(?:exec\s+|uv\s+run\s+(?:--\S+\s+(?:(?!bf\s)(?:-?[^-\s]\S*|--?)\s+)?)*)?bf\s")
# Each SKILL.md stays a short router: detail lives in the references it names.
WORDS = 1300


def slugs(path: Path) -> set[str]:
    """Explicit `{#id}` anchors and heading slugs, as both the docs site and GitHub derive them."""
    tokens = MARKDOWN.parse(path.read_text(encoding="utf-8"))
    anchors = set()
    for token, inline in pairwise(tokens):
        if token.type == "heading_open":
            title = inline.content
            if explicit := re.search(r"\s*\{#([\w.-]+)\}$", title):
                anchors.add(explicit[1])
                title = title[: explicit.start()]
            words = re.sub(r"[^\w\s-]", "", title.replace("`", "").lower()).strip()
            anchors |= {re.sub(r"[-\s]+", "-", words), re.sub(r"\s", "-", words)}
    return anchors


def commands(text: str) -> Iterator[list[str]]:
    """Each `bf ...` invocation in code spans and code blocks, split at shell separators, from `bf` on."""
    snippets = []
    for token in MARKDOWN.parse(text):
        if token.type in {"fence", "code_block"}:
            snippets.append(token.content.replace("\\\n", " "))
        snippets += [child.content for child in token.children or [] if child.type == "code_inline"]
    for snippet in snippets:
        for segment in re.split(r"\n|\|+|;|&&|\$\(|\(|\)", snippet):
            if match := INVOCATION.match(segment):
                segment = segment[match.end() - 3 :]
                try:
                    yield shlex.split(segment, comments=True)
                except ValueError:
                    yield segment.split()


def links(text: str) -> Iterator[tuple[str, str]]:
    """Each relative Markdown link in a skill file, as (path, anchor)."""
    for token in MARKDOWN.parse(text):
        for child in token.children or []:
            href = str(child.attrGet("href") or "")
            if child.type == "link_open" and not re.match(r"[a-z][a-z0-9+.-]*:", href):
                path, _, anchor = href.partition("#")
                yield path, anchor


def test_skill_commands_and_options_exist() -> None:
    # Installed skills cannot follow a renamed command; an agent would discover it only on failure.
    group = typer.main.get_command(cli.app)
    assert isinstance(group, TyperGroup)
    found = 0
    for name, text in TEXTS.items():
        for argv in commands(text):
            # Options end at `--`: what follows passes through to a routine.
            head = argv[1 : argv.index("--")] if "--" in argv else argv[1:]
            words = [word for word in head if not word.startswith("-")]
            if words and (words[0].isupper() or words[0] == "..."):
                continue  # A placeholder such as `bf COMMAND --help` or `bf ...`.
            command = group.commands.get(words[0]) if words and head[0] == words[0] else group
            assert command is not None, f"{name}: unknown command in {shlex.join(argv)}"
            options = {"--help", "-h", *(option for param in command.params for option in param.opts)}
            options |= {option for param in command.params for option in param.secondary_opts}
            for option in (word.partition("=")[0] for word in head if word.startswith("-")):
                assert option in options, f"{name}: unknown option {option} in {shlex.join(argv)}"
            found += 1
    assert found > 80


def test_skills_never_retrieve_through_a_brain_pinned_runtime() -> None:
    # `uv run --project BRAIN` installs and runs what the brain's uv.lock names: offline commands and helpers use the
    # installed bf, so a shared brain gains no code execution from a search. Only reviewed execution goes through it.
    # A helper never runs through `uv run`, even with --no-project: it adopts a .venv in the working directory or a
    # parent, and uv before 0.12 reads the uv.toml there. `uv python find --system --no-config --no-project` reads
    # neither, nor the brain's pyproject.toml.
    pinned = re.compile(
        r"uv\s+run\s+(?:-\S+\s+(?:(?!bf\s)(?:[^-\s]\S*|-)\s+)?)*"
        r"(?:bf\s+(?:search|read|status|validate|eval|export|mcp|COMMAND|--version)\b|python3?\s|\S*scripts/)"
        r"|uv\s+python\s+find\s+(?!--system\s+--no-config\s+--no-project\s)"
    )
    for name, text in TEXTS.items():
        assert not pinned.search(text), name
        # Retrieval and note updates never go through a pin, whatever `bf ...` command a sentence names.
        if name.startswith("src/bf/skills/bf-use/") or name == "src/bf/cli.py:AGENTS":
            assert not re.search(r"\buv\s+run\b", text), name
    assert pinned.search("uv run --project PATH --locked bf COMMAND ... --brain PATH")
    assert pinned.search("uv run --project PATH --locked bf read REF")
    assert pinned.search('uv run --project PATH --locked python3 "$SKILL_DIR/scripts/evidence.py"')
    assert pinned.search('uv run --locked "$SKILL_DIR/scripts/check-handoff.py"')
    assert pinned.search('uv run --no-project python3 "$SKILL_DIR/scripts/evidence.py"')
    assert not pinned.search("uv run --project PATH --locked bf collect SENSOR")
    assert pinned.search('uv run --no-project --no-config --python 3.14 "$SKILL_DIR/scripts/NAME.py"')
    assert pinned.search('uv run -p 3.14 "$SKILL_DIR/scripts/NAME.py"')
    assert pinned.search("uv python find --no-config 3.14")
    assert pinned.search("uv python find --system --no-config 3.14")
    assert not pinned.search('"$(uv python find --system --no-config --no-project 3.14)" "$SKILL_DIR/scripts/NAME.py"')


def test_skill_links_resolve_to_this_checkout() -> None:
    found = 0
    for name, text in TEXTS.items():
        for page, anchor in DOCS.findall(text):
            target = ROOT / "docs/docs" / f"{page or 'index'}.md"
            assert target.is_file(), f"{name}: missing docs page {page}"
            assert not anchor or anchor in slugs(target), f"{name}: missing anchor {page}#{anchor}"
            found += 1
        for path in REPOSITORY.findall(text):
            assert (ROOT / path).exists(), f"{name}: missing repository path {path}"
            found += 1
        if name.startswith("src/bf/skills/"):
            source = ROOT / name
            for path, anchor in links(text):
                target = (source.parent / path).resolve() if path else source
                assert target.exists(), f"{name}: missing {path}#{anchor}"
                assert not anchor or target.suffix != ".md" or anchor in slugs(target), f"{name}: {path}#{anchor}"
                found += 1
    assert found > 80


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda path: path.parent.name)
def test_each_skill_file_is_reachable_from_its_router(entry: Path) -> None:
    # Agents load a skill's other files only through its SKILL.md: an unlisted file is dead weight.
    folder = entry.parent
    linked = {(entry.parent / path).resolve() for path, _ in links(entry.read_text(encoding="utf-8"))}
    files = {path.resolve() for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    assert files - {entry.resolve()} <= linked, sorted(str(path.relative_to(folder)) for path in files - linked)
    assert {path for path in linked if path.is_relative_to(folder)} <= files
    # Helpers are named relative to the skill folder, never by an installation path.
    for path in files:
        if path.suffix == ".md":
            text = path.read_text(encoding="utf-8")
            assert ".agents/skills/" not in text, path
            assert "~/.claude/skills/bf-" not in text, path
            # Only the router carries skill metadata; references are plain guides.
            assert path.parent.name != "references" or not text.startswith("---\n"), path


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda path: path.parent.name)
def test_skill_routers_stay_short(entry: Path) -> None:
    assert len(entry.read_text(encoding="utf-8").split()) <= WORDS


def test_blocking_skill_statuses_are_explained_where_bf_skills_is_run() -> None:
    # An unexplained status invites --force, which replaces a person's edits or downgrades a newer bf's copy.
    for name in (
        "src/bf/skills/README.md",
        "src/bf/skills/bf-setup/SKILL.md",
        "src/bf/skills/bf-maintain/references/operations.md",
    ):
        assert {status for status in BLOCKED if f"`{status}`" not in TEXTS[name]} == set(), name


def test_packaged_skills_are_the_installed_ones() -> None:
    assert {entry.parent.name for entry in ENTRIES} == set(INSTALLED)
    assert {path.name for path in SKILLS.iterdir() if path.is_dir()} == set(INSTALLED)


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: path.name)
def test_helpers_are_standalone_python311_scripts(helper: Path) -> None:
    # Agents invoke helpers with their own python3; the package itself requires 3.14.
    source = helper.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 11))
    assert source.startswith("#!/usr/bin/env python3\n")
    assert os.access(helper, os.X_OK)
    modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    modules |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert {module.partition(".")[0] for module in modules} <= sys.stdlib_module_names | {"__future__"}


def test_helpers_use_only_the_python311_standard_library() -> None:
    # Grammar checks miss newer library calls, such as itertools.batched in Python 3.12.
    result = subprocess.run(  # noqa: S603 - the locked type checker on the bundled helpers only
        [Path(sys.executable).with_name("ty"), "check", "--python-version", "3.11", *HELPERS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("skill", ENTRIES, ids=lambda path: path.parent.name)
def test_distributed_skills_have_portable_metadata(skill: Path) -> None:
    # bf skills records the package version beside each copy: a version line in the skill would change it every release.
    frontmatter = yaml.safe_load(skill.read_text(encoding="utf-8").split("---\n")[1])
    assert frontmatter["name"] == skill.parent.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", frontmatter["name"])
    assert len(frontmatter["name"]) <= 64
    assert isinstance(frontmatter["description"], str)
    assert 1 <= len(frontmatter["description"].strip()) <= 1024
    assert frontmatter["license"] == "MIT"
    assert isinstance(frontmatter["compatibility"], str)
    assert 1 <= len(frontmatter["compatibility"].strip()) <= 500
    assert set(frontmatter) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert "metadata" not in frontmatter
    assert f"Brain Framework {__version__.split('.')[0]} " in frontmatter["compatibility"]


def test_action_helper_writes_the_template_sections(brain: Store) -> None:
    helper = SKILLS / "bf-use/scripts/new-action.py"
    reply = subprocess.run(  # noqa: S603 - the bundled helper on a synthetic brain
        [sys.executable, str(helper), "review", "--brain", str(brain.root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    created = brain.read(json.loads(reply.stdout)["action"]).decode()
    template = (SKILLS / "bf-use/templates/action.md").read_text(encoding="utf-8")
    assert re.findall(r"^## .*", created, re.MULTILINE) == re.findall(r"^## .*", template, re.MULTILINE)
    assert validate(brain)["valid"]
    # A quoted `~user` that names no account is invalid input, not a traceback.
    unknown = subprocess.run(  # noqa: S603 - the bundled helper, refused before any write
        [sys.executable, str(helper), "review", "--brain", "~bf-no-such-user-9c4f/brain"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert (unknown.returncode, "Traceback" in unknown.stderr) == (2, False)
    assert "home directory cannot be resolved" in unknown.stderr


def test_action_helper_removes_only_what_a_failed_write_created(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("new_action", SKILLS / "bf-use/scripts/new-action.py")
    assert spec is not None
    assert spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    # Text the file cannot encode fails once ACTION.md exists: the file and both new folders go.
    monkeypatch.setattr(helper, "skeleton", lambda *_: "\ud800")
    with pytest.raises(UnicodeEncodeError):
        helper.start(brain.root, "review", unique=False)
    assert not (brain.root / "actions").exists()
    # An interruption removes the new action folder, never an existing session or the actions folder it holds.
    other = "actions/2026-09-27_review/ACTION.md"
    brain.write(other, b"---\ntype: action\nstatus: draft\n---\n\n# Review\n")
    monkeypatch.setattr(helper, "skeleton", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        helper.start(brain.root, "review", unique=True)
    assert sorted(path.relative_to(brain.root).as_posix() for path in (brain.root / "actions").rglob("*")) == [
        "actions/2026-09-27_review",
        other,
    ]


def test_unedited_templates_validate_and_keep_their_documented_anchors(brain: Store) -> None:
    # Copies must not claim a sample identity: two unedited project notes would make it ambiguous.
    for path in ("projects/one.md", "projects/two.md"):
        brain.write(path, (SKILLS / "bf-use/templates/project.md").read_bytes())
    brain.write("concepts/idea.md", (SKILLS / "bf-use/templates/concept.md").read_bytes())
    brain.write("actions/2026-09-27_review/ACTION.md", (SKILLS / "bf-use/templates/action.md").read_bytes())
    report = validate(brain)
    assert report["valid"], report
    for ref in ("projects/one.md#now", "projects/one.md#decision", "projects/one.md#next-actions"):
        assert retrieve.read([brain], ref)["text"]
    for section in ("context", "decision", "resume"):
        assert retrieve.read([brain], f"actions/2026-09-27_review/ACTION.md#{section}")["text"]


def test_template_comments_are_valid_frontmatter(brain: Store) -> None:
    # The commented optional keys of the project template validate once uncommented.
    template = (SKILLS / "bf-use/templates/project.md").read_text(encoding="utf-8")
    brain.write("bf.yaml", b"version: 7\nname: fixture\n" + cli_fields())
    uncommented = re.sub(r"^# (\w+: .*?)(?:\s+#.*)?$", r"\1", template, flags=re.MULTILINE)
    uncommented = uncommented.replace("[LABEL]", "[website]")
    brain.write("projects/one.md", uncommented.encode())
    report = validate(brain)
    assert report["valid"], report
    listed = cast("list[dict[str, object]]", retrieve.read([brain], "projects")["items"])
    item = next(item for item in listed if item["ref"] == "projects/one.md")
    assert item["review_source"] == "stale_after"
    assert retrieve.read([brain], "https://github.com/owner/name")["ref"] == "projects/one.md"


def cli_fields() -> bytes:
    """The relations `bf init` declares, so the template's `fields:` example validates."""
    return b"fields:\n  owner:\n    description: Responsible.\n    type: identity\n    cardinality: many\n    relation: true\n"
