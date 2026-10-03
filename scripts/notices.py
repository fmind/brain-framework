"""Check or regenerate third-party notices from the synced environment; the default checks without changing files."""

import argparse
import re
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
SITE = ROOT / "docs/third-party-notices.md"
SITE_FILES = ROOT / "docs/third-party"
INTRODUCTION = """# Third-party notices

Brain Framework depends on the following non-development Python distributions on its declared Linux and macOS platforms. The inventory is the complete locked closure selected by `uv tree --no-dev`; build and test dependencies are intentionally excluded.

License expressions come from the installed distribution metadata. Legal text is copied from the corresponding installed wheel files and formatted only as Markdown text blocks. The Brain Framework project itself is licensed separately in `LICENSE`.
"""
# Reviewed expressions for distributions whose metadata declares no SPDX expression; metadata wins once it does.
LICENSES = {"markdown-it-py": "MIT", "mdurl": "MIT", "shellingham": "ISC"}
SOURCES = ("source", "source code", "repository", "github", "homepage", "home")
# SPDX identifiers joined by operators, with optional parentheses; unbalanced parentheses are rejected separately.
IDENTIFIER = r"[A-Za-z0-9.+-]+"
SPDX = re.compile(rf"\(*{IDENTIFIER}\)*(?: (?:AND|OR|WITH) \(*{IDENTIFIER}\)*)*")
# Reviewed licenses for distributed dependencies; trivy skips .venv, so a new license fails here until reviewed.
PERMITTED = {"Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MIT", "MIT-0", "PSF-2.0"}
# Reviewed code vendored inside a distribution, keyed by its folder: the component it adapts and that license.
VENDORED = {"typer/_click": ("Click", "BSD-3-Clause")}
LEGAL = re.compile(r"(?:LICEN[CS]E|COPYING|NOTICE)[^/]*")
# Site copies of the licenses bundled by the locked Zensical distribution, keyed by their published name.
BUNDLED = {
    "zensical-LICENSE.txt": "zensical-{version}.dist-info/licenses/LICENSE.md",
    "lucide-LICENSE.txt": "zensical/templates/.icons/lucide/LICENSE",
    "simple-icons-LICENSE.txt": "zensical/templates/.icons/simple/LICENSE.md",
    "zensical-javascript-LICENSE.txt": "zensical/templates/assets/javascripts/LICENSE",
}
SITE_VERSION = re.compile(r"^(\| Zensical(?: browser bundle)? +\| )(\S+)", re.MULTILINE)


def closure() -> list[metadata.Distribution]:
    """The runtime distributions required by brain-framework on this platform, following requested extras."""
    selected: dict[str, metadata.Distribution] = {}
    extras: dict[str, set[str]] = {}
    pending: list[tuple[str, set[str]]] = [("brain-framework", set())]
    while pending:
        name, wanted = pending.pop()
        if name in selected and wanted <= extras[name]:
            continue
        extras.setdefault(name, set()).update(wanted)
        distribution = selected.setdefault(name, metadata.distribution(name))
        for requirement in map(Requirement, distribution.requires or []):
            marker = requirement.marker
            if marker is None or any(marker.evaluate({"extra": extra}) for extra in extras[name] | {""}):
                pending.append((canonicalize_name(requirement.name), set(requirement.extras)))
    del selected["brain-framework"]
    return list(selected.values())


def field(distribution: metadata.Distribution, name: str) -> str:
    return distribution.metadata.get(name) or ""


def source(name: str, distribution: metadata.Distribution) -> str:
    urls: dict[str, str] = {}
    for entry in distribution.metadata.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls.setdefault(label.strip().casefold(), url.strip())
    for url in [*(urls.get(label, "") for label in SOURCES), field(distribution, "Home-page")]:
        if url.startswith("https://"):
            return url
    raise SystemExit(f"{name}: no https source URL in its metadata")


def permitted(name: str, value: str) -> str:
    if unreviewed := set(re.findall(IDENTIFIER, value)) - PERMITTED - {"AND", "OR", "WITH"}:
        raise SystemExit(f"{name}: review license {', '.join(sorted(unreviewed))} before adding it to PERMITTED")
    return value


