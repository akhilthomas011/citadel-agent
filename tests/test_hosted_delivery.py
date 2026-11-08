"""Hosted artifact integrity and safe deployment preflights without Azure mutations."""

import argparse
import copy
import csv
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(result)
    finally:
        sys.path.pop(0)
    return result


@pytest.fixture
def delivery():
    return module("deploy_hosted")


def hosts():
    web = {
        "kind": "app,linux",
        "reserved": True,
        "virtualNetworkSubnetId": "/subscriptions/test/vnet/subnets/bff",
    }
    agent = {
        "properties": {
            "environmentId": "/subscriptions/test/environments/private",
            "configuration": {
                "ingress": {
                    "external": True,
                    "allowInsecure": False,
                    "targetPort": 8000,
                    "fqdn": "agent.private.example",
                }
            },
            "template": {
                "containers": [
                    {
                        "probes": [{"type": "Readiness"}],
                        "env": [
                            {"name": name, "value": "configured"}
                            for name in (
                                "CITADEL_PORTAL_HUB_CONFIG",
                                "CITADEL_PORTAL_BFF_SERVICE_CLIENT_ID",
                                "CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID",
                            )
                        ],
                    }
                ]
            },
        },
    }
    environment = {
        "properties": {
            "vnetConfiguration": {
                "internal": True,
                "infrastructureSubnetId": "/subscriptions/test/vnet/subnets/aca",
            }
        }
    }
    return web, agent, environment


IMAGE = "ghcr.io/example/citadel@sha256:" + "a" * 64


def test_private_topology_preflight_rejects_public_environment_and_environment_only_ingress(delivery):
    web, agent, environment = hosts()
    delivery.validate_hosts(web, agent, environment, IMAGE)
    public = copy.deepcopy(environment)
    public["properties"]["vnetConfiguration"]["internal"] = False
    with pytest.raises(ValueError, match="INTERNAL"):
        delivery.validate_hosts(web, agent, public, IMAGE)
    internal_only = copy.deepcopy(agent)
    internal_only["properties"]["configuration"]["ingress"]["external"] = False
    with pytest.raises(ValueError, match="VNet-reachable"):
        delivery.validate_hosts(web, internal_only, environment, IMAGE)
    with pytest.raises(ValueError, match="digest"):
        delivery.validate_hosts(web, agent, environment, "ghcr.io/example/citadel:latest")
    with pytest.raises(ValueError, match="Windows"):
        delivery.validate_hosts({**web, "reserved": False}, agent, environment, IMAGE)


def test_deploy_defaults_to_validation_only_and_does_not_change_host_state(delivery, tmp_path):
    calls = []
    web, agent, environment = hosts()
    bundle = tmp_path / "citadel-portal.zip"
    bundle.write_bytes(b"built-artifact")

    def command(arguments):
        calls.append(arguments)
        if arguments[:2] == ["webapp", "show"]:
            return web
        if arguments[:2] == ["containerapp", "show"]:
            return agent
        if arguments[:2] == ["resource", "show"]:
            return environment
        if arguments[:4] == ["webapp", "config", "appsettings", "list"]:
            return [
                {"name": "CITADEL_PORTAL_AGENT_API_URL", "value": "https://agent.private.example"},
                {"name": "CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID", "value": "bff-id"},
                {"name": "CITADEL_PORTAL_PORTAL_ORIGIN", "value": "https://portal.example"},
            ]
        pytest.fail(f"Unexpected mutating command: {arguments}")

    args = argparse.Namespace(
        subscription="test",
        web_resource_group="web",
        web_app="portal",
        agent_resource_group="agent",
        agent_app="agent",
        agent_image=IMAGE,
        bundle=bundle,
        apply=False,
    )
    delivery.deploy(args, command=command)
    assert len(calls) == 4
    assert all("--subscription" in call for call in calls)


