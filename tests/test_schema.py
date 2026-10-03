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
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from typer.testing import CliRunner

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
        {"version": 7, "name": "brain\n"},
        {"version": 7, "name": "brain", "sensors": {"Invalid Name": {"nonsense": True}}},
        {"version": 7, "name": "brain", "sensors": {"demo\n": {"command": ["echo"]}}},
        {"version": 7, "name": "brain", "routines": {"bad--slug": {"command": ["echo"]}}},
        {"version": 7, "name": "brain", "brains": {"Invalid Name": {"path": "../team"}}},
        {"version": 7, "name": "brain", "fields": {"Invalid Name": {"description": "Kind", "type": "string"}}},
        {
            "version": 7,
            "name": "brain",
            "fields": {"kind": {"description": "Kind", "type": "string", "relation": True}},
        },
        *(
            {"version": 7, "name": "brain", "fields": {"owner": {"description": "Owner", "type": "identity", **extra}}}
            for extra in (
                {"targets": ["repo:"]},
                {"broader": "owner", "relation": False},
                {"relation": True, "targets": []},
                {"relation": True, "targets": ["Repo:"]},
                {"relation": True, "targets": ["repo:\n"]},
                {"relation": True, "targets": ["repo"]},
                {"relation": True, "broader": "Owner"},
            )
        ),
        {
            "version": 7,
            "name": "brain",
            "sensors": {"demo": {"command": ["echo"], "mode": "snapshot", "reconcile": {"refresh": 1, "lookback": 1}}},
        },
        *(
            {"version": 7, "name": "brain", "watch": watch}
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
        {"version": 5, "name": "brain"},
        *(
            {"version": 7, "name": "brain", "sensors": {"demo": {"command": [executable]}}}
            for executable in ("/usr/bin/env", "../escape.sh", "sensors/../x", "sensors//x", "bin/x", ".", "a{b")
        ),
        {"version": 7, "name": "brain", "routines": {"digest": {"command": ["routines/./digest.py"]}}},
        {"version": 7, "name": "brain", "sensors": []},
        {"version": 7, "name": "brain", "brains": {f"brain-{i}": {"path": f"../{i}"} for i in range(33)}},
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
            ("brain", f"version: 7\nname: brain\n{key}:\n  # demo: {{}}\n".encode(), {"version": 7, "name": "brain"})
            for key in ("brains", "fields", "sensors", "watch", "routines")
        ),
        (
            "brain",
            b"version: 7\nname: brain\nsensors:\n  demo:\n    command: [echo]\n    fields:\n      # kind: {path: /a}\n",
            {"version": 7, "name": "brain", "sensors": {"demo": {"command": ["echo"]}}},
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
            {
                "fields": {"kind": {"description": "Item kind", "type": "string"}},
                "sensors": {
                    "mail": {"command": ["echo"]},
                    "chat": {"command": ["echo"], "fields": {"kind": {"value": 3}}},
                },
            },
            "sensors.chat.fields.kind.value: expected string",
        ),
        (
            {"fields": {"kind": {"description": "Item kind", "type": "string", "examples": ["ok", 3]}}},
            "fields.kind: examples.1: expected string",
        ),
        (
            {"sensors": {"digest": {"command": ["echo"]}}, "routines": {"digest": {"command": ["echo"]}}},
            "sensors.digest and routines.digest: sensor and routine names must be distinct",
        ),
    ],
    ids=["literal-value", "example", "program-name"],
)
def test_invalid_literal_values_and_shared_names_are_located(brain: Store, value: dict, message: str) -> None:
    # JSON is YAML: the configuration names the key to fix among several sensors or examples.
    brain.write("bf.yaml", json.dumps({"version": 7, "name": "fixture", **value}).encode())
    with pytest.raises(Error, match=rf"^invalid bf\.yaml: {re.escape(message)}"):
        load(brain)


@pytest.mark.parametrize(
    "mapping",
    [{}, {"path": None}, {"value": None}, {"path": "/title", "value": "x"}, {"path": "title"}, {"path": "/~2"}],
)
def test_editor_rejects_invalid_mapping_forms(mapping: dict) -> None:
    value = {
        "version": 7,
        "name": "brain",
        "fields": {"kind": {"description": "Item kind", "type": "string"}},
        "sensors": {"demo": {"command": ["echo"], "fields": {"kind": mapping}}},
    }
    with pytest.raises(ValidationError):
        Config.model_validate(value)
    assert not Draft202012Validator(Config.model_json_schema()).is_valid(value)


@pytest.mark.parametrize("field", ["name", "expect", "forbid", "text"])
@pytest.mark.parametrize("blank", ["", " \t\n"])
def test_evaluation_rejects_blank_labels_and_assertions(brain: Store, field: str, blank: str) -> None:
    case = {"name": "missing-evidence", "query": "absentneedle947ab3", "text": ["required evidence"]}
    case[field] = blank if field == "name" else [blank]
    brain.write("evals/retrieval.yaml", json.dumps({"version": 7, "cases": [case]}).encode())
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