def expression(name: str, distribution: metadata.Distribution) -> str:
    # A declared expression is authoritative: never fall back past it to a reviewed or legacy value.
    declared = field(distribution, "License-Expression")
    value = declared or LICENSES.get(name) or field(distribution, "License")
    if not (SPDX.fullmatch(value) and value.count("(") == value.count(")")):
        if declared:
            raise SystemExit(f"{name}: review its License-Expression, which the SPDX pattern cannot parse")
        raise SystemExit(f"{name}: no SPDX license expression; review its legal files and add one to LICENSES")
    return permitted(name, value)


def legal(distribution: metadata.Distribution) -> tuple[list[metadata.PackagePath], list[metadata.PackagePath]]:
    """Legal files in the wheel metadata, then license files of code vendored inside the package."""
    files = sorted((file for file in distribution.files or [] if LEGAL.fullmatch(file.name)), key=str)
    own = [file for file in files if file.parts[0].endswith(".dist-info")]
    return own, [file for file in files if file not in own]


def block(text: str) -> str:
    # Keep the legal text verbatim apart from line endings and trailing spaces, which Git whitespace checks reject.
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    body = "\n".join(lines).strip("\n")
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`{3,}", body))])
    return f"{fence}text\n{body}\n{fence}\n"


def notices() -> str:
    sections: list[str] = []
    reviewed: set[str] = set()
    for distribution in closure():
        name = canonicalize_name(distribution.metadata["Name"])
        own, vendored = legal(distribution)
        if not own:
            raise SystemExit(f"{name}: no installed legal file")
        section = f"## {name} {distribution.version}\n\nSource: <{source(name, distribution)}>\n\nLicense: `{expression(name, distribution)}`\n"
        for folder in sorted({file.parent.as_posix() for file in vendored}):
            if folder not in VENDORED:
                raise SystemExit(f"{name}: review the code vendored in {folder}/ and add it to VENDORED")
            component, spdx = VENDORED[folder]
            reviewed.add(folder)
            section += f"\nThe distribution vendors code adapted from {component} in `{folder}/`; its bundled `{permitted(name, spdx)}` license is included below.\n"
        for file in own + vendored:
            section += f"\nEvidence: `{file}`\n\n### {file}\n\n{block(file.read_text(encoding='utf-8'))}"
        sections.append(section)
    if stale := sorted(VENDORED.keys() - reviewed):
        raise SystemExit(f"remove VENDORED entries without vendored legal files: {', '.join(stale)}")
    return "\n".join([INTRODUCTION, *sorted(sections, key=lambda text: text.partition("\n")[0].casefold())])


def site() -> tuple[str, dict[str, bytes]]:
    zensical = metadata.distribution("zensical")
    page = SITE_VERSION.sub(lambda match: match[1] + zensical.version, SITE.read_text(encoding="utf-8"))
    return page, {
        published: zensical.locate_file(installed.format(version=zensical.version)).read_bytes()
        for published, installed in BUNDLED.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Regenerate; the default checks without changing files.")
    args = parser.parse_args()
    # Generate everything before replacing a file; a missing distribution must not leave a partial update.
    text, (page, bundled) = notices(), site()
    stray = sorted(path.name for path in SITE_FILES.iterdir() if path.name not in bundled)
    if args.write:
        NOTICES.write_text(text, encoding="utf-8")
        SITE.write_text(page, encoding="utf-8")
        for name, content in bundled.items():
            (SITE_FILES / name).write_bytes(content)
        for name in stray:
            (SITE_FILES / name).unlink()
        return
    drift = [NOTICES.name] if NOTICES.read_text(encoding="utf-8") != text else []
    drift += [SITE.name] if SITE.read_text(encoding="utf-8") != page else []
    drift += [name for name, content in bundled.items() if (SITE_FILES / name).read_bytes() != content]
    drift += [f"unexpected {name}" for name in stray]
    if drift:
        parser.exit(1, f"third-party notice drift ({', '.join(drift)}); run mise run generate:notices\n")


if __name__ == "__main__":
    main()
