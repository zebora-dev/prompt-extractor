"""
Chrome profile snapshot manager — Supabase Storage backend.

Uploads and downloads tarred Chrome profile directories so clones can start
with a pre-logged-in ChatGPT session without running auto_login or hitting 2FA.

Storage layout (Supabase Storage bucket: chrome-profiles):
    profile_0.tar.gz   ← account index 0
    profile_1.tar.gz   ← account index 1
    ...

Environment variables (required):
    BRANDSIGHT_SUPABASE_URL             Supabase project URL
    BRANDSIGHT_SUPABASE_SERVICE_KEY     Service role key — bypasses RLS and the
                                        50 MB per-file limit on the free tier.
                                        Falls back to BRANDSIGHT_SUPABASE_ANON_KEY
                                        if not set (uploads will fail unless bucket
                                        RLS policies allow anon inserts).

Environment variables (optional):
    CHROME_PROFILE_BUCKET               Bucket name (default: chrome-profiles)
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

LOGGER = logging.getLogger(__name__)

BUCKET_NAME = os.getenv("CHROME_PROFILE_BUCKET", "chrome-profiles")
_OBJECT_PREFIX = "profile_"

# Chrome subdirectories that are pure cache / ephemeral state and add bulk
# without helping session persistence. Stripping them keeps archives small.
_CACHE_DIRS = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "ShaderCache",
    "GrShaderCache",
    "MediaCache",
    "Service Worker",  # large, regenerated on first visit
    "CacheStorage",
}

# Chrome process-specific files — meaningless in a snapshot
_SKIP_NAMES = {
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "lockfile",
    ".org.chromium.Chromium",
}


def _get_client(*, prefer_service_key: bool = True):
    """
    Return an initialised Supabase client.

    For uploads prefer the service role key so RLS doesn't block writes.
    Downloads work with the anon key if bucket policies permit.
    """
    from supabase import create_client

    url = os.getenv("BRANDSIGHT_SUPABASE_URL", "").strip()
    if not url:
        raise RuntimeError("BRANDSIGHT_SUPABASE_URL must be set to use the profile manager.")

    if prefer_service_key:
        key = os.getenv("BRANDSIGHT_SUPABASE_SERVICE_KEY", "").strip()
        if key:
            LOGGER.debug("Using service role key for Supabase Storage.")
        else:
            LOGGER.warning(
                "BRANDSIGHT_SUPABASE_SERVICE_KEY is not set — falling back to anon key. "
                "Uploads will fail if the bucket RLS policy blocks anon inserts. "
                "Set BRANDSIGHT_SUPABASE_SERVICE_KEY to fix this."
            )
            key = os.getenv("BRANDSIGHT_SUPABASE_ANON_KEY", "").strip()
    else:
        key = os.getenv("BRANDSIGHT_SUPABASE_ANON_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "No Supabase key found. Set BRANDSIGHT_SUPABASE_SERVICE_KEY "
            "(preferred) or BRANDSIGHT_SUPABASE_ANON_KEY."
        )

    return create_client(url, key)


def _object_name(index: int) -> str:
    return f"{_OBJECT_PREFIX}{index}.tar.gz"


# ── Archive helpers ───────────────────────────────────────────────────────────


def _add_dir(tar: tarfile.TarFile, src: Path, arc_base: Path) -> None:
    """
    Recursively add src into the tar archive under arc_base, skipping:
      - sockets, FIFOs, device files (can't be archived)
      - Chrome singleton/lock files (process-specific)
      - Cache directories (bulk with no session value)
    """

    def _onerror(err: OSError) -> None:
        LOGGER.debug("Skipping unreadable path during walk: %s", err)

    for root, dirs, files in os.walk(str(src), followlinks=False, onerror=_onerror):
        root_path = Path(root)
        rel = root_path.relative_to(src)
        arc_dir = arc_base / rel

        # Prune cache dirs in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if d not in _CACHE_DIRS]

        for filename in files:
            if filename in _SKIP_NAMES:
                LOGGER.debug("Skipping Chrome lock/singleton file: %s", filename)
                continue
            file_path = root_path / filename
            try:
                st = file_path.lstat()
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    LOGGER.debug("Skipping special file (%s): %s", stat.filemode(st.st_mode), file_path)
                    continue
                tar.add(str(file_path), arcname=str(arc_dir / filename), recursive=False)
            except OSError as exc:
                LOGGER.debug("Skipping %s: %s", file_path, exc)


# ── Upload ────────────────────────────────────────────────────────────────────


def upload_profile(index: int, profile_dir: str | Path) -> None:
    """
    Tar the given Chrome profile directory and upload it to Supabase Storage
    as profile_{index}.tar.gz.  Cache directories are excluded to keep the
    archive small.  Existing objects are overwritten.

    Requires BRANDSIGHT_SUPABASE_SERVICE_KEY to bypass RLS.
    """
    profile_dir = Path(profile_dir).expanduser()
    if not profile_dir.exists():
        raise FileNotFoundError(f"Chrome profile directory not found: {profile_dir}")

    object_name = _object_name(index)
    LOGGER.info("Archiving Chrome profile at %s → %s (excluding cache dirs) …", profile_dir, object_name)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            _add_dir(tar, profile_dir, Path("chrome-profile"))

        size_mb = tmp_path.stat().st_size / 1_048_576
        LOGGER.info("Archive size: %.1f MB — uploading to bucket '%s' …", size_mb, BUCKET_NAME)

        if size_mb > 45:
            LOGGER.warning(
                "Archive is %.1f MB. Supabase free tier allows 50 MB per file. "
                "Consider upgrading to Supabase Pro or using the service key which "
                "bypasses this limit.",
                size_mb,
            )

        client = _get_client(prefer_service_key=True)
        with open(tmp_path, "rb") as f:
            data = f.read()

        # Try update (overwrite) first; fall back to upload (create) if not found
        try:
            client.storage.from_(BUCKET_NAME).update(
                object_name,
                data,
                {"content-type": "application/gzip"},
            )
            LOGGER.info("Overwrote existing profile object '%s'.", object_name)
        except Exception:
            client.storage.from_(BUCKET_NAME).upload(
                object_name,
                data,
                {"content-type": "application/gzip"},
            )
            LOGGER.info("Uploaded new profile object '%s'.", object_name)

    finally:
        tmp_path.unlink(missing_ok=True)

    LOGGER.info("✅ Profile %d uploaded successfully.", index)


# ── Download ──────────────────────────────────────────────────────────────────


def download_profile(index: int, dest_dir: str | Path) -> bool:
    """
    Download profile_{index}.tar.gz from Supabase Storage and extract it to
    dest_dir.  Returns True on success, False if the object does not exist.

    dest_dir is created if it does not already exist.
    """
    dest_dir = Path(dest_dir).expanduser()
    object_name = _object_name(index)

    LOGGER.info("Downloading profile %d (%s) from bucket '%s' …", index, object_name, BUCKET_NAME)

    client = _get_client(prefer_service_key=True)

    try:
        data: bytes = client.storage.from_(BUCKET_NAME).download(object_name)
    except Exception as exc:
        LOGGER.warning("Profile %d not found in storage (will start with empty profile): %s", index, exc)
        return False

    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(data)

    try:
        LOGGER.info("Extracting %.1f MB to %s …", len(data) / 1_048_576, dest_dir)
        with tarfile.open(tmp_path, "r:gz") as tar:
            for member in tar.getmembers():
                # Strip the leading "chrome-profile/" path component
                parts = Path(member.name).parts
                if len(parts) > 1:
                    member.name = str(Path(*parts[1:]))
                elif member.name in {"chrome-profile", "."}:
                    continue
                tar.extract(member, dest_dir)
    finally:
        tmp_path.unlink(missing_ok=True)

    LOGGER.info("✅ Profile %d restored to %s.", index, dest_dir)
    return True


# ── Existence check ────────────────────────────────────────────────────────────


def profile_exists(index: int) -> bool:
    """Return True if profile_{index}.tar.gz exists in the bucket."""
    client = _get_client(prefer_service_key=True)
    try:
        files = client.storage.from_(BUCKET_NAME).list()
        names = {f["name"] for f in (files or [])}
        return _object_name(index) in names
    except Exception as exc:
        LOGGER.warning("Could not check profile existence: %s", exc)
        return False


# ── CLI entry point ───────────────────────────────────────────────────────────


def main() -> None:
    """
    Standalone CLI for profile capture/restore operations.

    Usage:
        python -m automated_extraction.profile_manager upload --index 0 --dir /path/to/profile
        python -m automated_extraction.profile_manager restore --index 0 --dest /tmp/chrome-profile
        python -m automated_extraction.profile_manager exists --index 0
    """
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Chrome profile snapshot manager (Supabase Storage).")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="Archive and upload a Chrome profile directory.")
    up.add_argument("--index", type=int, required=True, help="Profile slot index (0, 1, 2 …).")
    up.add_argument("--dir", required=True, help="Path to the Chrome user data directory.")

    dl = sub.add_parser("restore", help="Download and extract a Chrome profile.")
    dl.add_argument("--index", type=int, required=True, help="Profile slot index.")
    dl.add_argument("--dest", required=True, help="Destination directory for the extracted profile.")

    ex = sub.add_parser("exists", help="Check whether a profile snapshot exists in storage.")
    ex.add_argument("--index", type=int, required=True, help="Profile slot index.")

    args = parser.parse_args()

    from .config import load_dotenv_if_available
    load_dotenv_if_available()

    if args.command == "upload":
        upload_profile(args.index, args.dir)
    elif args.command == "restore":
        ok = download_profile(args.index, args.dest)
        if not ok:
            LOGGER.warning("No profile found for index %d — dest dir left empty.", args.index)
    elif args.command == "exists":
        found = profile_exists(args.index)
        print(f"Profile {args.index}: {'EXISTS' if found else 'NOT FOUND'}")


if __name__ == "__main__":
    main()
