"""Follow the first-brain guides in an isolated home; check the replies they document and the queue errors docs quote."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import yaml

from conftest import BLOCK, FRAGMENT, anywhere

ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs" / "docs"
SAVE = re.compile(r"(?:Save|Create) `([^`]+)`")
ABSENT = re.compile(r"has no `([^`]+):` key")
REPLACE = re.compile(r"replace the `([^`]+)` entry under `([^`]+):`")
POINT = re.compile(r"In `([^`]+)`, point the (\w+) line")
INSTALL = "uv tool install --python 3.14 brain-framework"
REPOSITORY = "https://raw.githubusercontent.com/fmind/brain-framework/"
QUEUE_ERROR = re.compile(r"`(assistant queue [^`]*)`")


def _merge(config: Path, fragment: str, prose: str) -> None:
    """Add the fragment's top-level mappings to bf.yaml as the prose asks: into an absent key or replacing one entry."""
    data = yaml.safe_load(config.read_text())
    for key in ABSENT.findall(prose):
        assert key not in data, f"the guide says bf.yaml has no {key}: key"
    for entry, key in REPLACE.findall(prose):
        del data[key][entry]
    for key, value in yaml.safe_load(fragment).items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) else value
    config.write_text(yaml.safe_dump(data, sort_keys=False))


def _follow(text: str, home: Path, env: dict[str, str], release: str) -> None:
    """Run each command block, then check the replies and apply the files documented up to the next command block."""
    brain = home / "brain"
    segments = re.split(r"^(?=```bash\n)", text, flags=re.MULTILINE)[1:]
    assert segments, "the guide lost its command blocks"
    for segment in segments:
        blocks = [(match[1], match[2], match.start(), match.end()) for match in BLOCK.finditer(segment)]
        # The checkout's bf stands in for the published package; the documented install line stays pinned.
        installs = [line for line in blocks[0][1].splitlines() if line.startswith("uv tool install")]
        assert installs in ([], [INSTALL]), installs
        # Example files come from this checkout, served where the documented release tag would serve them.
        commands = [line.replace(REPOSITORY, release) for line in blocks[0][1].splitlines() if line not in installs]
        # A URL in another form would download the published file instead of testing this checkout, and need a network.
        assert not re.search(r"https?://", "\n".join(commands)), commands
        result = subprocess.run(  # noqa: S603 - execute the reviewed guide's commands on a disposable brain
            ["bash", "--noprofile", "--norc", "-euc", "\n".join(commands)],  # noqa: S607
            cwd=brain if brain.is_dir() else home,
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, f"{commands}\n{result.stderr}{result.stdout}"
        replies = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        for fragment in FRAGMENT.findall(BLOCK.sub("", segment)):
            assert fragment in result.stdout, f"{commands} did not return {fragment}"
        for index, (language, body, start, _) in enumerate(blocks[1:], 1):
            before = segment[blocks[index - 1][3] : start]
            if language == "json":
                assert any(anywhere(reply, json.loads(body)) for reply in replies), f"{commands} did not return {body}"
            elif language == "yaml":
                _merge(brain / "bf.yaml", body, before)
            elif saved := SAVE.findall(before):
                (brain / saved[-1]).parent.mkdir(parents=True, exist_ok=True)
                (brain / saved[-1]).write_text(body)
            elif "beneath the project's Decision section" in before:
                note = brain / "projects" / "new-website.md"
                note.write_text(note.read_text().replace("## Next actions", f"{body}\n## Next actions", 1))
            elif point := POINT.search(before):
                note = brain / point[1]
                line = re.search(rf"^{point[2]}:.*\n", note.read_text(), re.MULTILINE)
                assert line, f"{point[1]} has no {point[2]} line"
                note.write_text(note.read_text().replace(line[0], body, 1))
            else:
                raise AssertionError(f"no test action for this {language} block:\n{body}")


def test_first_brain_guides_return_the_documented_replies() -> None:
    # The autouse fixture already isolates HOME, XDG state and configuration, so `~/brain` is disposable.
    home = Path(os.environ["HOME"])
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "UV_OFFLINE": "1",
        "TZ": "UTC",
    }
    # A release tag directory named from the checkout's version, so `v$(bf --version)` must resolve to it.
    (home / "release").mkdir()
    (home / "release" / f"v{version('brain-framework')}").symlink_to(ROOT)
    release = f"{(home / 'release').as_uri()}/"
    note = home / "brain" / "projects" / "new-website.md"

    _follow((DOCS / "getting-started.md").read_text(), home, env, release)
    assert "brief:website-brief" in note.read_text()

    # Add a sensor starts from the brain Getting started leaves and replaces its tutorial sensor.
    sensors = (DOCS / "sensors.md").read_text()
    first = sensors[sensors.index("## Your first sensor") : sensors.index("## Selected highlights")]
    _follow(first, home, env, release)
    assert "local-documents:website-demo/brief.txt" in note.read_text()
    assert not (home / "brain" / "memories" / "brief").exists()


def _messages() -> list[str]:
    """Every string the package spells, with an f-string's fields as NUL: where its diagnostics come from."""
    messages = []
    for path in sorted((ROOT / "src" / "bf").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                messages.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                messages.append(
                    "".join(str(part.value) if isinstance(part, ast.Constant) else "\0" for part in node.values)
                )
    return messages


def test_quoted_queue_errors_are_ones_bf_reports() -> None:
    """A recovery row must match a message the code can print, so renaming one in code leaves no dead diagnosis."""
    messages = _messages()
    guides = [*DOCS.glob("*.md"), *(ROOT / "src" / "bf" / "skills").rglob("*.md")]
    quoted = {text for path in guides for text in QUEUE_ERROR.findall(path.read_text(encoding="utf-8"))}
    assert quoted
    for text in sorted(quoted):
        # `…` cuts the quote short, and upper-case words such as N or NAME stand for an f-string's fields.
        pattern = ".+?".join(map(re.escape, re.split(r"\b[A-Z]+\b", text.removesuffix("…").rstrip())))
        assert any(re.match(pattern, message, re.DOTALL) for message in messages), text
