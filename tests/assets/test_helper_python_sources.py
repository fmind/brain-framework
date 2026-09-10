"""Portable collectors preserve projection, completeness and private-data boundaries."""

from __future__ import annotations

import json
import os
import runpy
from typing import Any

import pytest

from .conftest import HelperInstallation

START = "2026-05-04T00:00:00Z"
END = "2026-05-05T00:00:00Z"
PROJECTIONS = (
    ("gcloud-projects-json.py", "gcloud", []),
    ("github-gists-json.py", "github-generic-list-json.py", []),
    ("github-stars-json.py", "github-generic-list-json.py", []),
    ("kaggle-competitions-json.py", "kaggle-json.py", []),
    ("kaggle-datasets-json.py", "kaggle-json.py", []),
    ("mise-tools-json.py", "mise", []),
    ("gws-doc-text.py", "gws", ["example-document"]),
)


@pytest.mark.parametrize(("helper", "provider", "arguments"), PROJECTIONS)
@pytest.mark.parametrize("failure", ["partial", "invalid", "shape", "nonfinite"])
def test_projection_failures_are_atomic_and_do_not_echo_provider_data(
    helpers: HelperInstallation,
    helper: str,
    provider: str,
    arguments: list[str],
    failure: str,
) -> None:
    helpers.fake(
        provider,
        """printf '%s\\n' 'PRIVATE_PROVIDER_DIAGNOSTIC' >&2
case "$FAILURE" in
  partial) printf '%s\\n' '[]'; exit 9 ;;
  invalid) printf '%s\\n' 'PRIVATE_INVALID_PAYLOAD' ;;
  shape) printf '%s\\n' '17' ;;
  nonfinite) printf '%s\\n' '[NaN]' ;;
esac
""",
    )
    result = helpers.run(helper, *arguments, environment={"FAILURE": failure})
    assert result.returncode != 0
    assert result.stdout == b""
    assert b"PRIVATE" not in result.stderr


@pytest.mark.parametrize("helper", ["kaggle-competitions-json.py", "kaggle-datasets-json.py"])
def test_kaggle_refs_are_sorted_and_duplicate_or_empty_refs_fail(helpers: HelperInstallation, helper: str) -> None:
    helpers.fake("kaggle-json.py", 'printf "%s\\n" "$PAYLOAD"\n')
    good = helpers.run(helper, environment={"PAYLOAD": '[{"ref":"owner/z"},{"ref":"owner/a","title":""}]'})
    assert good.returncode == 0
    records = json.loads(good.stdout)
    assert [record["ref"] for record in records] == ["owner/a", "owner/z"]
    if helper == "kaggle-datasets-json.py":
        assert [record["title"] for record in records] == ["", "owner/z"]
    for payload in ('[{"ref":"owner/a"},{"ref":"owner/a"}]', '[{"ref":""}]', '[{"ref":null}]'):
        bad = helpers.run(helper, environment={"PAYLOAD": payload})
        assert bad.returncode == 1
        assert bad.stdout == b""


def test_projects_project_only_reviewed_metadata_and_reject_overflow(helpers: HelperInstallation) -> None:
    helpers.fake("gcloud", 'printf "%s\\n" "$PAYLOAD"\n')
    result = helpers.run(
        "gcloud-projects-json.py",
        environment={
            "PAYLOAD": '[{"projectId":"example-project","name":null,"parent":{"id":"42","type":"folder","private":"omit"},"private":"omit"}]'
        },
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == [{"projectId": "example-project", "parent": {"id": "42", "type": "folder"}}]
    # The overflow fixture stays in a file so argv/environment size never limits the test.
    payload = helpers.root / "projects.json"
    payload.write_text(json.dumps([{"projectId": "example-project"}] * 10_001))
    helpers.fake("gcloud", 'cat "$PAYLOAD_FILE"\n')
    bad = helpers.run("gcloud-projects-json.py", environment={"PAYLOAD_FILE": os.fspath(payload)})
    assert bad.returncode == 1
    assert bad.stdout == b""


def test_mise_projects_version_sets_and_empty_installations(helpers: HelperInstallation) -> None:
    helpers.fake("mise", 'printf "%s\\n" "$PAYLOAD"\n')
    payload = {
        "python": [
            {"version": "3.14.0", "source": {"path": "synthetic.toml"}, "installed": False},
            {"installed": True, "active": True},
        ],
        "empty": [],
    }
    result = helpers.run("mise-tools-json.py", environment={"PAYLOAD": json.dumps(payload)})
    assert result.returncode == 0
    records = json.loads(result.stdout)
    assert records[0] == {
        "id": "python",
        "version": "3.14.0",
        "requested": None,
        "source": "synthetic.toml",
        "installed": True,
        "active": True,
        "title": "python 3.14.0",
    }
    assert records[1]["title"] == "empty ?"
    assert records[1]["installed"] is False


def test_docs_preserve_nested_text_order_and_opaque_document_argument(helpers: HelperInstallation) -> None:
    calls = helpers.root / "params.json"
    helpers.fake("gws", 'printf "%s" "$5" > "$CALLS"\nprintf "%s\\n" "$PAYLOAD"\n')
    payload = {
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Heading\n"}}]}},
                {"table": {"rows": [{"textRun": {"content": "Cell"}}]}},
            ]
        },
        "footer": {"textRun": {"content": "Footer"}},
    }
    result = helpers.run(
        "gws-doc-text.py",
        "document with spaces",
        environment={"CALLS": os.fspath(calls), "PAYLOAD": json.dumps(payload)},
    )
    assert result.returncode == 0
    assert result.stdout == b"Heading\nCellFooter\n"
    assert json.loads(calls.read_text()) == {"documentId": "document with spaces"}


