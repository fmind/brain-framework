"""The copy-in sensor kit keeps provider calls bounded and records within BF's contract; its example fails closed."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter
from typer.testing import CliRunner

from bf.cli import app
from bf.models import Record
from bf.storage import Store
from conftest import ROOT, Provider, assert_linear_time

KIT = ROOT / "examples/sensors/_sensor.py"
START, END = "2026-09-01T00:00:00.000000Z", "2026-09-02T00:00:00.000000Z"
WINDOW = {"BF_START": START, "BF_END": END}
# Fake credentials are assembled at run time, so the repository's secret scan never matches a literal.
# Joined at run time: the compiler folds literal concatenation, which secret scanners then read in .pyc caches.
SECRET = "".join(("ghp_", "A1b2C3d4E5" * 4))
KEY = "".join(("PRIVATE ", "KEY-----"))


def kit() -> SimpleNamespace:
    """The kit's names, loaded without writing bytecode beside the example."""
    return SimpleNamespace(**runpy.run_path(str(KIT)))


def issue(number: int, updated: str = "2026-09-01T10:00:00Z", **extra: object) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}\nsecond line",
        "body": f"Steps to reproduce.\nGITHUB_TOKEN={SECRET}",
        "state": "CLOSED",
        "stateReason": "COMPLETED",
        "author": {"login": "Alice-Dev", "is_bot": False},
        "assignees": [{"login": "bob"}],
        "labels": [{"name": "bug"}],
        "createdAt": "2026-08-20T09:00:00Z",
        "updatedAt": updated,
        "closedAt": updated,
        "url": f"https://github.com/acme/widgets/issues/{number}",
        **extra,
    }


def failed(result: subprocess.CompletedProcess[str], *private: str) -> None:
    assert result.returncode == 1
    assert not result.stdout
    assert len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.stderr
    for text in private:
        assert text not in result.stderr


def test_github_issues_maps_the_window_to_standard_attributes(provider: Provider) -> None:
    # The search widens to whole seconds; an issue updated at the window's end belongs to the next one.
    items = [issue(1), issue(2, updated=END), issue(3, author=None, assignees=[], labels=None, body=None)]
    provider.install("gh", [{"match": ["issue", "list"], "stdout": items}])
    # Typed in GitHub's display case, the repository still yields lowercase ids and aliases.
    result = provider.run("github-issues.py", "Acme/Widgets", environment=WINDOW)
    assert result.returncode == 0, result.stderr
    first, ghost = TypeAdapter(list[Record]).validate_python(json.loads(result.stdout))
    assert (first.id, first.title) == ("acme/widgets/issues/1", "acme/widgets: Issue 1 second line")
    assert (first.time, first.url) == ("2026-08-20T09:00:00.000000Z", "https://github.com/acme/widgets/issues/1")
    assert first.aliases == ["github:acme/widgets/issues/1"]
    assert first.links == ["person:github.com/alice-dev", "person:github.com/bob", "repo:github.com/acme/widgets"]
    assert first.attributes == {
        "number": 1,
        "kind": "issue",
        "status": "closed",
        "reason": "completed",
        "labels": ["bug"],
        "label_refs": ["label:github.com/acme/widgets/bug"],
        "updated": "2026-09-01T10:00:00.000000Z",
        "author_refs": ["person:github.com/alice-dev"],
        "assignee_refs": ["person:github.com/bob"],
        "repository_refs": ["repo:github.com/acme/widgets"],
    }
    assert SECRET not in first.text
    assert "GITHUB_TOKEN=[REDACTED]" in first.text
    assert (ghost.id, ghost.attributes["author_refs"], ghost.links) == (
        "acme/widgets/issues/3",
        [],
        ["repo:github.com/acme/widgets"],
    )
    [argv] = provider.calls("gh")
    assert argv[argv.index("--repo") + 1] == "acme/widgets"
    assert argv[argv.index("--search") + 1] == "updated:2026-09-01T00:00:00+00:00..2026-09-02T00:00:00+00:00"
    assert argv[argv.index("--limit") + 1] == "501"