@pytest.mark.parametrize("loader", ["default", "pure"])
def test_yaml_constructor_failures_are_safe_with_either_parser(loader: str) -> None:
    program = """
import sys, yaml
if sys.argv[1] == "pure" and hasattr(yaml, "CSafeLoader"):
    del yaml.CSafeLoader
from bf.config import yaml_object
from bf.models import Error
assert yaml_object(b"name: brain\\nversion: 6\\n") == {"name": "brain", "version": 6}
for value in [b"!!bool PRIVATE", b'!!int ""', b'!!float ""', b"!!map [PRIVATE]"]:
    try:
        yaml_object(b"field: " + value, "bf.yaml")
    except Error as error:
        assert "PRIVATE" not in str(error)
        assert str(error).startswith("bf.yaml: invalid YAML")
    else:
        raise AssertionError("malformed tagged value accepted")
"""
    result = subprocess.run(  # noqa: S603 - fresh process selects the installed parser before importing BF
        [sys.executable, "-c", program, loader], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout


def test_starter_keeps_roles_without_serializing_redundant_defaults(tmp_path: Path) -> None:
    root = tmp_path / "new"
    result = CliRunner().invoke(app, ["init", str(root), "--name", "new"])
    assert result.exit_code == 0, result.output
    document = yaml_object((root / "bf.yaml").read_bytes())
    assert document["version"] == 7
    assert not {"brains", "sensors", "routines", "watch"} & document.keys()
    assert "examples:" not in (root / "bf.yaml").read_text()
    config = load(Store(root))
    assert set(config.ontology) == {"author", "owner", "depends-on", "related-to"}
    assert not config.sensors
    assert not config.routines
    assert not config.brains


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
    ("mapping", "kind", "expected"),
    [
        ({"path": "/attributes/a~1b/~0/0"}, "string", "value"),
        ({"value": False}, "boolean", False),
        ({"value": 0}, "integer", 0),
    ],
)
def test_valid_mappings_preserve_escaped_paths_and_false_constants(mapping: dict, kind: str, expected: object) -> None:
    from bf.models import Record
    from bf.ontology import project

    value = {
        "version": 7,
        "name": "brain",
        "fields": {"field": {"description": "Meaning", "type": kind}},
        "sensors": {"demo": {"command": ["echo"], "fields": {"field": mapping}}},
    }
    config = Config.model_validate(value)
    Draft202012Validator(document()).validate(value)
    record = Record(id="item", title="Item", attributes={"a/b": {"~": ["value"]}})
    assert project(record, config.sensors["demo"], config).fields == {"field": expected}


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
    [("", "has no version"), ("version: 8\n", "declares version 8")],
    ids=["missing", "other"],
)
def test_evaluation_suites_declare_their_format(brain: Store, header: str, problem: str) -> None:
    brain.write("evals/retrieval.yaml", f"{header}cases:\n- name: absent\n  query: word\n  empty: true\n".encode())
    with pytest.raises(Error, match=rf"^evals/retrieval\.yaml {problem}; this release reads version: 7"):
        evaluate(brain)


def test_all_evaluation_cases_are_checked_before_retrieval(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("retrieval ran before every suite was validated")

    monkeypatch.setattr("bf.evaluate.search", unexpected)
    valid = {"name": "valid", "query": "word", "text": ["answer"]}
    brain.write("evals/a.yaml", json.dumps({"version": 7, "cases": [valid]}).encode())
    brain.write("evals/z.yaml", json.dumps({"version": 7, "cases": [{**valid, "read": ""}]}).encode())
    with pytest.raises(Error, match=r"evals/z.yaml.*use either query or read"):
        evaluate(brain)
    with pytest.raises(ValidationError, match="duplicate evaluation case names"):
        Suite.model_validate({"version": 7, "cases": [valid, valid]})


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"version: 7\nname: &name brain\n", "anchors and aliases are not supported"),
        (b"version: 7\nname: brain\nname: other\n", "keys must be unique strings at line 3"),
        (b"version: 7\nname: brain\n---\nname: other\n", "invalid YAML at line 3"),
        (b"version: 7\nname: brain\nextra: !!python/object/apply:os.system [PRIVATE]\n", "invalid YAML at line 3"),
        (b"version: 7\nname: brain\nsensors: !!bool PRIVATE\n", "invalid YAML"),
        (b"version: 7\nname: brain\nsensors: " + b"[" * 33 + b"]" * 33, "structure exceeds its limit"),
        (b"version: 7\nname: brain\nextra: [" + b"0," * 20_000 + b"]", "structure exceeds its limit"),
        (b"version: 7\nname: brain\n#" + b"x" * (1 << 20), "exceeds 1048576 bytes"),
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
            [sys.executable, str(script), *args], capture_output=True, text=True, timeout=10, check=False
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