def test_writing_roots_are_explicit_and_articles_supersede_drafts(helpers: HelperInstallation) -> None:
    articles = helpers.home / "published articles"
    packages = helpers.home / "draft packages"
    articles.mkdir()
    (packages / "developed").mkdir(parents=True)
    (packages / "draft-only").mkdir()
    for path in (
        articles / "post.md",
        packages / "developed/article.md",
        packages / "developed/draft.txt",
        packages / "draft-only/draft.txt",
    ):
        path.write_text("PRIVATE_DOCUMENT_BODY")
    (articles / ".hidden.md").write_text("PRIVATE_HIDDEN_ARTICLE")
    (packages / ".hidden-package").mkdir()
    (packages / ".hidden-package/article.md").write_text("PRIVATE_HIDDEN_PACKAGE")
    helpers.fake("writing-index.py", 'printf "%s\\n" "$@"\n')
    result = helpers.run(
        "writing-source-json.py", "--articles-root", os.fspath(articles), "--packages-root", os.fspath(packages)
    )
    assert result.returncode == 0
    selected = result.stdout.decode().splitlines()
    assert len(selected) == 3
    assert os.fspath(packages / "developed/draft.txt") not in selected
    assert os.fspath(packages / "developed/article.md") in selected
    # Forward duplicates so the metadata indexer retains its duplicate-ID rejection.
    document = os.fspath(articles / "post.md")
    duplicates = helpers.run("writing-source-json.py", document, document)
    assert duplicates.stdout.decode().splitlines() == [document, document]
    for arguments in (
        [],
        ["--articles-root", os.fspath(helpers.home / "missing-private-path")],
        ["--unexpected-private-value"],
    ):
        bad = helpers.run("writing-source-json.py", *arguments)
        assert bad.returncode != 0
        assert bad.stdout == b""
        assert b"private" not in bad.stderr.lower()
        assert os.fsencode(helpers.home) not in bad.stderr


def test_drive_projection_rejects_incomplete_pages_and_drops_unreviewed_fields(helpers: HelperInstallation) -> None:
    payload = {
        "files": [
            {
                "id": "example-file",
                "name": "Example",
                "owners": [{"emailAddress": "OWNER@EXAMPLE.TEST", "private": "omit"}],
                "private": "omit",
            }
        ]
    }
    result = helpers.run("gws-drive-files-json.py", stdin=json.dumps(payload))
    assert result.returncode == 0
    record = json.loads(result.stdout)["files"][0]
    assert record["owners"][0]["uri"] == "person:email/owner@example.test"
    assert b"private" not in result.stdout
    for bad in ({"files": [], "nextPageToken": "unfinished"}, {"files": "wrong"}):
        result = helpers.run("gws-drive-files-json.py", stdin=json.dumps(bad))
        assert result.returncode == 1
        assert result.stdout == b""