def test_github_issues_takes_a_manual_window_from_arguments(provider: Provider) -> None:
    provider.install("gh", [{"match": ["issue", "list"], "stdout": [issue(1)]}])
    result = provider.run("github-issues.py", "acme/widgets", "2026-09-01T00:00:00+02:00", "2026-09-02T00:00:00+02:00")
    assert result.returncode == 0, result.stderr
    [argv] = provider.calls("gh")
    assert argv[argv.index("--search") + 1] == "updated:2026-08-31T22:00:00+00:00..2026-09-01T22:00:00+00:00"


@pytest.mark.parametrize(
    ("response", "arguments"),
    [
        ({"match": ["issue"], "stdout": "", "stderr": f"HTTP 401: Bad credentials {SECRET}", "code": 1}, []),
        ({"match": ["issue"], "stdout": [issue(n) for n in range(1, 503)]}, []),
        ({"match": ["issue"], "stdout": '[{"number": 1, "title": "partial'}, []),
        ({"match": ["issue"], "stdout": {"message": f"private {SECRET}"}}, []),
        ({"match": ["issue"], "stdout": [issue(1, state="private-state")]}, []),
        ({"match": ["issue"], "stdout": [issue(1), issue(1)]}, []),
        ({"match": ["issue"], "stdout": [issue(1, updatedAt=f"private {SECRET}")]}, []),
        ({"match": ["issue"], "stdout": []}, ["not a repository"]),
    ],
)
def test_github_issues_fails_closed_without_provider_text(
    provider: Provider, response: dict[str, object], arguments: list[str]
) -> None:
    provider.install("gh", [response])
    result = provider.run("github-issues.py", *(arguments or ["acme/widgets"]), environment=WINDOW)
    failed(result, SECRET, "private", "Bad credentials")


