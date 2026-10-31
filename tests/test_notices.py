from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
_PLATFORM_MARKERS = {
    "linux": {
        "implementation_name != 'PyPy'": True,
        "platform_python_implementation != 'PyPy'": True,
        "python_full_version < '3.15'": True,
        "sys_platform != 'emscripten'": True,
        "sys_platform == 'emscripten'": False,
        "sys_platform == 'win32'": False,
    },
    "macos": {
        "implementation_name != 'PyPy'": True,
        "platform_python_implementation != 'PyPy'": True,
        "python_full_version < '3.15'": True,
        "sys_platform != 'emscripten'": True,
        "sys_platform == 'emscripten'": False,
        "sys_platform == 'win32'": False,
    },
}


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _mapping(value: object, label: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{label} must be a table"
    assert all(isinstance(key, str) for key in value), f"{label} keys must be strings"
    return cast("dict[str, object]", value)


def _sequence(value: object, label: str) -> list[object]:
    assert isinstance(value, list), f"{label} must be an array"
    return cast("list[object]", value)


def _marker_matches(marker: object, platform: str) -> bool:
    if marker is None:
        return True
    assert isinstance(marker, str), "dependency marker must be a string"
    outcomes = _PLATFORM_MARKERS[platform]
    assert marker in outcomes, f"unsupported {platform} runtime marker in uv.lock: {marker}"
    return outcomes[marker]


def _dependency_entries(
    package: dict[str, object], *, include_base: bool, extras: set[str], package_name: str
) -> list[object]:
    entries = _sequence(package.get("dependencies", []), f"{package_name}.dependencies") if include_base else []
    optional = _mapping(package.get("optional-dependencies", {}), f"{package_name}.optional-dependencies")
    for extra in sorted(extras):
        assert extra in optional, f"{package_name} has no locked optional dependency group {extra}"
        entries.extend(_sequence(optional[extra], f"{package_name}.optional-dependencies.{extra}"))
    return entries


def _runtime_distributions(platform: str) -> dict[str, str]:
    assert platform in _PLATFORM_MARKERS, f"unsupported declared platform: {platform}"
    lock: dict[str, object] = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    raw_packages = _sequence(lock.get("package"), "package")
    packages: dict[str, dict[str, object]] = {}
    for index, value in enumerate(raw_packages):
        package = _mapping(value, f"package[{index}]")
        raw_name = package.get("name")
        assert isinstance(raw_name, str), f"package[{index}].name must be a string"
        name = _canonical_name(raw_name)
        assert name not in packages, f"uv.lock contains multiple entries for {name}"
        packages[name] = package

    # The notices cover every extra the package declares, as users install them too.
    declared = _mapping(packages["brain-framework"].get("optional-dependencies", {}), "brain-framework extras")
    requested_extras: dict[str, set[str]] = {"brain-framework": set(declared)}
    processed_extras: dict[str, set[str]] = {}
    selected: set[str] = set()
    pending = ["brain-framework"]
    while pending:
        name = pending.pop()
        assert name in packages, f"uv.lock has no package entry for runtime dependency {name}"
        package = packages[name]
        first_visit = name not in selected
        new_extras = requested_extras[name] - processed_extras.get(name, set())
        if not first_visit and not new_extras:
            continue
        selected.add(name)
        processed_extras.setdefault(name, set()).update(new_extras)

        for value in _dependency_entries(package, include_base=first_visit, extras=new_extras, package_name=name):
            dependency = _mapping(value, f"{name} dependency")
            if not _marker_matches(dependency.get("marker"), platform):
                continue
            raw_dependency_name = dependency.get("name")
            assert isinstance(raw_dependency_name, str), f"{name} dependency name must be a string"
            dependency_name = _canonical_name(raw_dependency_name)
            raw_extras = _sequence(dependency.get("extra", []), f"{name}->{dependency_name}.extra")
            extras: set[str] = set()
            for extra in raw_extras:
                assert isinstance(extra, str), f"{name}->{dependency_name} extra must be a string"
                extras.add(extra)
            known_extras = requested_extras.setdefault(dependency_name, set())
            if dependency_name not in selected or not extras.issubset(known_extras):
                known_extras.update(extras)
                pending.append(dependency_name)

    distributions: dict[str, str] = {}
    for name in selected:
        package = packages[name]
        source = _mapping(package.get("source"), f"{name}.source")
        if "registry" not in source:
            assert name == "brain-framework", f"unexpected non-registry runtime package: {name}"
            assert source == {"editable": "."}, "brain-framework must remain the editable lock root"
            continue
        version = package.get("version")
        assert isinstance(version, str), f"{name}.version must be a string"
        distributions[name] = version
    return distributions


def test_linux_and_macos_select_the_same_locked_runtime_closure() -> None:
    # Unknown lock markers fail until reviewed per platform. `check:notices` generates the notices from one platform's
    # synced environment, so the declared platforms must select the same closure.
    assert _runtime_distributions("linux") == _runtime_distributions("macos")


def test_distribution_packages_both_project_and_third_party_licenses() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
    assert notices.startswith("# Third-party notices\n\nBrain Framework depends on ")
    assert re.search(r"\bBrain Framework v?\d+\.\d+\.\d+\b", notices) is None


def _notices() -> ModuleType:
    spec = importlib.util.spec_from_file_location("notices", ROOT / "scripts/notices.py")
    assert spec is not None
    assert spec.loader is not None
    notices = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notices)
    return notices


def test_site_notices_fail_when_a_version_row_stops_matching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A relabeled row would otherwise keep its old version on the page while the check passes.
    notices = _notices()
    page = tmp_path / "third-party-notices.md"
    text = (ROOT / "docs/third-party-notices.md").read_text(encoding="utf-8")
    page.write_text(text.replace("Zensical browser bundle", "Zensical JavaScript"), encoding="utf-8")
    monkeypatch.setattr(notices, "SITE", page)
    with pytest.raises(SystemExit, match="expected the Zensical and browser bundle version rows, found 1"):
        notices.site()


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_site_notices_refuse_a_damaged_npm_license_copy_even_when_writing(
    damage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No synced distribution holds these copies, so --write can neither restore nor drop one.
    notices = _notices()
    files = tmp_path / "third-party"
    shutil.copytree(ROOT / "docs/third-party", files)
    copy = files / "clsx-LICENSE.txt"
    if damage == "changed":
        copy.write_bytes(copy.read_bytes().replace(b"Luke Edwards", b"Someone Else"))
    else:
        copy.unlink()
    for name, path in [("NOTICES", ROOT / "THIRD_PARTY_NOTICES.md"), ("SITE", ROOT / "docs/third-party-notices.md")]:
        shutil.copy(path, tmp_path / path.name)
        monkeypatch.setattr(notices, name, tmp_path / path.name)
    monkeypatch.setattr(notices, "SITE_FILES", files)
    monkeypatch.setattr(sys, "argv", ["notices.py", "--write"])
    with pytest.raises(SystemExit) as stopped:
        notices.main()
    assert stopped.value.code == 1
    assert "changed npm license copies (clsx-LICENSE.txt)" in capsys.readouterr().err
    assert copy.exists() == (damage == "changed")
