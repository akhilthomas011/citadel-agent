"""Regression checks for dependency installation before credentialed jobs."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "templates" / "contracts-repo" / ".github" / "workflows"


@pytest.mark.parametrize("name", ["citadel-validate", "citadel-whatif", "citadel-deploy"])
def test_contracts_install_the_pinned_checkout_with_a_lock(name: str) -> None:
    workflow = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    installer = next(step for step in steps if "CITADEL_AGENT_PACKAGE" in step.get("env", {}))
    script = installer["run"]
    assert "[0-9a-f]{40}" in script
    assert 'test "$(git -C "$agent" rev-parse HEAD)" = "$commit"' in script
    assert "submodule update --init --recursive" in script
    assert "--group build --no-install-project --no-build --python 3.11" in script
    assert "--group build --no-editable --no-build-isolation --python 3.11" in script
    assert "uv tool install" not in script
    for step in steps:
        if step.get("uses", "").startswith("azure/login@"):
            assert steps.index(installer) < steps.index(step)


@pytest.mark.parametrize("name", ["ci", "upstream-sync"])
def test_test_workflows_do_not_resolve_dependencies_implicitly(name: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8"))
    commands = [
        step["run"] for job in workflow["jobs"].values() for step in job.get("steps", []) if "run" in step
    ]
    for command in commands:
        for line in command.splitlines():
            if line.strip().startswith(("uv sync ", "uv run ")):
                assert "--locked" in line


@pytest.mark.parametrize(
    ("package", "accepted"),
    [
        ("git+https://github.com/owner/agent@" + "a" * 40, True),
        ("git+https://github.com/owner/agent@main", False),
        ("git+https://github.com/owner/agent@" + "a" * 39, False),
        ("git+https://other.example/owner/agent@" + "a" * 40, False),
        ("$(touch injected)", False),
        ("", False),
    ],
)
def test_installer_rejects_unpinned_inputs_before_running_commands(
    package: str, accepted: bool, tmp_path: Path
) -> None:
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    if not bash:
        pytest.skip("Bash is needed to execute the workflow installer")
    workflow = yaml.safe_load((WORKFLOWS / "citadel-deploy.yml").read_text(encoding="utf-8"))
    script = next(
        step["run"]
        for step in workflow["jobs"]["deploy"]["steps"]
        if "CITADEL_AGENT_PACKAGE" in step.get("env", {})
    )
    mocks = """
    git() {
      printf 'git-called\\n' >&2
      if [[ "$*" == *"rev-parse HEAD" ]]; then
        printf '%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      fi
    }
    uv() { printf 'locked-install %s\\n' "$*"; }
    """
    result = subprocess.run(  # noqa: S603
        [bash, "--noprofile", "--norc", "-c", mocks + script],
        cwd=tmp_path,
        env={
            **os.environ,
            "CITADEL_AGENT_PACKAGE": package,
            "RUNNER_TEMP": ".",
            "GITHUB_PATH": "github-path",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert (result.returncode == 0) is accepted, result.stderr
    assert ("locked-install" in result.stdout) is accepted
    assert ("git-called" in result.stderr) is accepted
    assert not (tmp_path / "injected").exists()