def test_github_issues_failure_keeps_saved_records(
    provider: Provider, brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Through real collection: BF passes the window in BF_START and BF_END and a private BF_STATE_DIR.
    for name in ("_sensor.py", "github-issues.py"):
        brain.write(f"sensors/{name}", (ROOT / "examples/sensors" / name).read_bytes())
    command = ["python3", "sensors/github-issues.py", "acme/widgets"]
    config = {"version": 8, "name": "fixture", "sensors": {"issues": {"command": command}}}
    brain.write("bf.yaml", yaml.safe_dump(config).encode())
    monkeypatch.setenv("PATH", f"{provider.bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin")
    provider.install("gh", [{"match": ["issue"], "stdout": [issue(1), issue(2)], "repeat": True}])
    args = ["collect", "issues", "--brain", str(brain.root), "--since", START, "--until", END]
    success = CliRunner().invoke(app, args)
    assert success.exit_code == 0, success.output
    saved = {path.name: path.read_bytes() for path in (brain.root / "memories/issues").glob("*.json")}
    assert len(saved) == 2
    provider.install("gh", [{"match": ["issue"], "stdout": "", "code": 1}])
    failure = CliRunner().invoke(app, args)
    assert failure.exit_code == 1
    assert saved == {path.name: path.read_bytes() for path in (brain.root / "memories/issues").glob("*.json")}


def driver(tmp_path: Path, body: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `body` as a sensor beside a copy of the kit, as an owner's brain holds them."""
    folder = tmp_path / "sensors"
    folder.mkdir(exist_ok=True)
    shutil.copy(KIT, folder / "_sensor.py")
    (folder / "driver.py").write_text("import sys\nimport _sensor\n" + body)
    return subprocess.run(  # noqa: S603 - the kit's own process boundary
        [sys.executable, str(folder / "driver.py")],
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8", **(environment or {})},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_window_and_state_dir_come_from_bf(tmp_path: Path) -> None:
    state = tmp_path / "state" / "issues"
    body = (
        "start, end = _sensor.window(sys.argv[1:3])\nprint(start.isoformat(), end.isoformat(), _sensor.state_dir())\n"
    )
    result = driver(tmp_path, body, {**WINDOW, "BF_STATE_DIR": str(state)})
    assert result.stdout.split() == ["2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", str(state)]
    # BF may delete the folder between runs: an empty or missing one is a first run.
    assert state.is_dir()
    assert not any(state.iterdir())
    failed(driver(tmp_path, "_sensor.main(_sensor.window, 'x')\n"))
    failed(driver(tmp_path, "_sensor.main(_sensor.state_dir, 'x')\n", WINDOW))
    failed(driver(tmp_path, "_sensor.main(_sensor.window, 'x')\n", {"BF_START": END, "BF_END": START}))
    failed(driver(tmp_path, "_sensor.main(_sensor.window, 'x')\n", {"BF_START": "2026-09-01", "BF_END": END}))


def test_main_emits_one_array_or_fails_closed(tmp_path: Path) -> None:
    ok = driver(tmp_path, "_sensor.main(lambda: [_sensor.record('a', 'A'), _sensor.record('b', 'B')], 'x')\n")
    assert ok.returncode == 0, ok.stderr
    assert [item["id"] for item in json.loads(ok.stdout)] == ["a", "b"]
    for body, message in (
        ("_sensor.main(lambda: [_sensor.record('a', 'A'), _sensor.record('a', 'private')], 'x')\n", "share one id"),
        ("_sensor.main(lambda: [{'id': 'a', 'title': 'A', 'fields': {'kind': 'private'}}], 'x')\n", "never prints"),
        (
            (
                "def collect():\n    yield _sensor.record('a', 'A')\n    raise KeyError('private provider key')\n"
                "_sensor.main(collect, 'collection failed')\n"
            ),
            "collection failed (KeyError)",
        ),
        ("_sensor.main(lambda: [_sensor.record('a\\nprivate', 'A')], 'x')\n", "record id"),
        ("_sensor.fail('line one\\nline two\\x1b[31m')\n", "line one line two [31m"),
    ):
        result = driver(tmp_path, body)
        failed(result, "private", "\x1b")
        assert message in result.stderr


def test_emit_reports_what_a_tolerant_sensor_missed(tmp_path: Path, brain: Store) -> None:
    records = "[_sensor.record('a', 'A'), _sensor.record('b', 'B')]"
    plain = driver(tmp_path, f"_sensor.emit({records}, missed=0, of=3)\n")
    assert [item["id"] for item in json.loads(plain.stdout)] == ["a", "b"]
    # One feed of three failed: the sensor names it on stderr, for BF's log, and reports the count.
    body = f"print('feed 2 timed out', file=sys.stderr)\n_sensor.emit({records}, missed=1, of=3)\n"
    partial = driver(tmp_path, body)
    assert partial.returncode == 0, partial.stderr
    printed = json.loads(partial.stdout)
    assert printed["incomplete"] == {"missed": 1, "of": 3}
    assert [item["id"] for item in printed["records"]] == ["a", "b"]
    # BF accepts what the kit prints, as a saved-output check shows.
    brain.write("bf.yaml", b"version: 8\nname: fixture\nsensors:\n  feeds:\n    command: [echo]\n")
    saved = tmp_path / "partial.json"
    saved.write_text(partial.stdout)
    reply = CliRunner().invoke(app, ["collect", "feeds", "--input", str(saved), "--brain", str(brain.root)])
    assert reply.exit_code == 0, reply.output
    assert json.loads(reply.stdout)["incomplete"] == {"missed": 1, "of": 3}
    for counts in ("missed=2, of=1", "missed=-1, of=1", "missed=True, of=1", "missed=1.0, of=2", "missed=1, of=2**53"):
        result = driver(tmp_path, f"_sensor.main(lambda: _sensor.emit([], {counts}) or [], 'x')\n")
        failed(result)
        assert "missed and of must be integers" in result.stderr


def test_run_is_bounded_reaped_and_starts_outside_the_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sensor = kit()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other.git"))
    assert sensor.run(["/bin/pwd"], timeout=30, max_bytes=1024) == b"/\n"
    assert b"GIT_DIR" not in sensor.run(["/usr/bin/env"], timeout=30, max_bytes=1 << 20)
    pid = tmp_path / "pid"
    for mode, message in (("overflow", "exceeds 1024 bytes"), ("timeout", "1s timeout")):
        producer = tmp_path / f"{mode}.sh"
        body = "head -c 8192 /dev/zero\n" if mode == "overflow" else "exec 1>&-\n"
        producer.write_text(f'echo $$ > "{pid}"\n{body}exec sleep 60\n')
        with pytest.raises(sensor.SensorError, match=message):
            sensor.run(["/bin/sh", str(producer)], timeout=30 if mode == "overflow" else 1, max_bytes=1024)
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid.read_text()), 0)
    leak = tmp_path / "leak.sh"
    leak.write_text(f"echo 'private {SECRET}' >&2\nexit 3\n")
    with pytest.raises(sensor.SensorError) as error:
        sensor.run(["/bin/sh", str(leak)], timeout=30, max_bytes=1024)
    assert str(error.value) == "sh failed with status 3; check its authentication and arguments"
    with pytest.raises(sensor.SensorError, match="cannot start"):
        sensor.run([str(tmp_path / "missing")], timeout=30, max_bytes=1024)


def test_secret_reads_only_a_private_file_outside_the_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sensor = kit()
    brain, private = tmp_path / "brain", tmp_path / "config"
    brain.mkdir()
    private.mkdir()
    monkeypatch.setenv("BF_BRAIN", str(brain))
    monkeypatch.setenv("HOME", str(tmp_path))

    def write(name: str, data: bytes, mode: int = 0o600, folder: Path = private) -> Path:
        (path := folder / name).write_bytes(data)
        path.chmod(mode)
        return path

    token = write("token", f"{SECRET}\n".encode())
    assert sensor.secret(token) == sensor.secret("~/config/token") == SECRET
    # Exactly one trailing newline goes, as an editor or `echo` writes it.
    assert sensor.secret(write("windows", f"{SECRET}\r\n".encode())) == SECRET
    assert sensor.secret(write("blank", b"a\n\n")) == "a\n"
    assert sensor.secret(write("full", b"x" * (64 << 10))) == "x" * (64 << 10)
    (link := private / "link").symlink_to(token)
    os.mkfifo(fifo := private / "fifo", 0o600)
    # A folder linked into the brain resolves inside it, wherever its own path is.
    (alias := tmp_path / "alias").symlink_to(brain, target_is_directory=True)
    write("inside", SECRET.encode(), folder=brain)
    for path, message in (
        (link, "must be a regular file"),
        (fifo, "must be a regular file"),
        (write("group", SECRET.encode(), 0o640), "open to your group or others; run chmod 600"),
        (write("others", SECRET.encode(), 0o604), "open to your group or others; run chmod 600"),
        (brain / "inside", "inside the brain"),
        (alias / "inside", "inside the brain"),
        (write("large", (SECRET * 2000).encode()[: 65 << 10]), "exceeds 65536 bytes"),
        (write("empty", b"\n"), "is empty"),
        (write("binary", SECRET.encode() + b"\xff"), "not UTF-8"),
        (private / "missing", "cannot read secret file missing"),
        ("config/token", "needs an absolute path"),
    ):
        with pytest.raises(sensor.SensorError, match=message) as error:
            sensor.secret(path)
        assert SECRET not in str(error.value)
    # A copied sensor fails closed on a shared token file: one line naming the problem, never the token.
    body = "import os\n_sensor.main(lambda: [_sensor.record('a', _sensor.secret(os.environ['TOKEN']))], 'x')\n"
    result = driver(tmp_path, body, {"TOKEN": str(private / "group")})
    failed(result, SECRET)
    assert "secret file group is open to your group or others" in result.stderr


def test_secret_names_an_unknown_home_and_finds_the_brain_by_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensor = kit()
    brain = tmp_path / "brain"
    brain.mkdir()
    (token := brain / "token").write_bytes(SECRET.encode())
    token.chmod(0o600)
    monkeypatch.setenv("BF_BRAIN", str(brain))
    # An unknown user's home folder fails as one diagnostic, never as a RuntimeError traceback.
    with pytest.raises(sensor.SensorError, match="names an unknown home folder"):
        sensor.secret("~no-such-user-for-bf-tests/token")
    # A case-insensitive volume reaches the brain by another spelling; the folder's identity still matches.
    real, spelled = os.stat, str(tmp_path / "BRAIN")
    monkeypatch.setenv("BF_BRAIN", spelled)
    monkeypatch.setattr(os, "stat", lambda target, *a, **k: real(brain if str(target) == spelled else target, *a, **k))
    with pytest.raises(sensor.SensorError, match="inside the brain") as error:
        sensor.secret(token)
    assert SECRET not in str(error.value)
    # Without the brain BF names, it stops before reading the file.
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "missing"))
    with pytest.raises(sensor.SensorError, match="cannot find the brain in BF_BRAIN"):
        sensor.secret(token)