def test_audit_exclusions_are_explicit_and_provider_failures_remain_private(helpers: HelperInstallation) -> None:
    calls = helpers.root / "calls"
    helpers.fake(
        "gcloud",
        """printf '%s\\n' "$*" >> "$CALLS"
case "$*" in
  projects*) printf '%s\\n' '[{"projectId":"example-project"}]' ;;
  logging*)
    if [ "${FAIL_LOGS:-}" = 1 ]; then printf '%s\\n' 'PRIVATE_PROVIDER_ERROR' >&2; exit 9; fi
    printf '%s\\n' '[{"insertId":"entry","timestamp":"2026-05-04T09:00:00Z","resource":{"labels":{"project_id":"example-project"}},"protoPayload":{"methodName":"create","request":{"private":"omit"}}}]' ;;
  *) exit 64 ;;
esac
""",
    )
    arguments = [START, END, "--exclude-project", "example-excluded", "--exclude-project-prefix", "example-prefix-"]
    result = helpers.run("gcloud-audit-projects-json.py", *arguments, environment={"CALLS": os.fspath(calls)})
    assert result.returncode == 0
    assert "--filter=NOT projectId = example-excluded AND NOT projectId ~ ^example-prefix-" in calls.read_text()
    assert "--limit=100001" in calls.read_text()
    assert b"private" not in result.stdout
    bad = helpers.run(
        "gcloud-audit-projects-json.py", *arguments, environment={"CALLS": os.fspath(calls), "FAIL_LOGS": "1"}
    )
    assert bad.returncode == 1
    assert bad.stdout == b""
    assert b"PRIVATE" not in bad.stderr
    for extra in (["--exclude-project", "bad OR projectId:*"], ["--exclude-project-prefix", "bad.*"]):
        bad = helpers.run("gcloud-audit-projects-json.py", START, END, *extra)
        assert bad.returncode == 2
        assert bad.stdout == b""
    calls.unlink()
    result = helpers.run("gcloud-audit-projects-json.py", START, END, environment={"CALLS": os.fspath(calls)})
    assert result.returncode == 0
    assert "--filter" not in calls.read_text().splitlines()[0]


def test_billing_collects_links_without_unreviewed_provider_fields(helpers: HelperInstallation) -> None:
    helpers.fake(
        "gcloud",
        """case "$*" in
  'billing accounts list'*) printf '%s\\n' '[{"name":"billingAccounts/ABC123-DEF456-GHI789"}]' ;;
  'billing projects list'*) printf '%s\\n' '[{"projectId":"example-project","billingAccountName":"billingAccounts/ABC123-DEF456-GHI789","billingEnabled":true,"private":"omit"}]' ;;
  *) exit 64 ;;
esac
""",
    )
    result = helpers.run("gcloud-billing-json.py")
    assert result.returncode == 0
    assert json.loads(result.stdout) == [
        {
            "projectId": "example-project",
            "billingAccountName": "billingAccounts/ABC123-DEF456-GHI789",
            "billingEnabled": True,
        }
    ]
    helpers.fake("gcloud", "printf '%s\\n' 'PRIVATE_PROVIDER_ERROR' >&2; exit 9\n")
    bad = helpers.run("gcloud-billing-json.py")
    assert bad.returncode == 1
    assert bad.stdout == b""
    assert b"PRIVATE" not in bad.stderr


def test_developer_feed_resolves_date_only_articles_and_refuses_external_urls(helpers: HelperInstallation) -> None:
    feed = helpers.root / "feed.xml"
    feed.write_text(
        "<rss><channel><item><guid>new</guid><title>New</title><link>https://developers.googleblog.com/new/</link></item><item><guid>old</guid><title>Old</title><link>https://developers.googleblog.com/old/</link></item></channel></rss>"
    )
    helpers.fake(
        "curl",
        """case "$*" in
  *'/feed/') cat "$FEED_FILE" ;;
  *'/new/') printf '%s\\n' '{"datePublished":"2026-05-04"}' ;;
  *'/old/') printf '%s\\n' '{"datePublished":"2026-05-03"}' ;;
  *) exit 64 ;;
esac
""",
    )
    result = helpers.run("google-developers-json.py", START, END, environment={"FEED_FILE": os.fspath(feed)})
    assert result.returncode == 0
    records = json.loads(result.stdout)
    assert len(records) == 1
    assert records[0]["time"] == "2026-05-04T12:00:00Z"
    assert records[0]["time_precision"] == "date"
    feed.write_text(
        feed.read_text().replace("https://developers.googleblog.com/new/", "https://outside.example.test/new/")
    )
    bad = helpers.run("google-developers-json.py", START, END, environment={"FEED_FILE": os.fspath(feed)})
    assert bad.returncode == 1
    assert bad.stdout == b""


@pytest.mark.parametrize(("helper", "provider", "arguments"), PROJECTIONS)
def test_python_projection_bounds_provider_reads_before_output(
    helpers: HelperInstallation,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    helper: str,
    provider: str,
    arguments: list[str],
) -> None:
    namespace: dict[str, Any] = runpy.run_path(os.fspath(helpers.bin / helper))
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "MAX_PROVIDER_BYTES", 2)
    helpers.fake(provider, 'printf "%s\\n" "[1,2,3]"\n')
    monkeypatch.setenv("PATH", helpers.environment()["PATH"])
    assert main(arguments) == 1
    captured = capfd.readouterr()
    assert captured.out == ""
