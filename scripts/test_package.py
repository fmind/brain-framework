"""Install both release artifacts with locked dependencies and exercise the public CLI.

With --unlocked, resolve the newest dependencies the package allows, as `uv tool install` does for users; this needs
network access and runs weekly in the security workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")


def run(
    *args: str, cwd: Path, env: dict[str, str], capture: bool = False, input_text: str | None = None, timeout: int = 180
) -> str:
    result = subprocess.run(  # noqa: S603
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        check=True,
        timeout=timeout,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout or ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="3.14", help="Interpreter version for the isolated environments.")
    parser.add_argument("--unlocked", action="store_true", help="Ignore uv.lock and resolve the newest dependencies.")
    args = parser.parse_args()
    if UV is None:
        raise SystemExit("uv is required")
    cache = subprocess.check_output([UV, "cache", "dir"], text=True, timeout=30).strip()  # noqa: S603
    artifacts = [
        list((ROOT / "dist").glob(pattern)) for pattern in ("brain_framework-*.whl", "brain_framework-*.tar.gz")
    ]
    if any(len(matches) != 1 for matches in artifacts):
        raise SystemExit("expected exactly one brain-framework wheel and one source distribution")
    scratch = Path(os.environ.get("XDG_CACHE_HOME", ""))
    if not scratch.is_absolute():
        scratch = Path.home() / ".cache"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bf-package-", dir=scratch) as temporary:
        root = Path(temporary).resolve()
        for (artifact,) in artifacts:
            home = root / artifact.name
            home.mkdir()
            venv = home / "venv"
            env: dict[str, str] = {
                **os.environ,
                "HOME": str(home),
                "XDG_STATE_HOME": str(home / "state"),
                "XDG_CONFIG_HOME": str(home / "config"),
                "VIRTUAL_ENV": str(venv),
                "UV_LINK_MODE": "copy",
                "UV_CACHE_DIR": cache,
            }
            for unset_key in ("BF_BRAIN", "DEFAULT_BRAIN_PATH", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
                env.pop(unset_key, None)
            run(UV, "venv", "--python", args.python, str(venv), cwd=home, env=env)
            python = str(venv / "bin/python")
            locked = [UV, "sync", "--project", str(ROOT), "--locked", "--offline", "--no-dev", "--no-install-project"]
            if args.unlocked:
                run(UV, "pip", "install", "--python", python, str(artifact), cwd=home, env=env, timeout=600)
            else:
                run(*locked, "--active", cwd=home, env=env)
                run(
                    UV, "pip", "install", "--offline", "--no-deps", "--python", python, str(artifact), cwd=home, env=env
                )
            run(UV, "pip", "check", "--python", python, cwd=home, env=env)
            run(
                python,
                "-I",
                "-c",
                """
from importlib.metadata import distribution
from pathlib import Path
from typing import get_args
import bf
from bf.schemas import Kind, document

assert get_args(Kind) == ("brain", "record", "concepts", "logs", "evals")
for kind, title in [
    ("brain", "Config"),
    ("record", "Record"),
    ("concepts", "Concept"),
    ("logs", "Log"),
    ("evals", "Eval"),
]:
    assert document(kind)["title"] == title
installed = distribution("brain-framework")
assert installed.metadata["Name"] == "brain-framework"
assert installed.version == bf.__version__
assert [(entry.name, entry.value) for entry in installed.entry_points] == [("bf", "bf:main")]
assert Path(bf.__file__).resolve().is_relative_to(Path.cwd())
""",
                cwd=home,
                env=env,
            )
            bf = str(venv / "bin/bf")
            brain = home / "brain"
            run(bf, "--help", cwd=home, env=env)
            if (
                run(bf, "--version", cwd=home, env=env, capture=True).strip()
                != subprocess.check_output(  # noqa: S603
                    [python, "-c", "import bf; print(bf.__version__)"], text=True
                ).strip()
            ):
                raise SystemExit(f"{artifact.name}: bf --version mismatch")
            init_reply = json.loads(run(bf, "init", str(brain), cwd=home, env=env, capture=True))
            if init_reply.get("brain") != "brain":
                raise SystemExit(f"{artifact.name}: unexpected bf init reply: {init_reply}")
            starter_yaml = (brain / "bf.yaml").read_text()
            if "version: 12" not in starter_yaml:
                raise SystemExit(f"{artifact.name}: unexpected starter bf.yaml: {starter_yaml}")
            for schema_file in ("record.json", "concepts.json", "logs.json", "evals.json"):
                if not (brain / "schemas" / schema_file).is_file():
                    raise SystemExit(f"{artifact.name}: missing starter schemas/{schema_file}")

            sample_records = json.dumps(
                [
                    {
                        "id": "brief",
                        "title": "Launch brief",
                        "text": "Explain who the product helps before signup.",
                        "time": "2026-09-20T09:00:00Z",
                        "updated": "2026-09-20T09:00:00Z",
                    }
                ]
            )
            ingest_reply = json.loads(
                run(bf, "ingest", "workspace", cwd=brain, env=env, capture=True, input_text=sample_records)
            )
            if ingest_reply.get("added") != 1 or ingest_reply.get("records") != 1:
                raise SystemExit(f"{artifact.name}: unexpected bf ingest reply: {ingest_reply}")

            (brain / "projects/package.md").write_text(
                "---\ntype: project\nstatus: draft\nupdated: 2026-09-25\n---\n# Package check\n\n"
                "## Next {#next owner=alice}\n\n"
                "See [brief](../memories/workspace/29a8825bd242f14386ee528d76e0e8f1e38f3c8c4047d7b2d6df7493368a17d0.json){rel=cites} and [welcome](../concepts/welcome.md#conventions).\n"
                "{: #decision-1 confidence=high}\n\n"
                "- [ ] Inspect the compiled note record.\n"
            )
            build_reply = json.loads(run(bf, "build", cwd=brain, env=env, capture=True))
            val_reply = json.loads(run(bf, "validate", cwd=brain, env=env, capture=True))
            rev_reply = json.loads(run(bf, "review", cwd=brain, env=env, capture=True))
            eval_reply = json.loads(run(bf, "eval", cwd=brain, env=env, capture=True))
            read_reply = json.loads(run(bf, "read", "bf:///projects/package.md#next", cwd=brain, env=env, capture=True))
            if (
                build_reply.get("problems")
                or not val_reply.get("valid")
                or rev_reply.get("tasks_open") != 1
                or not eval_reply.get("ok")
                or "Inspect the compiled note record" not in read_reply.get("text", "")
            ):
                raise SystemExit(
                    f"{artifact.name}: smoke check failed: {build_reply} / {val_reply} / {rev_reply} / {eval_reply}"
                )
            proj = json.loads((brain / "memories/projects/package.json").read_text())
            if (
                proj["headings"][1]["meta"] != {"owner": "alice"}
                or proj["attrs"].get("next") != {"owner": "alice"}
                or proj["attrs"].get("decision-1") != {"confidence": "high"}
                or proj["links"][0]["meta"] != {"rel": "cites"}
                or proj["elements"][0]["meta"] != {"confidence": "high"}
            ):
                raise SystemExit(f"{artifact.name}: compiled package.json missing attr_list metadata: {proj}")


if __name__ == "__main__":
    main()