def test_fetch_stays_on_one_https_host() -> None:
    sensor = kit()
    for url in (
        "http://api.example.test/",
        "file:///etc/passwd",
        "".join(("https://user:", "pw@api.example.test/")),
        "https://",
    ):
        with pytest.raises(sensor.SensorError, match="HTTPS URL without credentials"):
            sensor.fetch(url, timeout=1, max_bytes=1024)
    guard = sensor._SameHostHttps()  # noqa: SLF001 - the redirect policy itself
    request = urllib.request.Request("https://api.example.test/v1/items", headers={"Authorization": "Bearer t"})
    allowed = guard.redirect_request(request, None, 302, "Found", {}, "https://api.example.test/v2/items")
    assert allowed.full_url == "https://api.example.test/v2/items"
    for target in (
        "http://api.example.test/v2/items",
        "https://evil.example.test/v2/items",
        "https://api.example.test:8443/v2/items",
        "https://user@api.example.test/v2/items",
        "ftp://api.example.test/",
    ):
        with pytest.raises(sensor.SensorError, match="redirected outside"):
            guard.redirect_request(request, None, 302, "Found", {}, target)


class _Plain(http.client.HTTPConnection):
    """Plain HTTP in place of HTTPS, so the body-read bounds run against a local server without a certificate."""

    def __init__(self, host: str, **options: Any) -> None:
        for tls in ("context", "check_hostname"):
            options.pop(tls, None)
        super().__init__(host, **options)


