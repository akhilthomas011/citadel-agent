# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Offline acceptance inside the actual production Linux image, never a cloud readiness test.

Mount this script read-only and run with --network none --read-only --tmpfs /tmp.
Only cloud state, sign-in and service-token boundaries are fixtures. The installed
assets, compiler, FastAPI routes, static UI and real Uvicorn HTTP listener are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from azure.core.exceptions import AzureError
from fastapi import FastAPI

from citadel_agent import paths
from citadel_agent.portal.api import create_agent_app
from citadel_agent.portal.auth import EntraServiceTokenValidator, EntraTokenSettings, EntraTokenValidator
from citadel_agent.portal.auth_web import PortalAuthConfig
from citadel_agent.portal.bff import HttpxAgentJobStatusClient, create_bff_app
from citadel_agent.upstream.lock import read_lock

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ORIGIN = "https://portal.example.test"
PORT = 8000


class OfflineState:
    """Cloud-state test double, not a production persistence adapter."""

    unavailable = False

    def probe(self) -> None:
        if self.unavailable:
            raise AzureError("Offline state-store failure fixture.")

    def get_session(self, record_id: str, now) -> None:
        return None

    def __getattr__(self, name: str):
        raise AssertionError(f"Unexpected offline dependency call: {name}")


class OfflineOAuth:
    def begin(self):
        raise AssertionError("Container smoke must not initiate an external sign-in.")

    def complete(self, flow, response):
        raise AssertionError("Container smoke must not exchange a real sign-in code.")


class OfflineTokenProvider:
    def get_token(self) -> str:
        raise AssertionError("Anonymous requests must not acquire an Azure service token.")


def verify_artifacts() -> None:
    assert sys.platform == "linux", "Run acceptance in the production Linux image."
    assert os.geteuid() == 10001, "The image must execute as its non-root runtime user."
    assert paths.ENV_HOME not in os.environ, "Do not replace installed package assets with a checkout."
    root = paths.find_project_root()
    assert paths.is_bundled_root(root), "Acceptance must use installed wheel assets."
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    observed = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert observed == manifest, "Installed assets differ from their pinned manifest."
    lock = read_lock(root)
    assert (root / lock.upstream.path / "release.json").is_file()
    package = root.parent
    assert not list(package.rglob("*.pyd")), "Windows binary modules must not enter the Linux image."
    for path in (
        Path("/home/citadel/.npmrc"),
        Path("/build"),
        Path("/run/secrets/npmrc"),
    ):
        assert not path.exists(), "Build configuration must not be copied into the runtime filesystem."
    # Imports exercise native Linux dependencies, not just Python module discovery.
    import cryptography.hazmat.bindings._rust  # noqa: F401
    import jiter  # noqa: F401
    import pydantic_core  # noqa: F401

    result = subprocess.run(["bicep", "--version"], capture_output=True, text=True, timeout=30, check=True)  # noqa: S607
    assert "Bicep CLI version" in result.stdout
    with TemporaryDirectory(prefix="citadel-compiler-") as directory:
        source = Path(directory) / "smoke.bicep"
        source.write_text("output ready bool = true\n", encoding="utf-8")
        subprocess.run(  # noqa: S603
            ["bicep", "build", str(source)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert json.loads(source.with_suffix(".json").read_text())["outputs"]["ready"]["value"] is True
    for mode in ("agent", "assistant-worker"):
        unconfigured = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "citadel_agent.portal.server", mode],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert unconfigured.returncode != 0, f"The {mode} entry point must reject missing configuration."
        assert "ValidationError" in unconfigured.stderr and "tenant_id" in unconfigured.stderr
    print(
        f"Artifact acceptance: UID 10001, {len(manifest)} pinned assets, "
        "Linux imports, Bicep compile and agent/worker unconfigured-startup refusal pass."
    )


def verify_http(app: FastAPI, state: OfflineState, *, browser: bool) -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=PORT,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            timeout_graceful_shutdown=5,
        )
    )
    worker = threading.Thread(target=server.run, name="container-smoke-http")
    worker.start()
    try:
        for _ in range(100):
            if server.started:
                break
            assert worker.is_alive(), "The real HTTP listener failed during startup."
            time.sleep(0.05)
        assert server.started, "The real HTTP listener did not become ready."
        origin = f"http://127.0.0.1:{PORT}"
        with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
            assert client.get("/health/live").json() == {"status": "live"}
            assert client.get("/health/ready").json() == {"status": "ready"}
            state.unavailable = True
            failed = client.get("/health/ready")
            assert failed.status_code == 503
            assert "fixture" not in failed.text
            state.unavailable = False
            assert client.get("/health/ready").status_code == 200
            if browser:
                for path in ("/", "/assistant", "/finops", "/operations"):
                    response = client.get(path)
                    assert response.status_code == 200
                    assert "Citadel" in response.text
                    assert response.headers["x-frame-options"] == "DENY"
                    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
                html = client.get("/").text
                assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
                assert len(assets) >= 2, "The image must ship the production JS and CSS."
                for path in assets:
                    assert client.get(path).status_code == 200
                assert client.get("/auth/session").json() == {"authenticated": False}
                assert client.get("/api/portal").status_code == 401
                assert client.post("/api/conversations", json={}).status_code == 401
                assert client.get("/api/unknown").status_code == 404
            else:
                response = client.get("/internal/jobs/" + "a" * 64)
                assert response.status_code == 401, "The private API must not trust an anonymous caller."
            command = (
                "import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).close()"
            )
            subprocess.run([sys.executable, "-c", command], check=True, timeout=10)  # noqa: S603
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        assert not worker.is_alive(), "The smoke HTTP listener did not shut down cleanly."
    print(
        f"{'BFF' if browser else 'Agent'} HTTP acceptance: "
        "liveness, readiness failure/recovery and auth guards pass."
    )


