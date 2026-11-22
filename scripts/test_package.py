"""Install both release artifacts with locked dependencies and exercise the public CLI.

With --unlocked, resolve the newest dependencies the package allows, as `uv tool install` does for users; this needs
network access and runs weekly in the security workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
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


def _real_binary(tool: str) -> str:
    found = shutil.which(tool)
    if not found:
        raise SystemExit(f"{tool} is required on PATH")
    resolved = Path(found).resolve()
    if resolved.name == "mise":
        out = subprocess.check_output([str(resolved), "which", tool], cwd=ROOT, text=True, timeout=10).strip()  # noqa: S603
        if not out:
            raise SystemExit(f"mise which {tool} returned empty output")
        resolved = Path(out).resolve()
    if not resolved.is_file():
        raise SystemExit(f"{tool} resolved to non-file path: {resolved}")
    return str(resolved)


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
    real_rg_path, real_jq_path = _real_binary("rg"), _real_binary("jq")
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
            for unset_key in ("BF_BRAIN", "BRAIN_LOCATION", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
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
            if "version: 17" not in starter_yaml or "loops:" not in starter_yaml:
                raise SystemExit(f"{artifact.name}: unexpected starter bf.yaml: {starter_yaml}")
            for required_schema in (
                "schemas/concepts.json",
                "schemas/projects.json",
                "schemas/actions.json",
                "schemas/usages.json",
            ):
                if not (brain / required_schema).is_file():
                    raise SystemExit(f"{artifact.name}: missing starter schema {required_schema}")
            for retired in (
                "schemas/bf.schema.json",
                "schemas/record.json",
                "schemas/evals.json",
                "skills/bf/SKILL.md",
            ):
                if (brain / retired).exists():
                    raise SystemExit(f"{artifact.name}: unexpected retired starter file {retired}")
            schema_reply = json.loads(run(bf, "schema", "bf", cwd=home, env=env, capture=True))
            if schema_reply.get("title") != "Config":
                raise SystemExit(f"{artifact.name}: unexpected bf schema bf reply: {schema_reply}")

            usage_sample = json.dumps(
                [{"id": "u1", "title": "Session 1", "harness": "agy", "input_tokens": 120, "output_tokens": 45}]
            )
            usage_reply = json.loads(
                run(bf, "ingest", "--to", "usages", "agy", cwd=brain, env=env, capture=True, input_text=usage_sample)
            )
            if not usage_reply.get("ok") or usage_reply.get("target") != "usages" or usage_reply.get("added") != 1:
                raise SystemExit(f"{artifact.name}: unexpected usages ingest reply: {usage_reply}")
            status_text = run(bf, "status", "--format", "text", cwd=brain, env=env, capture=True)
            if "HEALTHY" not in status_text or "usages=1" not in status_text:
                raise SystemExit(f"{artifact.name}: unexpected status --format text: {status_text}")

            (brain / "schemas/workspace.json").write_text(
                json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}) + "\n"
            )
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
            if not ingest_reply.get("ok") or ingest_reply.get("added") != 1 or ingest_reply.get("records") != 1:
                raise SystemExit(f"{artifact.name}: unexpected bf ingest reply: {ingest_reply}")

            (brain / "projects/package.md").write_text(
                "---\ntype: project\nstatus: draft\nupdated: 2026-09-25\n---\n# Package check\n\n"
                "## Next {#next owner=alice}\n\n"
                "See [brief](workspace:brief){rel=cites} and [welcome](../concepts/welcome.md#conventions).\n\n"
                "- [ ] Inspect the compiled note record.\n"
            )
            build_reply = json.loads(run(bf, "build", cwd=brain, env=env, capture=True))
            check_reply = json.loads(run(bf, "check", cwd=brain, env=env, capture=True))
            rev_reply = json.loads(run(bf, "review", cwd=brain, env=env, capture=True))
            emit_reply = json.loads(
                run(bf, "emit", "pkg.smoke", "--data", '{"id":"s1"}', cwd=brain, env=env, capture=True)
            )
            status_reply = json.loads(run(bf, "status", cwd=brain, env=env, capture=True))
            run_reply = json.loads(run(bf, "run", "--all", "--dry-run", cwd=brain, env=env, capture=True))
            read_reply = json.loads(run(bf, "read", "bf:///projects/package.md#next", cwd=brain, env=env, capture=True))
            note_reply = json.loads(run(bf, "read", "projects/package.md", cwd=brain, env=env, capture=True))
            if (
                build_reply.get("problems")
                or not check_reply.get("ok")
                or rev_reply.get("total_tasks_open") != 1
                or not emit_reply.get("ok")
                or not status_reply.get("inside_brain")
                or not run_reply.get("ok")
                or "Inspect the compiled note record" not in read_reply.get("text", "")
                or note_reply.get("kind") != "note"
                or note_reply.get("record", {}).get("kind") != "note"
            ):
                raise SystemExit(f"{artifact.name}: smoke check failed: {build_reply} / {check_reply} / {rev_reply}")
            proj_lines = [
                json.loads(ln) for ln in (brain / "memories/projects.jsonl").read_text().splitlines() if ln.strip()
            ]
            proj = next(r for r in proj_lines if r["ref"] == "projects/package.md")
            if (
                proj["kind"] != "note"
                or proj["headings"][1]["meta"] != {"owner": "alice"}
                or proj["links"][0]["to"] != "workspace:brief"
                or proj["links"][0]["meta"] != {"rel": "cites"}
            ):
                raise SystemExit(f"{artifact.name}: compiled projects.jsonl mismatch: {proj}")
            run(python, str(brain / "evals/smoke.py"), cwd=brain, env={**env, "PATH": f"{venv / 'bin'}:{env['PATH']}"})

            # Exercise `bf query` and `bf query --jq` through wrapper-script and `mise`-style shims on PATH.
            for label, mode in (("wrapper", "script"), ("mise-shim", "mise")):
                shim_dir = home / f"shims-{label}"
                shim_dir.mkdir()
                if mode == "script":
                    for tool, target in (("rg", real_rg_path), ("jq", real_jq_path)):
                        wrapper = shim_dir / tool
                        wrapper.write_text(
                            "#!/bin/sh\n"
                            '[ -n "${HOME:-}" ] && [ -n "${XDG_CONFIG_HOME:-}" ] && [ -z "${GITHUB_TOKEN:-}" ] || exit 1\n'
                            f'exec {shlex.quote(target)} "$@"\n'
                        )
                        wrapper.chmod(0o755)
                else:
                    fake_mise = shim_dir / "mise"
                    fake_mise.write_text(
                        "#!/bin/sh\n"
                        '[ -n "${HOME:-}" ] && [ -z "${GITHUB_TOKEN:-}" ] || exit 1\n'
                        f'if [ "$1" = "which" ] && [ "$2" = "rg" ]; then printf "%s\\n" {shlex.quote(real_rg_path)}; exit 0; fi\n'
                        f'if [ "$1" = "which" ] && [ "$2" = "jq" ]; then printf "%s\\n" {shlex.quote(real_jq_path)}; exit 0; fi\n'
                        "exit 1\n"
                    )
                    fake_mise.chmod(0o755)
                    (shim_dir / "rg").symlink_to(fake_mise)
                    (shim_dir / "jq").symlink_to(fake_mise)
                shim_env = {
                    **env,
                    "PATH": f"{shim_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                    "GITHUB_TOKEN": "ghp_secret_leak_test_0123456789",
                }
                q_rg = json.loads(run(bf, "query", "welcome", cwd=brain, env=shim_env, capture=True))
                q_jq = json.loads(
                    run(bf, "query", "--jq", "[$ENV, env]", "-n", "1", cwd=brain, env=shim_env, capture=True)
                )
                if q_rg.get("count", 0) < 1 or q_jq.get("items") != [[{}, {}]]:
                    raise SystemExit(f"{artifact.name} ({label}): shim smoke failed: {q_rg} / {q_jq}")


if __name__ == "__main__":
    main()