def test_fetch_bounds_the_body_and_names_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    done = threading.Event()
    bodies = {"/ok": b'{"ok": true}', "/bad": b"not json", "/denied": b"denied", "/huge": b"x" * 4096}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = bodies.get(self.path, b"")
            self.send_response(401 if self.path == "/denied" else 200)
            self.send_header("Content-Length", str(len(body) or 100))
            self.end_headers()
            with suppress(OSError):
                self.wfile.write(body)
                # Otherwise one byte every 0.1 s: each socket read succeeds, but the body never ends in time.
                while not body and not done.wait(0.1):
                    self.wfile.write(b"x")
                    self.wfile.flush()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the overridden signature
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", _Plain)
    sensor = kit()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        assert sensor.fetch_json(f"{base}/ok", timeout=5, max_bytes=1024) == {"ok": True}
        with pytest.raises(sensor.SensorError, match="answered invalid JSON"):
            sensor.fetch_json(f"{base}/bad", timeout=5, max_bytes=1024)
        with pytest.raises(sensor.SensorError, match="answered HTTP 401; check the token and URL"):
            sensor.fetch(f"{base}/denied", timeout=5, max_bytes=1024)
        with pytest.raises(sensor.SensorError, match="answered more than 1024 bytes"):
            sensor.fetch(f"{base}/huge", timeout=5, max_bytes=1024)
        started = time.monotonic()
        with pytest.raises(sensor.SensorError, match="exceeded the 1s timeout"):
            sensor.fetch(f"{base}/trickle", timeout=1, max_bytes=1 << 20)
        assert time.monotonic() - started < 5
    finally:
        done.set()
        server.shutdown()
        server.server_close()


def test_identities_are_lowercase_encoded_or_empty() -> None:
    sensor = kit()
    assert sensor.person(" Alice.O'Neil+tag@Example.TEST ") == "person:email/alice.o%27neil+tag@example.test"
    assert sensor.person("a~b@example.test") == "person:email/a%7Eb@example.test"
    assert sensor.account("GitHub.com", "Octo-Cat") == "person:github.com/octo-cat"
    assert sensor.repo("github.com", "FMind", "Brain-Framework") == "repo:github.com/fmind/brain-framework"
    assert sensor.repo("gitlab.example.test", "group/sub", "app") == "repo:gitlab.example.test/group%2Fsub/app"
    for value in (sensor.person("no-at-sign"), sensor.person("a\n@example.test"), sensor.person("a@b@example.test")):
        assert value == ""
    assert sensor.repo("github.com", "", "x") == sensor.account("github.com", "a\x00b") == ""
    assert sensor.person("a" * 9000 + "@example.test") == ""


