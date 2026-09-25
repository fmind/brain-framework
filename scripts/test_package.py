"""Install both release artifacts with locked dependencies and exercise the public CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")


def run(*args: str, cwd: Path, env: dict[str, str]) -> None:
    # Callers supply fixed CLI operations and local artifact paths; no shell is used.
    subprocess.run(args, cwd=cwd, env=env, check=True, timeout=180)  # noqa: S603


def main() -> None:
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
            run(UV, "venv", "--python", "3.14", str(venv), cwd=home, env=env)
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
            python = str(venv / "bin/python")
            run(UV, "pip", "install", "--offline", "--no-deps", "--python", python, str(artifact), cwd=home, env=env)
            run(UV, "pip", "check", "--python", python, cwd=home, env=env)
            run(
                python,
                "-I",
                "-c",
                """
from importlib.metadata import distribution
from pathlib import Path
import bf

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
            run(bf, "--version", cwd=home, env=env)
            run(bf, "init", str(brain), cwd=home, env=env)
            if (home / "config/bf/config.yaml").exists():
                raise SystemExit("init unexpectedly registered the smoke-test brain")
            for arguments in [
                ("validate",),
                ("search", "welcome"),
                ("read", "concepts/welcome.md"),
                ("status", "--check"),
            ]:
                run(bf, *arguments, cwd=brain, env=env)
            (brain / "evals").mkdir(exist_ok=True)
            (brain / "evals/retrieval.yaml").write_text(
                "version: 5\ncases:\n  - name: installed-welcome\n    query: welcome\n    expect: [concepts/welcome.md]\n"
            )
            run(bf, "eval", cwd=brain, env=env)


if __name__ == "__main__":
    main()
