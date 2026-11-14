"""Editor schemas and runtime validation reject malformed configuration without executing it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import markdown
from bf.cli import app
from bf.collect import collect
from bf.config import load, user_path, yaml_object
from bf.evaluate import Case, Suite, evaluate
from bf.models import Config, Error, Registration, UserConfig
from bf.schemas import Kind, document
from bf.storage import Store
from conftest import plain

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "value",
    [
        {"version": 10, "name": "brain\n"},
        {"version": 10, "name": "brain", "jobs": {"Invalid Name": {"nonsense": True}}},
        {"version": 10, "name": "brain", "jobs": {"demo\n": {"run": ["echo"]}}},
        {"version": 10, "name": "brain", "jobs": {"bad_slug": {"run": ["echo"]}}},
        {"version": 10, "name": "brain", "brains": {"team": {"path": "../team"}}},
        {"version": 10, "name": "brain", "fields": {"Invalid Name": {"description": "Kind"}}},
        {"version": 10, "name": "brain", "fields": {"owner": {"description": "Owner", "type": "identity"}}},
        *(
            {"version": 10, "name": "brain", "fields": {"owner": {"description": "Owner", **extra}}}
            for extra in (
                {"targets": []},
                {"targets": ["Repo:"]},
                {"targets": ["repo:\n"]},
                {"targets": ["repo"]},
                {"broader": "Owner"},
            )
        ),
        {"version": 10, "name": "brain", "jobs": {"demo": {"run": ["echo"], "emit": "invalid"}}},
        *(
            {"version": 10, "name": "brain", "watch": watch}
            for watch in (
                [],
                {"interval": 0},
                {"interval": "60"},
                {"poll_interval": 61},
                {"notifications": False},
                {"notifications": "email"},
                {"notification_cooldown": -1},
                {"intervall": 60},
            )
        ),
        {"name": "brain"},
        {"version": 9, "name": "brain"},
        *(
            {"version": 10, "name": "brain", "jobs": {"demo": {"run": [executable]}}}
            for executable in ("/usr/bin/env", "../escape.sh", "sensors/../x", "sensors//x", "bin/x", ".", "a{b")
        ),
        {"version": 10, "name": "brain", "jobs": {"digest": {"run": ["routines/./digest.py"]}}},
        {"version": 10, "name": "brain", "jobs": []},
    ],
)
def test_editor_and_runtime_reject_invalid_structure(value: dict) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(value)
    assert not Draft202012Validator(Config.model_json_schema()).is_valid(value)


@pytest.mark.parametrize(
    ("kind", "data", "expected"),
    [
        *(
            ("brain", f"version: 10\nname: brain\n{key}:\n  # demo: {{}}\n".encode(), {"version": 10, "name": "brain"})
            for key in ("fields", "defaults", "watch", "validate", "jobs")
        ),
        (
            "brain",
            (
                b"version: 10\nname: brain\njobs:\n  helper:\n    prompt: prompts/helper.md\n    harness: [echo]\n"
                b"    on:\n      # every: 3600\n"
            ),
            {
                "version": 10,
                "name": "brain",
                "jobs": {"helper": {"prompt": "prompts/helper.md", "harness": ["echo"]}},
            },
        ),
        ("registry", b"brains:\n  # brain:\n  #   path: ~/brain\n", {}),
    ],
)
def test_a_mapping_whose_entries_are_all_commented_out_is_empty(kind: Kind, data: bytes, expected: dict) -> None:
    # YAML reads a key whose entries are all commented out as null, which the strict models once rejected.
    value = yaml_object(data)
    model = {"brain": Config, "registry": UserConfig}[kind].model_validate(value)
    assert model.model_dump(exclude_defaults=True) == expected
    Draft202012Validator(document(kind)).validate(value)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            {"fields": {"owner": {"targets": ["Repo:"]}}},
            "fields.owner.targets.0",
        ),
        (
            {"fields": {"owner": {"broader": "absent"}}},
            "fields.owner.broader: name another declared relation without its own broader",
        ),
        (
            {
                "jobs": {
                    "a": {"run": ["echo"], "emit": "records", "source": "shared"},
                    "b": {"run": ["echo"], "emit": "records", "source": "shared"},
                }
            },
            "jobs.b: record source shared is already used by jobs.a",
        ),
        (
            {
                "jobs": {
                    "demo": {
                        "run": ["echo"],
                        "emit": "records",
                        "mode": "snapshot",
                        "reconcile": {"every": 1, "lookback": 1},
                    }
                }
            },
            "jobs.demo: reconcile requires emit: records and mode: window",
        ),
    ],
    ids=["target-prefix", "missing-broader", "record-source", "snapshot-reconcile"],
)
def test_invalid_literal_values_and_shared_names_are_located(brain: Store, value: dict, message: str) -> None:
    # JSON is YAML: the configuration names the key to fix among several fields or jobs.
    brain.write("bf.yaml", json.dumps({"version": 10, "name": "fixture", **value}).encode())
    with pytest.raises(Error, match=rf"^invalid bf\.yaml: {re.escape(message)}"):
        load(brain)


@pytest.mark.parametrize(
    "legacy",
    [
        {"fields": {"kind": {"description": "Kind", "type": "string"}}},
        {"fields": {"owner": {"description": "Owner", "cardinality": "one"}}},
        {"fields": {"owner": {"description": "Owner", "relation": True}}},
        {"fields": {"owner": {"description": "Owner", "examples": ["person:alice"]}}},
        {"fields": {"owner": {"description": "Owner", "path": "/attributes/owner"}}},
        {"jobs": {"demo": {"run": ["echo"], "fields": {"owner": {"path": "/a"}}}}},
        {"sensors": {}},
        {"routines": {}},
        {"assistants": {}},
        {"channels": {}},
    ],
)
def test_editor_rejects_legacy_field_and_mapping_keys(legacy: dict) -> None:
    value = {"version": 10, "name": "brain", **legacy}
    with pytest.raises(ValidationError):
        Config.model_validate(value)
    assert not Draft202012Validator(Config.model_json_schema()).is_valid(value)


@pytest.mark.parametrize("field", ["name", "expect", "forbid", "text"])
@pytest.mark.parametrize("blank", ["", " \t\n"])
def test_evaluation_rejects_blank_labels_and_assertions(brain: Store, field: str, blank: str) -> None:
    case = {"name": "missing-evidence", "query": "absentneedle947ab3", "text": ["required evidence"]}
    case[field] = blank if field == "name" else [blank]
    brain.write("evals/retrieval.yaml", json.dumps({"version": 10, "cases": [case]}).encode())
    with pytest.raises(Error, match=r"evals/retrieval.yaml"):
        evaluate(brain)


@pytest.mark.parametrize(
    "value",
    [b"!!bool PRIVATE", b'!!int ""', b'!!float ""', b"!!map [PRIVATE]", b"!!python/object/apply:os.system [PRIVATE]"],
)
def test_tagged_yaml_failures_are_safe(value: bytes) -> None:
    with pytest.raises(Error, match=r"^bf.yaml: invalid YAML") as failure:
        yaml_object(b"private-key: " + value, "bf.yaml")
    assert "PRIVATE" not in str(failure.value)
    assert "private-key" not in str(failure.value)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "latin-1"])
def test_yaml_other_than_utf8_is_named_not_decoded(encoding: str) -> None:
    # PyYAML would read UTF-16 or UTF-32 with a byte-order mark; brain files and the registry are UTF-8 only.
    with pytest.raises(Error, match=r"^config.yaml: YAML is not UTF-8$"):
        yaml_object("brains: {café: {path: ~/brain}}\n".encode(encoding), "config.yaml")
    assert yaml_object("\ufeffname: café\n".encode(), "bf.yaml") == {"name": "café"}


LIBYAML = (
    r"reading YAML needs PyYAML built with libyaml, which this installation lacks; .*: "
    r"uv tool install --reinstall --python 3\.14 brain-framework, or 'brain-framework\[mcp\]' with MCP$"
)


def test_yaml_is_read_only_through_libyaml(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # PyYAML built without libyaml lacks CSafeLoader: its pure-Python parser is slower and rejects some YAML libyaml
    # accepts, so BF names the reinstall instead of loading a file differently.
    brain.write("projects/plan.md", b"---\ntype: project\n---\n# Plan\n")
    brain.write(
        "evals/retrieval.yaml", b"version: 10\ncases: [{name: plan, query: plan, expect: [projects/plan.md]}]\n"
    )
    monkeypatch.delattr(yaml, "CSafeLoader")
    # A configuration this process already validated is reused by its digest: drop them, as a new process would.
    monkeypatch.setattr("bf.config._LOADED", {})
    # bf.yaml, the registry, eval suites and note frontmatter all parse through yaml_object and name their file.
    for name, data, kwargs in (
        ("bf.yaml", b"version: 10\nname: brain\n", {}),
        ("config.yaml", b"brains: {}\n", {}),
        ("evals/retrieval.yaml", b"version: 10\n", {}),
        ("projects/plan.md", b"type: project\n", {"line": 2}),
    ):
        with pytest.raises(Error, match=rf"^{re.escape(name)}: {LIBYAML}"):
            yaml_object(data, name, **kwargs)
    for arguments, prefix in (
        (["search", "plan"], "bf.yaml"),
        (["validate"], "bf.yaml"),
        (["eval"], "evals/retrieval.yaml"),
    ):
        result = CliRunner().invoke(app, [*arguments, "--brain", str(brain.root)])
        assert result.exit_code == 1, arguments
        assert re.match(rf"^{re.escape(prefix)}: {LIBYAML}", str(result.exception)), arguments
    with pytest.raises(Error, match=rf"^projects/plan\.md: {LIBYAML}"):
        markdown.parse("projects/plan.md", brain.read("projects/plan.md"))


def test_a_pyyaml_without_libyaml_fails_with_one_diagnostic(brain: Store) -> None:
    # A fresh process imports BF after removing the C loader, as a PyYAML built without libyaml installs: BF still
    # imports and prints its version, and each command reading YAML names the fix on one line.
    program = """