def test_record_normalizes_what_bf_would_reject() -> None:
    sensor = kit()
    refs = [f"person:email/u{index:04d}@example.test" for index in range(1200)]
    item = sensor.record(
        "plans/2026",
        "Plan\x07\n review " + "x" * 5000,
        text="y" * ((4 << 20) + 1),
        time="2026-09-01T12:00:00+02:00",
        url="https://example.test/\x85plan",
        links=[*refs, "not an identity", "bad:\u200bhidden", "", "x:" + "a" * 9000],
        aliases=["https://example.test/plan", "Display Name", "person:email/a@example.test"],
        attributes={"sender_refs": [refs[0], refs[0], "plain", None], "updated": "yesterday", "observed": START},
    )
    record = Record.model_validate(item)
    assert record.title.startswith("Plan review xxx")
    assert len(record.title) == 4096
    assert (record.url, record.time, record.attributes["partial"]) == ("", "2026-09-01T10:00:00.000000Z", True)
    assert len(record.text) == 4 << 20
    assert record.links == sorted(refs[:1000])
    assert record.aliases == ["https://example.test/plan", "person:email/a@example.test"]
    assert record.attributes["sender_refs"] == [refs[0]]
    assert "updated" not in record.attributes
    assert record.observed == START
    naive = sensor.record("t", "T", time=datetime(2026, 9, 1), attributes={"kind": ["x"], "status": "a\nb"})
    assert (naive["time"], naive["attributes"]) == ("", {})
    huge = sensor.record("many", "Many", attributes={"participant_refs": refs})
    assert huge["attributes"]["participant_refs"] == refs[:1000]
    # A lone surrogate, as JSON decodes a string cut inside a UTF-16 pair, becomes U+FFFD outside the id.
    torn = sensor.record(
        "s",
        "Launch \ud83d",
        text="\ud83d",
        url="https://example.test/\udc00",
        links=["person:email/\ud83d@example.test"],
        attributes={"n": ["\udc00"], "\ud83d": "x"},
    )
    loaded = Record.model_validate(torn)
    assert (loaded.title, loaded.text, loaded.url, loaded.links) == ("Launch \ufffd", "\ufffd", "", [])
    assert torn["attributes"] == {"n": ["\ufffd"], "\ufffd": "x"}
    for bad in ("", " ", "a\x00b", "x" * 4097, "é" * 2000, "a\ud83d"):
        with pytest.raises(sensor.SensorError, match="record id"):
            sensor.record(bad, "Title")
    with pytest.raises(sensor.SensorError, match="list of identities"):
        sensor.record("a", "A", attributes={"owner_refs": "person:email/a@example.test"})
    with pytest.raises(sensor.SensorError, match="partial"):
        sensor.record("a", "A", attributes={"partial": "yes"})
    with pytest.raises(sensor.SensorError, match="2 MiB"):
        sensor.record("a", "A", attributes={"bulk": "z" * (2 << 20)})
    # Multi-byte UTF-8 text exceeding 4 MiB in UTF-8 bytes (while < 4M codepoints) is truncated cleanly at codepoint boundary.
    multibyte = sensor.record("mb", "Multibyte", text="é" * ((2 << 20) + 50))
    mb_record = Record.model_validate(multibyte)
    assert len(mb_record.text.encode("utf-8")) <= 4 << 20
    assert mb_record.attributes["partial"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abcdefghijklmnop1234", "Authorization: Bearer [REDACTED]"),
        (f"use {SECRET} here", "use [REDACTED] here"),
        ("pat github_pat_11ABCDEFG0123456789_abcdefghijklmnop", "pat [REDACTED]"),
        ("key AKIAIOSFODNN7EXAMPLE end", "key [REDACTED] end"),
        ("".join(("aws_secret_access_key = ", "wJalrXUtnFEMI/K7MDENG")), "aws_secret_access_key = [REDACTED]"),
        ("".join(('{"password": ', '"hunter2", "user": "ann"}')), '{"password": [REDACTED], "user": "ann"}'),
        ("".join(("DB_PASSWORD=", "s3cr3t;next=1")), "DB_PASSWORD=[REDACTED];next=1"),
        (f"-----BEGIN RSA {KEY}\nMIIE\n-----END RSA {KEY}\nafter", "[REDACTED KEY]\nafter"),
        (f"-----BEGIN OPENSSH {KEY}\ncut off", "[REDACTED KEY]"),
        ("The password policy changed; tokens rotate weekly.", "The password policy changed; tokens rotate weekly."),
    ],
)
def test_redact_masks_common_credential_shapes(text: str, expected: str) -> None:
    assert kit().redact(text) == expected


