# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Prepare verified runtime assets and a self-contained App Service ZIP without cloud access."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import tomllib
import zipfile
from pathlib import Path

_INSTALLER_BOOTSTRAP = """import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def activate():
    sys.dont_write_bytecode = True
    manifest = json.loads((ROOT / "installer-manifest.json").read_text(encoding="utf-8"))
    if any(path.is_symlink() for path in ROOT.rglob("*")):
        raise SystemExit("Installer content must not contain symbolic links.")
    actual = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in ROOT.rglob("*")
        if path.is_file() and path != ROOT / "installer-manifest.json"
    }
    if actual != manifest["files"]:
        raise SystemExit("Installer integrity check failed; re-extract the verified release.")
    sys.path.insert(0, str(ROOT / ".python_packages" / "lib" / "site-packages"))
"""


def portable_package_files(site_packages: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(site_packages.rglob("*")):
        relative = path.relative_to(site_packages)
        if relative.as_posix() in {".lock", ".gitignore"}:
            continue
        if relative.parts[0].lower() in {"bin", "scripts"} or "__pycache__" in relative.parts:
            continue
        if path.suffix == ".pyc" or (
            path.name == "direct_url.json" and path.parent.name.endswith(".dist-info")
        ):
            continue
        if not path.is_file():
            continue
        if (
            path.is_symlink()
            or path.suffix.lower() in {".exe", ".dll", ".pyd", ".egg-link"}
            or (path.name.startswith("__editable__"))
        ):
            raise ValueError(f"The bundle requires portable, non-editable Linux dependencies: {relative}")
        files[relative.as_posix()] = path
    return files


def portable_record(path: Path, name: str, files: dict[str, Path]) -> str:
    result = io.StringIO(newline="")
    writer = csv.writer(result, lineterminator="\n")
    for row in csv.reader(io.StringIO(path.read_text(encoding="utf-8"))):
        if len(row) != 3:
            raise ValueError(f"Installed dependency RECORD is malformed: {name}")
        relative = row[0].replace("\\", "/")
        if relative not in files:
            continue
        writer.writerow([relative, "", ""] if relative == name else [relative, row[1], row[2]])
    return result.getvalue()


def write_package_files(archive: zipfile.ZipFile, files: dict[str, Path]) -> None:
    for name, path in files.items():
        target = ".python_packages/lib/site-packages/" + name
        if path.name == "RECORD" and path.parent.name.endswith(".dist-info"):
            archive.writestr(target, portable_record(path, name, files))
        else:
            archive.write(path, target)


def prepare(root: Path) -> Path:
    from build_assets import build_assets

    ui = root / "src" / "citadel_agent" / "portal" / "static"
    if not (ui / "index.html").is_file() or not any((ui / "assets").glob("*.js")):
        raise ValueError("Build the frontend before preparing hosted artifacts.")
    assets = root / "src" / "citadel_agent" / "_assets"
    if assets.exists():
        raise ValueError("Generated runtime assets already exist; use a clean packaging checkout.")
    build_assets(root, assets)
    print("Pinned runtime assets and production portal UI are ready for packaging.")
    return assets


def bundle_site_packages(site_packages: Path, output: Path) -> Path:
    required = site_packages / "citadel_agent" / "portal" / "static" / "index.html"
    if not required.is_file():
        raise ValueError("The installed package must contain the built portal UI.")
    files = portable_package_files(site_packages)
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / "citadel-portal.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        write_package_files(archive, files)
        archive.writestr(
            "app.py",
            "from citadel_agent.portal.server import main\n"
            "if __name__ == '__main__':\n"
            "    main(['bff', '--port', '8000'])\n",
        )
    manifest = {
        "file": bundle.name,
        "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "packageFiles": len(files),
    }
    (output / "hosted-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Hosted bundle prepared: {bundle.name}, SHA-256 {manifest['sha256']}")
    return bundle


def bundle_installer(site_packages: Path, root: Path, output: Path) -> Path:
    from citadel_agent.portal.installation import HostingInstallConfig

    files = portable_package_files(site_packages)
    templates = root / "infra"
    if not (templates / "main.bicep").is_file() or not files:
        raise ValueError("The sealed installer requires reviewed infrastructure and installed dependencies.")
    portal = output / "citadel-portal.zip"
    manifest_path = output / "hosted-manifest.json"
    if not portal.is_file() or not manifest_path.is_file():
        raise ValueError("Prepare the matching BFF artifact before its installer.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if hashlib.sha256(portal.read_bytes()).hexdigest() != manifest.get("sha256"):
        raise ValueError("The BFF artifact does not match its manifest.")
    bundle = output / "citadel-installer.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        write_package_files(archive, files)
        for path in sorted(templates.rglob("*.bicep")):
            if path.is_symlink():
                raise ValueError("Infrastructure assets must not link outside the reviewed installer.")
            archive.write(path, "infra/" + path.relative_to(templates).as_posix())
        archive.writestr("bootstrap.py", _INSTALLER_BOOTSTRAP)
        archive.writestr(
            "install.py",
            "import sys\nsys.dont_write_bytecode = True\n"
            "from pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "from bootstrap import ROOT, activate\nactivate()\n"
            "if any(arg == '--template' or arg.startswith('--template=') for arg in sys.argv[1:]):\n"
            "    raise SystemExit('Templates are pinned; --template is not allowed.')\n"
            "from citadel_agent.portal.installation import main\n"
            "raise SystemExit(main([*sys.argv[1:], '--template', str(ROOT / 'infra' / 'main.bicep')]))\n",
        )
        archive.writestr(
            "update.py",
            "import sys\nsys.dont_write_bytecode = True\n"
            "from pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "from bootstrap import activate\nactivate()\n"
            "from citadel_agent.portal.delivery import main\nraise SystemExit(main())\n",
        )
        archive.writestr(
            "installation-schema.json",
            json.dumps(HostingInstallConfig.model_json_schema(by_alias=True), indent=2) + "\n",
        )
    # Reopening normalizes ZipInfo names before reading entries on Windows.
    with zipfile.ZipFile(bundle, "a") as archive:
        sealed = {
            "schemaVersion": 1,
            "packageVersion": tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
                "version"
            ],
            "runtime": "Linux x86_64 Python 3.11",
            "requiredRunnerTools": ["Azure CLI", "Bicep 0.42.1"],
            "supportedProfile": "existing-hosts, Azure public cloud, same-tenant single-subscription",
            "files": {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()},
        }
        archive.writestr("installer-manifest.json", json.dumps(sealed, indent=2) + "\n")
    manifest.update(
        installerFile=bundle.name,
        installerSha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Sealed installer prepared: {bundle.name}, SHA-256 {manifest['installerSha256']}")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("build") / "hosted")
    parser.add_argument("--site-packages", type=Path)
    parser.add_argument(
        "--installer", action="store_true", help="Also seal installed tooling and reviewed IaC."
    )
    args = parser.parse_args()
    if args.installer and args.site_packages is None:
        parser.error("--installer requires --site-packages")
    if args.site_packages is not None:
        bundle_site_packages(args.site_packages.resolve(), args.output.resolve())
        if args.installer:
            bundle_installer(args.site_packages.resolve(), args.root.resolve(), args.output.resolve())
    else:
        prepare(args.root.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
