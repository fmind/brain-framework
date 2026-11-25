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
            for unset_key in ("BF_BRAIN", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
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

assert get_args(Kind) == ("bf", "record")
for kind, title in [("bf", "Config"), ("record", "Record")]:
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
            if not init_reply.get("ok") or init_reply.get("brain") != "brain":
                raise SystemExit(f"{artifact.name}: unexpected bf init reply: {init_reply}")
            starter_yaml = (brain / "bf.yaml").read_text()
            if "version: 19" not in starter_yaml or "loops:" not in starter_yaml:
                raise SystemExit(f"{artifact.name}: unexpected starter bf.yaml: {starter_yaml}")
            for required_schema in (
                "schemas/concepts.json",
                "schemas/projects.json",
                "schemas/actions.json",
            ):
                if not (brain / required_schema).is_file():
                    raise SystemExit(f"{artifact.name}: missing starter schema {required_schema}")
            for retired in (
                "schemas/bf.schema.json",
                "schemas/record.json",
                "schemas/usages.json",
                "schemas/evals.json",
                "skills/bf/SKILL.md",
            ):
                if (brain / retired).exists():
                    raise SystemExit(f"{artifact.name}: unexpected retired starter file {retired}")
            schema_reply = json.loads(run(bf, "schema", "bf", cwd=home, env=env, capture=True))
            if schema_reply.get("title") != "Config":
                raise SystemExit(f"{artifact.name}: unexpected bf schema bf reply: {schema_reply}")

            # Optional schema: ingesting without schemas/workspace.json succeeds using bundled record schema.
            sample_records = json.dumps(
                [
                    {
                        "id": "brief",
                        "title": "Launch brief",
                        "type": "doc",
                        "text": "Explain who the product helps before signup.",
                        "created": "2026-09-20T09:00:00Z",
                        "updated": "2026-09-20T09:00:00Z",
                    }
                ]
            )
            ingest_reply = json.loads(
                run(bf, "ingest", "workspace", cwd=brain, env=env, capture=True, input_text=sample_records)
            )
            if not ingest_reply.get("ok") or ingest_reply.get("added") != 1 or ingest_reply.get("records") != 1:
                raise SystemExit(f"{artifact.name}: unexpected bf ingest reply: {ingest_reply}")
            status_text = run(bf, "status", "--format", "text", cwd=brain, env=env, capture=True)
            if "HEALTHY" not in status_text or "memories=1" not in status_text:
                raise SystemExit(f"{artifact.name}: unexpected status --format text: {status_text}")

            (brain / "projects/package.md").write_text(
                "---\ntype: project\nstatus: active\ncreated: 2026-09-25\nupdated: 2026-09-25\n---\n# Package check\n\n"
                "## Next {#next owner=alice}\n\n"
                "See [brief](workspace:brief){rel=cites} and [welcome](../concepts/welcome.md#conventions).\n\n"
                "- [ ] Inspect the compiled note record.\n"
            )
            dry_build = json.loads(run(bf, "build", "--dry-run", cwd=brain, env=env, capture=True))
            build_reply = json.loads(run(bf, "build", cwd=brain, env=env, capture=True))
            check_reply = json.loads(run(bf, "check", cwd=brain, env=env, capture=True))
            check_notes = json.loads(run(bf, "check", "--notes", cwd=brain, env=env, capture=True))
            check_text = run(bf, "check", "--format", "text", cwd=brain, env=env, capture=True)
            rev_reply = json.loads(run(bf, "review", cwd=brain, env=env, capture=True))
            rev_text = run(bf, "review", "--format", "text", cwd=brain, env=env, capture=True)
            emit_dry = json.loads(
                run(bf, "emit", "pkg.smoke", "--data", '{"id":"s1"}', "--dry-run", cwd=brain, env=env, capture=True)
            )
            emit_reply = json.loads(
                run(bf, "emit", "pkg.smoke", "--data", '{"id":"s1"}', "--dedupe", cwd=brain, env=env, capture=True)
            )
            emit_dup = json.loads(
                run(bf, "emit", "pkg.smoke", "--data", '{"id":"s1"}', "--dedupe", cwd=brain, env=env, capture=True)
            )
            emit_tmr = json.loads(
                run(
                    bf,
                    "emit",
                    "pkg.wake",
                    "--in",
                    "1h",
                    "--data",
                    '{"id":"w1"}',
                    "--dedupe",
                    cwd=brain,
                    env=env,
                    capture=True,
                )
            )
            evs_reply = json.loads(run(bf, "events", "--kind", "pkg.smoke", cwd=brain, env=env, capture=True))
            tmrs_reply = json.loads(run(bf, "events", "--timers", cwd=brain, env=env, capture=True))
            pause_reply = json.loads(run(bf, "pause", "--all", "--dry-run", cwd=brain, env=env, capture=True))
            resume_reply = json.loads(run(bf, "resume", "--all", "--dry-run", cwd=brain, env=env, capture=True))
            status_reply = json.loads(run(bf, "status", "--check", cwd=brain, env=env, capture=True))
            state_prune = json.loads(run(bf, "state", "prune", "--dry-run", cwd=brain, env=env, capture=True))
            run_reply = json.loads(run(bf, "run", "--all", "--dry-run", cwd=brain, env=env, capture=True))
            q_fts = json.loads(run(bf, "query", "Launch brief", cwd=brain, env=env, capture=True))
            q_back = json.loads(run(bf, "query", "--links-to", "workspace:brief", cwd=brain, env=env, capture=True))
            read_reply = json.loads(run(bf, "read", "bf:///projects/package.md#next", cwd=brain, env=env, capture=True))
            raw_reply = run(bf, "read", "projects/package.md", "--format", "raw", cwd=brain, env=env, capture=True)
            note_reply = json.loads(run(bf, "read", "projects/package.md", cwd=brain, env=env, capture=True))
            if (
                not dry_build.get("dry_run")
                or build_reply.get("problems")
                or not check_reply.get("ok")
                or not check_notes.get("ok")
                or "[OK]" not in check_text
                or rev_reply.get("total_tasks_open") != 1
                or "Review:" not in rev_text
                or not emit_dry.get("dry_run")
                or not emit_reply.get("ok")
                or emit_reply.get("deduped") is not False
                or emit_dup.get("deduped") is not True
                or not emit_tmr.get("scheduled")
                or evs_reply.get("count") != 1
                or tmrs_reply.get("count") != 1
                or not pause_reply.get("ok")
                or not resume_reply.get("ok")
                or not status_reply.get("inside_brain")
                or not state_prune.get("ok")
                or not run_reply.get("ok")
                or q_fts.get("engine") != "fts5"
                or q_fts.get("count", 0) < 1
                or q_back.get("count") != 1
                or q_back["items"][0].get("ref") != "projects/package.md"
                or "Inspect the compiled note record" not in read_reply.get("text", "")
                or "# Package check" not in raw_reply
                or note_reply.get("kind") != "note"
                or note_reply.get("path") != "projects/package.md"
                or note_reply.get("record", {}).get("kind") != "note"
            ):
                raise SystemExit(f"{artifact.name}: smoke check failed: {build_reply} / {check_reply} / {rev_reply}")
            note_lines = [
                json.loads(ln) for ln in (brain / "memories/_notes.jsonl").read_text().splitlines() if ln.strip()
            ]
            proj = next(r for r in note_lines if r["ref"] == "projects/package.md")
            if (
                proj["kind"] != "note"
                or proj["headings"][1]["meta"] != {"owner": "alice"}
                or proj["links"][0]["to"] != "workspace:brief"
                or proj["links"][0]["meta"] != {"rel": "cites"}
            ):
                raise SystemExit(f"{artifact.name}: compiled _notes.jsonl mismatch: {proj}")
            run(python, str(brain / "evals/smoke.py"), cwd=brain, env={**env, "PATH": f"{venv / 'bin'}:{env['PATH']}"})


if __name__ == "__main__":
    main()