def test_redact_stays_linear_on_adversarial_text() -> None:
    redact = kit().redact
    # Sizes keep each small measurement near 20 ms, well above timer noise on a loaded runner.
    for unit, size in (("token", 80_000), ("-eyJ", 40_000), ("xoxb-", 400_000), ("password.", 80_000)):
        assert_linear_time(lambda n, unit=unit: redact(unit * n), size)
    assert_linear_time(lambda n: redact(f"-----BEGIN {KEY}" * n), 200_000)


def test_kit_imports_quietly(tmp_path: Path) -> None:
    # Importing the kit prints nothing and needs no environment: a sensor decides when to call a provider.
    result = driver(tmp_path, "")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_load_and_save_state_are_atomic_bounded_and_refuse_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensor = kit()
    state = tmp_path / "state"
    monkeypatch.setenv("BF_STATE_DIR", str(state))
    default = {"cursor": None, "seen": []}
    loaded = sensor.load_state("cursor.json", default)
    assert loaded == default
    loaded["seen"].append("a")
    assert default["seen"] == []
    sensor.save_state("cursor.json", {"cursor": "tok-1", "seen": ["a", "b"]})
    assert sensor.load_state("cursor.json", default) == {"cursor": "tok-1", "seen": ["a", "b"]}
    assert not list(state.glob("*.tmp"))
    for bad_name in ("", ".", "..", "sub/cursor.json", "a\\b", "bad\x00.json"):
        with pytest.raises(sensor.InvalidError, match="state file name"):
            sensor.load_state(bad_name)
    with pytest.raises(sensor.InvalidError, match="value JSON cannot represent"):
        sensor.save_state("nan.json", float("nan"))
    with pytest.raises(sensor.InvalidError, match="exceeds 8 bytes"):
        sensor.save_state("big.json", {"cursor": "toolong"}, max_bytes=8)
    (state / "damaged.json").write_text("{broken")
    with pytest.raises(sensor.InvalidError, match="damaged"):
        sensor.load_state("damaged.json")
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": "' + SECRET + '"}')
    (state / "linked.json").symlink_to(outside)
    with pytest.raises(sensor.InvalidError, match=r"cannot read state file linked\.json"):
        sensor.load_state("linked.json")


def test_cli_handles_known_and_unexpected_failures_without_echoing_payloads(tmp_path: Path) -> None:
    for body, expected in (
        (
            "_sensor.cli(lambda: (_ for _ in ()).throw(_sensor.InvalidError('START must precede END')), 'Demo failed')\n",
            "Demo failed: START must precede END.\n",
        ),
        (
            "_sensor.cli(lambda: (_ for _ in ()).throw(_sensor.ProviderError('gh failed', status=1)), 'Demo failed')\n",
            "Demo failed: gh failed.\n",
        ),
        (
            f"_sensor.cli(lambda: (_ for _ in ()).throw(OSError('private {SECRET}')), 'Demo failed', 'check access')\n",
            "Demo failed; check access.\n",
        ),
        (
            f"_sensor.cli(lambda: (_ for _ in ()).throw(KeyError('private {SECRET}')), 'Demo failed', 'check access')\n",
            "Demo failed; check access.\n",
        ),
    ):
        result = driver(tmp_path, body)
        failed(result, SECRET, "private")
        assert result.stderr == expected
    # argparse exits 2 via SystemExit: cli() must let SystemExit pass through unchanged.
    arg_error = driver(tmp_path, "_sensor.cli(lambda: (_ for _ in ()).throw(SystemExit(2)))\n")
    assert arg_error.returncode == 2