def verify_bundle(bundle: Path, wheel_path: Path) -> None:
    with zipfile.ZipFile(bundle) as archive, zipfile.ZipFile(wheel_path) as wheel:
        names = archive.namelist()
        assert not any(
            name.lower().endswith((".pyd", ".dll", ".exe", ".egg-link", "/direct_url.json")) for name in names
        ), "The BFF bundle contains Windows binaries or machine-specific installation metadata."
        assert all(
            (
                not PurePosixPath(name).is_absolute()
                and ".." not in PurePosixPath(name).parts
                and "\\" not in name
            )
            for name in names
        ), "The BFF bundle contains unsafe archive paths."
        prefix = ".python_packages/lib/site-packages/"
        packaged = [
            name for name in wheel.namelist() if name.startswith("citadel_agent/") and not name.endswith("/")
        ]
        assert packaged, "The reference wheel contains no agent package."
        for name in packaged:
            assert archive.read(prefix + name) == wheel.read(name), f"BFF/wheel content differs: {name}"
        with TemporaryDirectory(prefix="citadel-zip-") as directory:
            archive.extractall(directory)
            site = str(Path(directory) / prefix)
            command = """
import sys, pathlib
sys.path.insert(0, sys.argv[1])
import citadel_agent.portal.worker as worker
import citadel_agent.portal.release as release
import citadel_agent.portal.server as server
import pydantic_core, cryptography.hazmat.bindings._rust as crypto, jiter, msal, azure.cosmos
site = pathlib.Path(sys.argv[1]).resolve()
for module in (worker, release, server, pydantic_core, crypto, jiter, msal, azure.cosmos):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(site), module.__name__
assert not any('site-packages' in p and not pathlib.Path(p).is_relative_to(site) for p in sys.path)
print('Standalone Linux ZIP imports passed with only its own site-packages and stdlib.')
"""
            subprocess.run(  # noqa: S603
                [sys.executable, "-I", "-S", "-c", command, site],
                cwd=directory,
                check=True,
                timeout=60,
            )
        print(f"Portable bundle acceptance: {len(packaged)} wheel package files match; no image fallback.")


def verify_installer(bundle: Path) -> None:
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        assert all(
            not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts for name in names
        ), "Unsafe installer archive path."
        assert not any(
            name.lower().endswith((".pyd", ".dll", ".exe", ".egg-link", "/direct_url.json")) for name in names
        ), "Installer contains incompatible binaries or machine-specific metadata."
        manifest = json.loads(archive.read("installer-manifest.json"))
        assert manifest["files"] == {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name != "installer-manifest.json"
        }
        assert {"install.py", "update.py", "installation-schema.json", "infra/main.bicep"} <= set(names)
        with TemporaryDirectory(prefix="citadel-installer-") as directory:
            archive.extractall(directory)
            for entry in ("install.py", "update.py"):
                result = subprocess.run(  # noqa: S603
                    [sys.executable, "-I", "-S", str(Path(directory) / entry), "--help"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=60,
                )
                assert "--apply" in result.stdout
    print("Sealed installer acceptance: exact content map and isolated install/update entrypoints pass.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--installer", type=Path)
    args = parser.parse_args(argv)
    if (args.bundle is None) != (args.wheel is None):
        parser.error("--bundle and --wheel must be supplied together")
    verify_artifacts()
    if args.bundle is not None:
        verify_bundle(args.bundle, args.wheel)
    if args.installer is not None:
        verify_installer(args.installer)
    settings = EntraTokenSettings(
        tenant_id=TENANT,
        audience=CLIENT,
        allowed_client_ids=frozenset({CLIENT}),
    )
    state = OfflineState()
    agent = create_agent_app(
        service_token_validator=EntraServiceTokenValidator(settings),
        session_store=state,
        grant_store=state,
        job_store=state,
        readiness_probe=state.probe,
    )
    verify_http(agent, state, browser=False)
    http = httpx.AsyncClient(timeout=5, trust_env=False)
    private = HttpxAgentJobStatusClient(http, "https://agent.example.test")
    bff = create_bff_app(
        auth_config=PortalAuthConfig(origin=ORIGIN, callback_uri=ORIGIN + "/auth/callback"),
        oauth_client=OfflineOAuth(),
        delegated_token_validator=EntraTokenValidator(settings),
        session_store=state,
        agent_token_provider=OfflineTokenProvider(),
        agent_client=private,
        product_client=private,
        readiness_probe=state.probe,
    )
    bff.router.add_event_handler("shutdown", http.aclose)
    verify_http(bff, state, browser=True)
    print(
        "Offline container acceptance passed. Azure identity, data access and model calls are NOT verified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