import sys, yaml
del yaml.CSafeLoader
from bf.cli import main
sys.argv = ["bf", *sys.argv[1:]]
main()
"""
    for arguments, code in ((["--version"], 0), (["validate", "--brain", str(brain.root)], 1)):
        result = subprocess.run(  # noqa: S603 - fresh process selects the installed parser before importing BF
            [sys.executable, "-c", program, *arguments], capture_output=True, text=True, timeout=120, check=False
        )
        assert result.returncode == code, result.stderr
        if code:
            assert not result.stdout
            assert len(result.stderr.splitlines()) == 1
            assert re.match(rf"^bf\.yaml: {LIBYAML}", result.stderr.removeprefix("bf: ").rstrip("\n"))


def test_no_program_is_named_bf(brain: Store) -> None:
    # A job's name (or explicit record source) is its records' source, whose `bf:ID` refs would read as BF addresses.
    brain.write("bf.yaml", b"version: 10\nname: fixture\njobs:\n  bf: {run: [echo]}\n")
    with pytest.raises(
        Error, match=r"^invalid bf\.yaml: jobs\.bf: the name bf is reserved for BF addresses; rename the job$"
    ):
        load(brain)
    result = CliRunner().invoke(app, ["validate", "--brain", str(brain.root)])
    assert result.exit_code == 1
    assert "jobs.bf: the name bf is reserved" in str(result.exception)
    brain.write("bf.yaml", b"version: 10\nname: fixture\njobs:\n  mail: {run: [echo], emit: records, source: bf}\n")
    with pytest.raises(Error, match=r"^invalid bf\.yaml: jobs\.mail\.source: bf is reserved for BF addresses$"):
        load(brain)
    # Only the exact name: a longer one starting with bf stays valid.
    for name in ("bfx", "bf-notes"):
        brain.write("bf.yaml", f"version: 10\nname: fixture\njobs:\n  {name}: {{run: [echo]}}\n".encode())
        assert name in load(brain).jobs


def test_starter_writes_minimal_config_without_boilerplate(tmp_path: Path) -> None:
    root = tmp_path / "new"
    result = CliRunner().invoke(app, ["init", str(root), "--name", "new"])
    assert result.exit_code == 0, result.output
    text = (root / "bf.yaml").read_text()
    document = yaml_object(text.encode())
    assert document == {"version": 10, "name": "new"}
    assert "bf.schema.json" in text
    config = load(Store(root))
    assert not config.ontology
    assert not config.jobs


@pytest.mark.parametrize("kind", get_args(Kind))
def test_published_schemas_match_offline_cli_and_have_only_local_references(kind: Kind) -> None:
    registry = user_path()
    registry.parent.mkdir(parents=True)
    registry.write_text("private-key: [invalid\n")
    result = CliRunner().invoke(app, ["schema", "--kind", kind])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.stdout)
    assert schema == document(kind)
    assert schema == json.loads((ROOT / "docs" / f"{'bf' if kind == 'brain' else kind}.schema.json").read_bytes())
    Draft202012Validator.check_schema(schema)

    def local(value: object) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/")
            for child in value.values():
                local(child)
        elif isinstance(value, list):
            for child in value:
                local(child)

    local(schema)
    if kind == "brain":
        assert json.loads(CliRunner().invoke(app, ["schema"]).stdout) == schema


@pytest.mark.parametrize("path", sorted((ROOT / "examples").rglob("bf.yaml")), ids=lambda p: p.parent.name)
def test_example_brains_pass_runtime_and_editor_validation(path: Path) -> None:
    value = yaml_object(path.read_bytes())
    Config.model_validate(value)
    Draft202012Validator(document()).validate(value)


@pytest.mark.parametrize(
    ("case", "valid"),
    [
        ({"query": "word", "text": ["answer"]}, True),
        ({"query": "absent", "empty": True, "forbid": ["projects/secret.md"]}, True),
        ({"read": "", "text": ["brain"]}, True),
        ({"read": "projects/missing.md", "empty": True}, True),
        ({"query": "word", "read": None, "text": ["answer"]}, True),
        ({"read": "", "query": "", "scope": "", "text": ["brain"]}, True),
        ({"query": "word", "text": [""]}, False),
        ({"query": "word", "text": [" \t\n"]}, False),
        ({"query": "word", "expect": [""]}, False),
        ({"query": "word", "empty": True, "forbid": [""]}, False),
        ({"query": "word", "read": "", "text": ["answer"]}, False),
        ({"read": "", "limit": 10, "text": ["brain"]}, False),
        ({"read": "", "scope": "projects", "text": ["brain"]}, False),
        ({"read": "", "empty": True, "expect": ["projects/one.md"]}, False),
        ({"query": "word", "forbid": ["projects/one.md"]}, False),
        ({"query": " \t\n", "text": ["answer"]}, False),
    ],
)
def test_evaluation_structure_matches_editor_validation(case: dict, valid: bool) -> None:
    value = {"name": "case", **case}
    assert Draft202012Validator(Case.model_json_schema()).is_valid(value) == valid
    if valid:
        Case.model_validate(value)
    else:
        with pytest.raises(ValidationError):
            Case.model_validate(value)


@pytest.mark.parametrize(
    ("header", "problem"),
    [("", "has no version"), ("version: 99\n", "declares version 99")],
    ids=["missing", "other"],
)
def test_evaluation_suites_declare_their_format(brain: Store, header: str, problem: str) -> None:
    brain.write("evals/retrieval.yaml", f"{header}cases:\n- name: absent\n  query: word\n  empty: true\n".encode())
    with pytest.raises(Error, match=rf"^evals/retrieval\.yaml {problem}; this release reads version: 10"):
        evaluate(brain)


def test_all_evaluation_cases_are_checked_before_retrieval(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("retrieval ran before every suite was validated")

    monkeypatch.setattr("bf.evaluate.search", unexpected)
    valid = {"name": "valid", "query": "word", "text": ["answer"]}
    brain.write("evals/a.yaml", json.dumps({"version": 10, "cases": [valid]}).encode())
    brain.write("evals/z.yaml", json.dumps({"version": 10, "cases": [{**valid, "read": ""}]}).encode())
    with pytest.raises(Error, match=r"evals/z.yaml.*use either query or read"):
        evaluate(brain)
    with pytest.raises(ValidationError, match="duplicate evaluation case names"):
        Suite.model_validate({"version": 10, "cases": [valid, valid]})


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"version: 10\nname: &name brain\n", "anchors and aliases are not supported"),
        (b"version: 10\nname: brain\nname: other\n", "keys must be unique strings at line 3"),
        (b"version: 10\nname: brain\n---\nname: other\n", "invalid YAML at line 3"),
        (b"version: 10\nname: brain\nextra: !!python/object/apply:os.system [PRIVATE]\n", "invalid YAML at line 3"),
        (b"version: 10\nname: brain\njobs: !!bool PRIVATE\n", "invalid YAML"),
        (b"version: 10\nname: brain\njobs: " + b"[" * 33 + b"]" * 33, "structure exceeds its limit"),
        (b"version: 10\nname: brain\nextra: [" + b"0," * 20_000 + b"]", "structure exceeds its limit"),
        (b"version: 10\nname: brain\n#" + b"x" * (1 << 20), "exceeds 1048576 bytes"),
    ],
    ids=["anchor", "duplicate-key", "two-documents", "python-tag", "bool-tag", "nesting", "events", "size"],
)
def test_malformed_configuration_never_runs_a_sensor_or_changes_evidence(
    brain: Store, data: bytes, problem: str
) -> None:
    before = {path: path.read_bytes() for path in (brain.root / "memories").rglob("*.json")}
    brain.write("bf.yaml", data)

    def unexpected(*_args):
        pytest.fail("sensor executed with malformed configuration")

    # A version keeps each case from failing on its format alone: each limit or rule must reject it.
    with pytest.raises(Error, match=problem) as failure:
        collect(brain, "demo", start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z", runner=unexpected)
    assert "PRIVATE" not in str(failure.value)
    assert before == {path: path.read_bytes() for path in (brain.root / "memories").rglob("*.json")}


@pytest.mark.parametrize("path", ["relative", "/synthetic/brain\x00", "/synthetic/brain\n", "/synthetic/brain\x1b[31m"])
def test_registration_rejects_ambiguous_or_control_character_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        Registration(path=path)


def test_schema_generation_detects_drift_without_overwriting_it(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    script = scripts / "schemas.py"
    shutil.copyfile(ROOT / "scripts/schemas.py", script)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed local generator, synthetic output directory
            [sys.executable, str(script), *args], capture_output=True, text=True, timeout=120, check=False
        )

    missing = run()
    assert missing.returncode == 1
    assert "generate:schema" in missing.stderr
    assert run("--write").returncode == 0
    assert run().returncode == 0
    changed = docs / "bf.schema.json"
    generated = changed.read_text()
    changed.write_text('{"title": "altered"}\n')
    drift = run()
    assert drift.returncode == 1
    assert "bf.schema.json: schema drift" in drift.stderr
    assert json.loads(changed.read_text()) == {"title": "altered"}
    # A last-key-wins JSON parser would miss this ambiguity and report no drift.
    changed.write_text('{"title":"Config",' + generated[1:])
    duplicate = run()
    assert duplicate.returncode == 1
    assert "bf.schema.json: missing or invalid schema" in duplicate.stderr


@pytest.mark.parametrize("kind", ["missing", "watch"])
def test_schema_rejects_unknown_kind_before_selection(kind: str) -> None:
    result = CliRunner().invoke(app, ["schema", "--kind", kind])
    assert result.exit_code == 2
    assert not result.stdout
    assert "--kind" in plain(result.stderr)


@pytest.mark.parametrize("kind", ["registry", "eval"])
def test_other_configuration_examples_match_their_schemas(kind: Kind) -> None:
    from bf.models import UserConfig

    model = {"registry": UserConfig, "eval": Suite}[kind]
    paths = {
        "registry": [],
        "eval": sorted((ROOT / "examples").rglob("evals/*.yaml")),
    }[kind]
    values = [yaml_object(path.read_bytes()) for path in paths] or [{"brains": {"local": {"path": "/synthetic/brain"}}}]
    for value in values:
        model.model_validate(value)
        Draft202012Validator(document(kind)).validate(value)