def test_appservice_bundle_contains_installed_ui_and_no_checkout_state(tmp_path):
    preparation = module("prepare_hosted")
    packages = tmp_path / "site-packages"
    ui = packages / "citadel_agent" / "portal" / "static"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html>Citadel</html>")
    (packages / "citadel_agent" / "__pycache__").mkdir()
    (packages / "citadel_agent" / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    path = preparation.bundle_site_packages(packages, tmp_path / "output")
    with zipfile.ZipFile(path) as archive:
        assert (
            ".python_packages/lib/site-packages/citadel_agent/portal/static/index.html" in archive.namelist()
        )
        assert "app.py" in archive.namelist()
        assert not any("__pycache__" in name or ".env" in name for name in archive.namelist())
    manifest = json.loads((path.parent / "hosted-manifest.json").read_text())
    assert manifest["file"] == path.name
    assert len(manifest["sha256"]) == 64


def test_missing_ui_cannot_produce_a_successful_hosted_bundle(tmp_path):
    preparation = module("prepare_hosted")
    with pytest.raises(ValueError, match="portal UI"):
        preparation.bundle_site_packages(tmp_path / "missing", tmp_path / "output")


def test_portable_bundle_omits_build_origins_launchers_and_their_record_entries(tmp_path):
    preparation = module("prepare_hosted")
    packages = tmp_path / "site-packages"
    ui = packages / "citadel_agent" / "portal" / "static"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html>Citadel</html>")
    metadata = packages / "citadel_agent-0.1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: citadel-agent\nVersion: 0.1.0\n")
    (metadata / "direct_url.json").write_text(json.dumps({"url": "file:///private-builder/project.whl"}))
    launchers = packages / "bin"
    launchers.mkdir()
    (launchers / "citadel-agent.exe").write_bytes(b"Windows launcher")
    (metadata / "RECORD").write_text(
        "citadel_agent/portal/static/index.html,,\n"
        "citadel_agent-0.1.0.dist-info/METADATA,,\n"
        "citadel_agent-0.1.0.dist-info/direct_url.json,,\n"
        "bin/citadel-agent.exe,,\n"
        "../../../bin/citadel-agent,,\n"
        "citadel_agent-0.1.0.dist-info/RECORD,,\n"
    )
    bundle = preparation.bundle_site_packages(packages, tmp_path / "output")
    prefix = ".python_packages/lib/site-packages/"
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        assert not any(name.endswith(".exe") or name.endswith("direct_url.json") for name in names)
        rows = list(
            csv.reader(io.StringIO(archive.read(prefix + "citadel_agent-0.1.0.dist-info/RECORD").decode()))
        )
        assert len(rows) == 3
        assert all(prefix + row[0] in names for row in rows)
        assert rows[-1] == ["citadel_agent-0.1.0.dist-info/RECORD", "", ""]
        assert b"private-builder" not in b"".join(archive.read(name) for name in names)
    manifest = json.loads(bundle.with_name("hosted-manifest.json").read_text())
    assert manifest["packageFiles"] == 3
    assert (metadata / "direct_url.json").is_file(), "Packaging must not mutate the build environment."


@pytest.mark.parametrize("native_name", ["native.pyd", "native.dll", "native.exe", "__editable__.pth"])
def test_portable_bundle_rejects_incompatible_or_editable_dependencies(tmp_path, native_name):
    preparation = module("prepare_hosted")
    packages = tmp_path / "site-packages"
    ui = packages / "citadel_agent" / "portal" / "static"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html>Citadel</html>")
    (packages / native_name).write_bytes(b"nonportable")
    with pytest.raises(ValueError, match=r"portable|editable|Windows"):
        preparation.bundle_site_packages(packages, tmp_path / "output")
    assert not (tmp_path / "output" / "citadel-portal.zip").exists()


