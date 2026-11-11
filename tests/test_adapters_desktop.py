"""The send-only desktop channel passes text to the notifier only as argv and returns a receipt BF accepts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from bf.cli import app
from bf.messages import Receipt
from bf.storage import Store
from conftest import ROOT, Provider

ADAPTER = "desktop.py"
FOLDER = "channels/channels"
# Text a shell or AppleScript would run if it were ever interpolated into code.
HOSTILE = '"; do shell script "touch pwned" & "$(touch pwned)'
SCRIPT = [
    "-e",
    "on run argv",
    "-e",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "-e",
    "end run",
]


def send(provider: Provider, message: dict, *arguments: str):
    return provider.run(ADAPTER, "send", *arguments, folder=FOLDER, stdin=json.dumps(message))


def receipt(result) -> Receipt:
    assert result.returncode == 0, result.stderr
    return Receipt.model_validate_json(result.stdout)


def test_notify_send_receives_the_text_only_after_the_end_of_options(provider: Provider) -> None:
    provider.install("notify-send", [{"match": [], "stdout": ""}])
    result = send(
        provider, {"id": "d1", "text": f"Review ready <a href='x'>{HOSTILE}</a>"}, "--notifier", "notify-send"
    )
    assert receipt(result).conversation == "desktop"
    [call] = provider.calls("notify-send")
    # Markup is escaped, so no text becomes a link or a tag.
    escaped = HOSTILE.replace("&", "&amp;")
    assert call == ["--", "Brain", f"Review ready &lt;a href='x'&gt;{escaped}&lt;/a&gt;"]


def test_osascript_receives_the_text_as_run_handler_arguments(provider: Provider) -> None:
    provider.install("osascript", [{"match": [], "stdout": ""}])
    result = send(provider, {"id": "d1", "text": HOSTILE}, "--notifier", "osascript", "--title", "Watcher")
    receipt(result)
    assert provider.calls("osascript") == [[*SCRIPT, HOSTILE, "Watcher"]]
    # osascript reads options until its first other argument: a text starting with a dash never becomes one.
    provider.install("osascript", [{"match": [], "stdout": ""}])
    receipt(send(provider, {"id": "d2", "text": "-e do shell script"}, "--notifier", "osascript", "--title=-s"))
    assert provider.calls("osascript") == [[*SCRIPT, " -e do shell script", " -s"]]


def test_a_requested_conversation_is_preserved_and_receipts_differ(provider: Provider) -> None:
    provider.install("notify-send", [{"match": [], "stdout": "", "repeat": True}])
    message = {"id": "d1", "text": "Ready", "conversation": "thread-7"}
    first = receipt(send(provider, message, "--notifier", "notify-send"))
    second = receipt(send(provider, message, "--notifier", "notify-send"))
    assert first.conversation == second.conversation == "thread-7"
    assert first.id != second.id


def test_long_text_is_cut_and_hidden_characters_stay_visible(provider: Provider) -> None:
    provider.install("notify-send", [{"match": [], "stdout": ""}])
    receipt(send(provider, {"id": "d1", "text": "a\u202eb\nc" + "x" * 5000}, "--notifier", "notify-send"))
    body = provider.calls("notify-send")[0][-1]
    assert body.startswith("a\\u202eb\nc")
    assert body.endswith("x…")
    assert len(body) == 1000 + len("\\u202e") - 1


@pytest.mark.parametrize("notifier", ["notify-send", "osascript"])
def test_a_missing_or_failing_notifier_never_echoes_the_text(provider: Provider, notifier: str) -> None:
    message = {"id": "d1", "text": "Private launch figures"}
    # Only the fake folder is on PATH: the host's own notifier must never run.
    result = subprocess.run(  # noqa: S603 - the adapter's own process boundary
        [sys.executable, str(ROOT / "examples" / FOLDER / ADAPTER), "send", "--notifier", notifier],
        env={"PATH": str(provider.bin), "LANG": "C.UTF-8"},
        input=json.dumps(message),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == f"desktop: {notifier} is not on PATH; install it or pass --notifier; nothing was shown\n"
    provider.install(notifier, [{"match": [], "stderr": "Private launch figures", "code": 3}])
    result = send(provider, message, "--notifier", notifier)
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == f"desktop: {notifier} exited with status 3; nothing was confirmed\n"


@pytest.mark.parametrize("stdin", ["", "not json", '{"id": "d1"}', '{"id": "", "text": "x"}', '{"id": 1, "text": "x"}'])
def test_an_invalid_message_exits_2_before_any_notification(provider: Provider, stdin: str) -> None:
    provider.install("notify-send", [])
    result = provider.run(ADAPTER, "send", "--notifier", "notify-send", folder=FOLDER, stdin=stdin)
    assert (result.returncode, result.stdout) == (2, "")
    assert provider.calls("notify-send") == []


def test_bf_channel_send_accepts_the_receipt(provider: Provider, brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("channels/_bf.py", (ROOT / "examples" / FOLDER / "_bf.py").read_bytes())
    brain.write("channels/desktop.py", (ROOT / "examples" / FOLDER / ADAPTER).read_bytes())
    channel = {"desktop": {"send": {"command": ["python3", "channels/desktop.py", "send"]}}}
    brain.write("bf.yaml", yaml.safe_dump({"version": 8, "name": "fixture", "channels": channel}).encode())
    # Either platform's notifier: the adapter picks one by sys.platform.
    for notifier in ("notify-send", "osascript"):
        provider.install(notifier, [{"match": [], "stdout": ""}])
    monkeypatch.setenv("PATH", f"{provider.bin}:{Path(sys.executable).parent}:/usr/bin:/bin")
    result = CliRunner().invoke(
        app,
        ["channel", "send", "desktop", "--stdin", "--brain", str(brain.root)],
        input='{"text":"The review is ready."}',
    )
    assert result.exit_code == 0, result.output
    reply = json.loads(result.stdout)
    assert reply["state"] == "delivered"
    assert reply["receipt"]["conversation"] == "desktop"
    calls = provider.calls("notify-send") + provider.calls("osascript")
    assert [call[-2:] for call in calls] in ([["Brain", "The review is ready."]], [["The review is ready.", "Brain"]])
