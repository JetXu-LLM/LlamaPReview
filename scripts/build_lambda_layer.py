#!/usr/bin/env python3
"""Build and verify a deterministic minimal Lambda Layer from a hash lock."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

try:
    from lambda_manifest import ROOT_DIR, load_manifest
except ModuleNotFoundError:  # Imported as scripts.build_lambda_layer in tests.
    from scripts.lambda_manifest import ROOT_DIR, load_manifest


ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
REQUIRED_IMPORT_ROOTS = {
    "certifi",
    "cffi",
    "charset_normalizer",
    "cryptography",
    "dateutil",
    "github",
    "idna",
    "jwt",
    "llama_github",
    "nacl",
    "requests",
    "urllib3",
}


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_pip(args: list[str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "--disable-pip-version-check", "--no-input", *args],
        cwd=ROOT_DIR,
        check=True,
        stdout=sys.stderr,
    )


def verify_sdk_wheel_identity(manifest: dict, wheel: Path) -> None:
    """Fail before installation unless the public SDK artifact is exact."""

    release = manifest.get("sdk_release")
    if not isinstance(release, dict):
        raise ValueError("Manifest is missing sdk_release identity")
    expected_hash = str(release.get("wheel_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("Manifest sdk_release wheel_sha256 is invalid")
    spec = manifest.get("layers", {}).get("LlamaPReviewPipelineDependencies", {})
    if str(release.get("project") or "") != str(spec.get("wheel_project") or "") or str(
        release.get("version") or ""
    ) != str(spec.get("wheel_version") or ""):
        raise ValueError("SDK release and Pipeline Layer wheel identity diverge")
    if wheel.name != str(release.get("wheel_filename") or ""):
        raise ValueError("llama-github wheel filename does not match the release identity")
    actual_hash = _sha256(wheel)
    if actual_hash != expected_hash:
        raise ValueError("llama-github wheel hash does not match the public release identity")


def _clean_install_metadata(target: Path) -> None:
    # Lambda imports packages from the Layer and never consumes dependency
    # console scripts. Pip writes its invoking interpreter's absolute path into
    # those scripts, so retaining them would make the same locked inputs produce
    # host-specific artifacts.
    scripts = target / "bin"
    if scripts.is_dir():
        shutil.rmtree(scripts)
    for path in sorted(target.rglob("*"), reverse=True):
        if path.name == "__pycache__" and path.is_dir():
            shutil.rmtree(path)
        elif (
            path.name in {"direct_url.json", "INSTALLER", "RECORD", "REQUESTED"} and path.is_file()
        ):
            path.unlink()
        elif path.suffix in {".pyc", ".pyo"} and path.is_file():
            path.unlink()


def _installed_distributions(target: Path) -> dict[str, str]:
    return {
        _normalize_distribution(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions(path=[str(target)])
        if dist.metadata.get("Name")
    }


def _verify_layer(target: Path, spec: dict, wheel: Path) -> dict:
    installed = _installed_distributions(target)
    project = _normalize_distribution(str(spec["wheel_project"]))
    if installed.get(project) != str(spec["wheel_version"]):
        raise ValueError(
            f"Expected {project}=={spec['wheel_version']}, found {installed.get(project)!r}"
        )
    forbidden = {_normalize_distribution(name) for name in spec["forbidden_distributions"]}
    present_forbidden = sorted(forbidden & installed.keys())
    if present_forbidden:
        raise ValueError(f"Forbidden heavy distributions in layer: {', '.join(present_forbidden)}")
    missing_roots = sorted(
        root
        for root in REQUIRED_IMPORT_ROOTS
        if not (target / root).exists() and not (target / f"{root}.py").exists()
    )
    if missing_roots:
        raise ValueError(f"Layer is missing import roots: {', '.join(missing_roots)}")

    unzipped_bytes = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
    max_bytes = int(spec["max_unzipped_bytes"])
    if unzipped_bytes > max_bytes:
        raise ValueError(f"Layer is {unzipped_bytes} bytes; maximum is {max_bytes}")
    return {
        "schema_version": 1,
        "wheel": wheel.name,
        "wheel_sha256": _sha256(wheel),
        "requirements_lock_sha256": _sha256((ROOT_DIR / spec["requirements_lock"]).resolve()),
        "runtime": spec["runtime"],
        "architecture": spec["architecture"],
        "platform": spec["platform"],
        "abi": spec["abi"],
        "unzipped_bytes": unzipped_bytes,
        "max_unzipped_bytes": max_bytes,
        "installed_distributions": dict(sorted(installed.items())),
    }


def _write_deterministic_zip(source_root: Path, output_zip: Path) -> None:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_zip.with_suffix(output_zip.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for item in source_root.rglob("*"):
                if item.is_symlink():
                    raise ValueError(f"Refusing to package Layer symlink: {item}")
            for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
                info = zipfile.ZipInfo(
                    path.relative_to(source_root).as_posix(), date_time=ZIP_TIMESTAMP
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(
                    info,
                    path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        temporary.replace(output_zip)
    finally:
        temporary.unlink(missing_ok=True)


def build(layer_name: str, wheel: Path, output_zip: Path) -> dict:
    manifest = load_manifest()
    try:
        spec = manifest["layers"][layer_name]
    except KeyError as exc:
        raise ValueError(f"Unknown Lambda layer: {layer_name}") from exc
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError(f"llama-github wheel not found: {wheel}")
    verify_sdk_wheel_identity(manifest, wheel)

    with tempfile.TemporaryDirectory(prefix="llamapreview-layer-") as directory:
        root = Path(directory)
        target = root / "python"
        target.mkdir()
        lock_path = (ROOT_DIR / spec["requirements_lock"]).resolve()
        _run_pip(
            [
                "install",
                "--no-compile",
                "--require-hashes",
                "--only-binary=:all:",
                f"--platform={spec['platform']}",
                f"--python-version={str(spec['runtime']).removeprefix('python')}",
                f"--implementation={spec['implementation']}",
                f"--abi={spec['abi']}",
                f"--target={target}",
                "-r",
                str(lock_path),
            ]
        )
        _run_pip(
            [
                "install",
                "--no-compile",
                "--no-deps",
                f"--target={target}",
                str(wheel.resolve()),
            ]
        )
        _clean_install_metadata(target)
        build_manifest = _verify_layer(target, spec, wheel)
        manifest_path = target / ".llamapreview-layer-manifest.json"
        for _ in range(5):
            manifest_path.write_text(
                json.dumps(build_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            final_bytes = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
            if final_bytes == build_manifest["unzipped_bytes"]:
                break
            build_manifest["unzipped_bytes"] = final_bytes
        else:
            raise ValueError("Layer manifest size did not converge")
        if final_bytes > int(spec["max_unzipped_bytes"]):
            raise ValueError(
                f"Layer is {final_bytes} bytes; maximum is {spec['max_unzipped_bytes']}"
            )
        _write_deterministic_zip(root, output_zip)
    return build_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("layer_name")
    parser.add_argument("llama_github_wheel", type=Path)
    parser.add_argument("output_zip", type=Path)
    args = parser.parse_args()
    output_zip = args.output_zip if args.output_zip.is_absolute() else ROOT_DIR / args.output_zip
    result = build(args.layer_name, args.llama_github_wheel, output_zip)
    print(json.dumps({"output": str(output_zip), **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