def test_sealed_installer_contains_pinned_templates_schema_and_complete_integrity_map(tmp_path):
    preparation = module("prepare_hosted")
    packages = tmp_path / "site-packages"
    ui = packages / "citadel_agent" / "portal" / "static"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html>Citadel</html>")
    project = tmp_path / "project"
    templates = project / "infra"
    (templates / "modules").mkdir(parents=True)
    (templates / "main.bicep").write_text("output ready bool = true\n")
    (templates / "modules" / "example.bicep").write_text("output moduleReady bool = true\n")
    (templates / "private-customer.json").write_text('{"mustNotShip": true}')
    (project / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    output = tmp_path / "output"
    preparation.bundle_site_packages(packages, output)
    installer = preparation.bundle_installer(packages, project, output)
    with zipfile.ZipFile(installer) as archive:
        names = archive.namelist()
        assert "infra/main.bicep" in names and "infra/modules/example.bicep" in names
        assert "infra/private-customer.json" not in names
        schema = json.loads(archive.read("installation-schema.json"))
        assert "subscriptionId" in schema["properties"]
        sealed = json.loads(archive.read("installer-manifest.json"))
        assert sealed["packageVersion"] == "0.1.0"
        assert sealed["files"] == {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name != "installer-manifest.json"
        }
        for entry in ("bootstrap.py", "install.py", "update.py"):
            compile(archive.read(entry), entry, "exec")
        assert b"Templates are pinned" in archive.read("install.py")
        assert b"customer" not in archive.read("installer-manifest.json")
    manifest = json.loads((output / "hosted-manifest.json").read_text())
    assert manifest["installerFile"] == installer.name
    assert manifest["installerSha256"] == hashlib.sha256(installer.read_bytes()).hexdigest()
    assert "sourceSha" not in manifest and manifest.get("imagePublished") is not True


def test_sealed_installer_refuses_a_changed_bff_before_packaging(tmp_path):
    preparation = module("prepare_hosted")
    packages = tmp_path / "site-packages"
    ui = packages / "citadel_agent" / "portal" / "static"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html>Citadel</html>")
    project = tmp_path / "project"
    (project / "infra").mkdir(parents=True)
    (project / "infra" / "main.bicep").write_text("output ready bool = true\n")
    output = tmp_path / "output"
    portal = preparation.bundle_site_packages(packages, output)
    portal.write_bytes(b"changed")
    with pytest.raises(ValueError, match="does not match"):
        preparation.bundle_installer(packages, project, output)
    assert not (output / "citadel-installer.zip").exists()


def test_customer_install_uses_verified_sealed_tooling_without_source_or_dependency_builds():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "install-hosted.yml").read_text())
    steps = workflow["jobs"]["install"]["steps"]
    assert not any("actions/checkout@" in step.get("uses", "") for step in steps)
    assert not any("setup-uv" in step.get("uses", "") for step in steps)
    validation = next(
        i for i, step in enumerate(steps) if "Validate artifact manifest" in step.get("name", "")
    )
    login = next(i for i, step in enumerate(steps) if "azure/login@" in step.get("uses", ""))
    assert validation < login
    assert "installerSha256" in steps[validation]["run"]
    assert "SOURCE_SHA" in steps[validation]["env"]
    invocation = steps[-1]["run"]
    assert "python -I -S build/installer/install.py" in invocation
    assert all(command not in invocation for command in ("uv sync", "pip install", "npm"))
    assert "--template" not in invocation
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["apply"]["default"] is False


@pytest.mark.parametrize("corruption", ["none", "source", "image", "portal", "installer", "path"])
def test_customer_workflow_checks_real_hashes_before_extracting_code(tmp_path, monkeypatch, corruption):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "install-hosted.yml").read_text())
    validation = next(
        step["run"]
        for step in workflow["jobs"]["install"]["steps"]
        if "Validate artifact manifest" in step.get("name", "")
    )
    script = validation.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    artifacts = tmp_path / "build" / "hosted"
    artifacts.mkdir(parents=True)
    portal = artifacts / "citadel-portal.zip"
    portal.write_bytes(b"verified BFF fixture")
    installer = artifacts / "citadel-installer.zip"
    with zipfile.ZipFile(installer, "w") as archive:
        archive.writestr("../outside.py" if corruption == "path" else "install.py", "# fixture")
    sha = "d" * 40
    manifest = {
        "file": portal.name,
        "sha256": hashlib.sha256(portal.read_bytes()).hexdigest(),
        "installerFile": installer.name,
        "installerSha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
        "sourceSha": "e" * 40 if corruption == "source" else sha,
        "imagePublished": corruption != "image",
        "agentImage": IMAGE,
    }
    (artifacts / "hosted-manifest.json").write_text(json.dumps(manifest))
    if corruption in {"portal", "installer"}:
        (portal if corruption == "portal" else installer).write_bytes(b"changed")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SOURCE_SHA", sha)
    monkeypatch.setenv("AGENT_IMAGE", IMAGE)
    if corruption == "none":
        exec(compile(script, "trusted-workflow-validation", "exec"), {})  # noqa: S102
        assert (tmp_path / "build" / "installer" / "install.py").is_file()
    else:
        with pytest.raises(SystemExit, match=r"manifest|Unsafe"):
            exec(compile(script, "trusted-workflow-validation", "exec"), {})  # noqa: S102
        assert not (tmp_path / "build" / "installer").exists()
        assert not (tmp_path / "outside.py").exists()


def test_customer_workflow_is_manual_and_validates_before_oidc():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy-hosted.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["apply"]["default"] is False
    assert workflow["jobs"]["deploy"]["needs"] == "verify"
    assert workflow["jobs"]["verify"]["uses"] == "./.github/workflows/verify-hosted-artifact.yml"
    verify = (ROOT / ".github" / "workflows" / "verify-hosted-artifact.yml").read_text()
    assert ".head_sha" in verify and ".head_branch" in verify and "publish-hosted.yml" in verify
    assert "id-token" not in workflow["jobs"]["verify"]["permissions"]
    assert workflow["jobs"]["deploy"]["permissions"]["id-token"] == "write"
    assert workflow["jobs"]["deploy"]["environment"] == "${{ inputs.environment }}"
    assert "az deployment" not in str(workflow)


