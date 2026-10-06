"""The download-everything zip: one zip per target, inside one zip per run.

    <run>.zip
      <target>.zip              the archive, its .for-check.csv, and extras/<target>/...
      <other-target>.zip
      run-files.zip             manifest.json and anything else at the top of the run

Tarballs and zips are stored as they are (already compressed); extras, logs and
CSVs are deflated. The outer zip only stores, so nothing is compressed twice.

`plan()` decides what goes where and touches no file contents; `build()` writes
it to a temporary file, one inner zip at a time, so peak disk use is roughly
the final zip plus the largest single target.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

from errors import MigrationError
from migration_api import safe_name
from safety import check_disk

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip", ".gz", ".bz2", ".xz")
EXTRAS_DIR = "extras"

Entry = Tuple[Path, str]            # (file on disk, name inside the zip)


def _inside(root: Path, path: Path) -> bool:
    """True for a regular file that really lives under `root` (no symlink games)."""
    try:
        return not path.is_symlink() and path.is_file() and root in path.resolve().parents
    except OSError:
        return False


def _files_under(root: Path, directory: Path) -> List[Path]:
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if _inside(root, candidate) and not name.endswith(".part"):
                found.append(candidate)
    return found


def _target_dirs(label: str, org: str) -> List[str]:
    """Names an extras folder might have for an archive's target label."""
    names: List[str] = []
    for part in (p.strip() for p in label.split(",") if p.strip()):
        names += [safe_name(part), safe_name(part.split("/")[-1])]
        if org and "/" not in part:
            names.append(safe_name(f"{org}/{part}"))
    return list(dict.fromkeys(names))


def plan(run_dir: Path) -> Dict[str, List[Entry]]:
    """{inner zip name: [(file, arcname), ...]} for a run folder."""
    root = run_dir.resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    org = manifest.get("org", "")

    extras_root = root / EXTRAS_DIR
    extras_dirs = ({d.name: d for d in sorted(extras_root.iterdir()) if d.is_dir() and not d.is_symlink()}
                   if extras_root.is_dir() else {})
    used_dirs: set = set()
    claimed: set = set()
    bundles: Dict[str, List[Entry]] = {}

    def add(zip_name: str, path: Path, arcname: str) -> None:
        entries = bundles.setdefault(zip_name, [])
        if all(arc != arcname for _p, arc in entries):
            entries.append((path, arcname))

    for record in manifest.get("archives", []):
        name = Path(record.get("path") or "").name
        archive = root / name if name else None
        if not archive or not _inside(root, archive):
            continue
        zip_name = f"{safe_name(record.get('target') or archive.name)}.zip"
        add(zip_name, archive, archive.name)
        claimed.add(archive)
        stem = archive.name
        for suffix in ARCHIVE_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        csv_file = root / f"{stem}.for-check.csv"
        if _inside(root, csv_file):
            add(zip_name, csv_file, csv_file.name)
            claimed.add(csv_file)
        for dirname in _target_dirs(record.get("target", ""), org):
            if dirname in extras_dirs and dirname not in used_dirs:
                used_dirs.add(dirname)
                for f in _files_under(root, extras_dirs[dirname]):
                    add(zip_name, f, f"{EXTRAS_DIR}/{dirname}/{f.relative_to(extras_dirs[dirname]).as_posix()}")

    for dirname, directory in extras_dirs.items():          # targets that had no archive
        if dirname in used_dirs:
            continue
        for f in _files_under(root, directory):
            add(f"{dirname}.zip", f, f"{EXTRAS_DIR}/{dirname}/{f.relative_to(directory).as_posix()}")

    for f in sorted(root.iterdir()):                         # everything else at the top
        if f not in claimed and _inside(root, f) and not f.name.endswith(".part"):
            add("run-files.zip", f, f.name)

    return {name: entries for name, entries in bundles.items() if entries}


def _method(arcname: str) -> int:
    return zipfile.ZIP_STORED if arcname.endswith(ARCHIVE_SUFFIXES) else zipfile.ZIP_DEFLATED


def total_bytes(bundles: Dict[str, List[Entry]]) -> int:
    return sum(p.stat().st_size for entries in bundles.values() for p, _a in entries)


def build(run_dir: Path, tmp_dir: Path | None = None) -> Path:
    """Write the nested zip to a temporary file and return its path (caller deletes it)."""
    bundles = plan(run_dir)
    if not bundles:
        raise MigrationError("nothing to download in this run")
    tmp_dir = tmp_dir or Path(tempfile.gettempdir())
    size_mb = int(total_bytes(bundles) * 1.1 / (1024 * 1024)) + 64
    check_disk(tmp_dir, need_mb=size_mb, what="the download zip")

    fd, final_name = tempfile.mkstemp(suffix=".zip", prefix="md-run-", dir=tmp_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(final_name, "w", zipfile.ZIP_STORED, allowZip64=True) as outer:
            for zip_name, entries in bundles.items():
                ifd, inner_name = tempfile.mkstemp(suffix=".zip", prefix="md-part-", dir=tmp_dir)
                os.close(ifd)
                try:
                    with zipfile.ZipFile(inner_name, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as inner:
                        for path, arcname in entries:
                            inner.write(path, arcname, compress_type=_method(arcname))
                    outer.write(inner_name, zip_name)
                finally:
                    os.unlink(inner_name)
    except BaseException:
        os.unlink(final_name)
        raise
    return Path(final_name)
