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
# The installed `bf mcp` over stdio, with the installed mcp SDK: the other public interface and its dependency.
MCP = """
import asyncio, os, sys
import bf
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check() -> None:
    executable, brain = sys.argv[1:3]
    parameters = StdioServerParameters(
        command=executable,
        args=["mcp", "--brain", brain],
        env={key: os.environ[key] for key in ("HOME", "PATH", "XDG_STATE_HOME", "XDG_CONFIG_HOME")},
    )
    async with (
        stdio_client(parameters) as (incoming, outgoing),
        ClientSession(incoming, outgoing, read_timeout_seconds=30) as session,
    ):
        initialized = await session.initialize()
        assert initialized.server_info.version == bf.__version__
        listing = await session.list_tools()
        assert {tool.name for tool in listing.tools} == {"search", "read"}
        found = await session.call_tool("search", {"query": "welcome"})
        assert not found.is_error
        assert any(item["ref"] == "concepts/welcome.md" for item in found.structured_content["items"])
        note = await session.call_tool("read", {"ref": "concepts/welcome.md"})
        assert not note.is_error
        assert note.structured_content["text"]


asyncio.run(check())
"""


def run(*args: str, cwd: Path, env: dict[str, str], capture: bool = False) -> str:
    # Callers supply fixed CLI operations and local artifact paths; no shell is used.
    result = subprocess.run(  # noqa: S603
        args, cwd=cwd, env=env, check=True, timeout=180, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout or ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="3.14", help="Interpreter version for the isolated environments.")
    parser.add_argument("--unlocked", action="store_true", help="Ignore uv.lock and resolve the newest dependencies.")
    args = parser.parse_args()
    if UV is None:
        raise SystemExit("uv is required")
    # UV is the resolved toolchain executable, and both arguments are literal.
    cache = subprocess.check_output(  # noqa: S603
        [UV, "cache", "dir"], text=True, timeout=30
    ).strip()
    artifacts = [
        list((ROOT / "dist").glob(pattern)) for pattern in ("brain_framework-*.whl", "brain_framework-*.tar.gz")
    ]
    if any(len(matches) != 1 for matches in artifacts):
        raise SystemExit("expected exactly one brain-framework wheel and one source distribution")
    with tempfile.TemporaryDirectory(prefix="bf-package-") as temporary:
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
            env.pop("BF_BRAIN", None)
            env.pop("PYTHONPATH", None)
            env.pop("UV_PROJECT_ENVIRONMENT", None)
            run(UV, "venv", "--python", args.python, str(venv), cwd=home, env=env)
            python = str(venv / "bin/python")
            if args.unlocked:
                run(UV, "pip", "install", "--python", python, str(artifact), cwd=home, env=env)
            else:
                run(
                    UV,
                    "sync",
                    "--project",
                    str(ROOT),
                    "--locked",
                    "--offline",
                    "--no-dev",
                    "--no-install-project",
                    "--active",
                    cwd=home,
                    env=env,
                )
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
import bf
from bf.watch import Dashboard
from bf.schemas import document

assert Dashboard("package-smoke").render(80, 24)
for kind, title in [("brain", "Config"), ("watch", "Settings"), ("registry", "UserConfig"), ("eval", "Suite")]:
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
            run(bf, "watch", "--help", cwd=home, env=env)
            run(bf, "schedule", "--help", cwd=home, env=env)
            run(bf, "--version", cwd=home, env=env)
            run(bf, "init", str(brain), cwd=home, env=env)
            if (home / "config/bf/config.yaml").exists():
                raise SystemExit("init unexpectedly registered the smoke-test brain")
            (brain / "projects/package.md").write_text(
                "---\ntype: project\nstatus: draft\nreview_after: 7\n---\n# Package check\n\n"
                "## Next\n\n- [ ] Inspect the installed task page.\n"
            )
            replies = {
                arguments: json.loads(run(bf, *arguments, cwd=brain, env=env, capture=True))
                for arguments in [
                    ("validate",),
                    ("search", "welcome"),
                    ("read", "concepts/welcome.md"),
                    ("read", "projects"),
                    ("read", "tasks"),
                    ("status", "--check"),
                    ("eval",),
                ]
            }
            for arguments, reply in replies.items():
                if not isinstance(reply, dict) or reply.get("problems") or reply.get("stale"):
                    raise SystemExit(f"{artifact.name}: incomplete reply from {' '.join(arguments)}")
            checks = {
                "validation": replies[("validate",)].get("valid") is True,
                "search": any(
                    item.get("ref") == "concepts/welcome.md" for item in replies[("search", "welcome")].get("items", [])
                ),
                "exact read": replies[("read", "concepts/welcome.md")].get("text")
                == (brain / "concepts/welcome.md").read_text(),
                "projects": any(
                    item.get("ref") == "projects/package.md" for item in replies[("read", "projects")].get("items", [])
                ),
                "tasks": any(
                    item.get("ref") == "projects/package.md#next"
                    and item.get("text") == "Inspect the installed task page."
                    for item in replies[("read", "tasks")].get("items", [])
                ),
                "health": replies[("status", "--check")].get("healthy") is True,
                "evaluation": replies[("eval",)].get("passed") is True and bool(replies[("eval",)].get("cases")),
            }
            if failed := [name for name, passed in checks.items() if not passed]:
                raise SystemExit(f"{artifact.name}: incorrect package smoke results: {', '.join(failed)}")
            run(python, "-I", "-c", MCP, bf, str(brain), cwd=home, env=env)


if __name__ == "__main__":
    main()