def test_container_context_excludes_development_secrets_and_requires_verified_bicep():
    ignore = (ROOT / ".dockerignore").read_text()
    assert ignore.startswith("**\n")
    assert "!src/citadel_agent/" in ignore
    assert "!.env" not in ignore and "!.citadel-agent/" not in ignore
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'test -n "$BICEP_SHA256"' in dockerfile
    assert "sha256sum --check" in dockerfile
    assert "USER citadel" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "pip install --no-cache-dir --no-compile --no-deps" in dockerfile
    assert "DOTNET_BUNDLE_EXTRACT_BASE_DIR=/opt/citadel-dotnet" in dockerfile


def test_frontend_lock_is_integrity_pinned_and_registry_portable():
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text())
    packages = {name: item for name, item in lock["packages"].items() if name}
    assert all(item.get("version") and item.get("integrity") for item in packages.values())
    assert all("resolved" not in item for item in packages.values())
    assert "node_modules/@rolldown/binding-linux-x64-gnu" in packages
    assert "node_modules/@rolldown/binding-win32-x64-msvc" in packages


def test_bff_dependencies_target_portable_linux_wheels_not_the_build_runner_abi():
    workflow = (ROOT / ".github" / "workflows" / "publish-hosted.yml").read_text()
    assert "--python-platform x86_64-manylinux_2_28" in workflow
    assert "--only-binary :all: --require-hashes" in workflow


def test_generated_assets_build_exactly_once_without_git_or_ignore_files(tmp_path):
    wheel_builder = pytest.importorskip("hatchling.builders.wheel").WheelBuilder
    sdist_builder = pytest.importorskip("hatchling.builders.sdist").SdistBuilder
    for name in ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md", "uv.lock"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts" / "build_assets.py", scripts / "build_assets.py")
    package = tmp_path / "src" / "citadel_agent"
    assets = package / "_assets"
    ui = package / "portal" / "static"
    assets.mkdir(parents=True)
    (ui / "assets").mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (package / "portal" / "__init__.py").write_text("")
    (assets / "manifest.json").write_text("{}")
    (assets / "citadel.lock.json").write_text("{}")
    (ui / "index.html").write_text("<html>Smoke</html>")
    (ui / "assets" / "app.js").write_text("console.log('smoke');")
    (ui / "assets" / "app.css").write_text("body {}")
    output = tmp_path / "dist"
    wheel_name = next(wheel_builder(str(tmp_path)).build(directory=str(output)))
    with zipfile.ZipFile(output / wheel_name) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert "citadel_agent/_assets/citadel.lock.json" in names
        assert "citadel_agent/portal/static/index.html" in names
    sdist_name = next(sdist_builder(str(tmp_path)).build(directory=str(output)))
    with tarfile.open(output / sdist_name) as archive:
        names = archive.getnames()
        assert len(names) == len(set(names))
        assert any(name.endswith("/src/citadel_agent/_assets/citadel.lock.json") for name in names)
        assert any(name.endswith("/src/citadel_agent/portal/static/index.html") for name in names)


def test_container_mirror_configuration_uses_secret_mount_not_copy_or_build_arg():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--mount=type=secret,id=npmrc,target=/root/.npmrc" in dockerfile
    assert "--mount=type=secret,id=pip_index,env=PIP_INDEX_URL" in dockerfile
    assert "ARG NPM_TOKEN" not in dockerfile
    assert "COPY .npmrc" not in dockerfile
    assert "strict-ssl=false" not in dockerfile


def test_image_must_pass_offline_acceptance_before_publication():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "publish-hosted.yml").read_text())
    steps = workflow["jobs"]["package"]["steps"]
    built = next(index for index, step in enumerate(steps) if step.get("id") == "container")
    checked = next(
        index for index, step in enumerate(steps) if "smoke_hosted_container.py" in step.get("run", "")
    )
    published = next(index for index, step in enumerate(steps) if step.get("id") == "published")
    assert built < checked < published
    assert steps[built]["with"]["load"] is True
    assert steps[built]["with"]["push"] is False
    assert "--network none --read-only" in steps[checked]["run"]
    assert "--cap-drop ALL --security-opt no-new-privileges" in steps[checked]["run"]
    assert steps[published]["if"] == "inputs.publish_image"
    assert steps[published]["with"]["push"] is True
    assert steps[published]["with"]["provenance"] == "mode=max"
    assert steps[0]["name"] == "Require reviewed default-branch source for image publication"
