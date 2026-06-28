"""
One-time script: SSH into each prompt-extractor-uk machine, tar the Chrome
profile, and upload it to the Tigris bucket as chatgpt/profile_<index>.tar.gz.

Run AFTER setting AWS_* secrets on gpt-extractor-uk and adding account rows
to chatgpt_profiles.  Safe to re-run (overwrites existing archives).

Usage:
    python scripts/upload_profiles_to_tigris.py

Prerequisites:
    pip install boto3
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_ENDPOINT_URL_S3 / BUCKET_NAME
    must be set in the environment (copy from `flyctl secrets list -a gpt-extractor-uk`
    or run from inside a gpt-extractor-uk machine where they are injected by Fly).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Machine → account index mapping ──────────────────────────────────────────
# Adjust to match the actual machines and the chatgpt_profiles indices you want.
MACHINES: list[dict] = [
    {"machine_id": "0805614bd911d8", "profile_index": 0, "email": "dev@zebora.io"},
    {"machine_id": "891244c62419e8", "profile_index": 1, "email": "cleo@zebora.io"},
    # Add more machines here as needed
]

FLY_APP = "prompt-extractor-uk"
PROFILE_PATH_ON_MACHINE = "/data/chrome-profile"


def _upload(local_tar: Path, index: int) -> None:
    import boto3

    key = f"chatgpt/profile_{index}.tar.gz"
    bucket = os.environ.get("BUCKET_NAME", "gpt-extractor-profiles")
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3", "https://fly.storage.tigris.dev"),
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "auto"),
    )
    size_mb = local_tar.stat().st_size / 1_048_576
    print(f"  Uploading {size_mb:.1f} MB → {bucket}/{key} …")
    s3.upload_file(str(local_tar), bucket, key)
    print(f"  ✅ Uploaded profile_{index}")


def _tar_on_machine(machine_id: str, dest: Path) -> None:
    """SSH into the machine, tar the profile, stream it back locally."""
    print(f"  SSH → {machine_id}: taring {PROFILE_PATH_ON_MACHINE} …")
    cmd = [
        "flyctl", "ssh", "console",
        "-a", FLY_APP,
        "-m", machine_id,
        "-C",
        f"tar -czf - --exclude='Cache' --exclude='Code Cache' --exclude='GPUCache' "
        f"--exclude='ShaderCache' --exclude='blob_storage' --exclude='SingletonLock' "
        f"-C {PROFILE_PATH_ON_MACHINE} .",
    ]
    with open(dest, "wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"SSH tar failed for {machine_id}: {result.stderr.decode()}")
    size_mb = dest.stat().st_size / 1_048_576
    print(f"  Captured {size_mb:.1f} MB from {machine_id}")


def main() -> None:
    missing = [k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if not os.environ.get(k)]
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}", file=sys.stderr)
        print("Set them from Fly secrets or run inside a gpt-extractor-uk machine.", file=sys.stderr)
        sys.exit(1)

    for m in MACHINES:
        machine_id = m["machine_id"]
        index = m["profile_index"]
        email = m["email"]
        print(f"\n[profile_{index}] {email} — machine {machine_id}")

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            _tar_on_machine(machine_id, tmp_path)
            _upload(tmp_path, index)
        except Exception as exc:
            print(f"  ❌ Failed: {exc}")
        finally:
            tmp_path.unlink(missing_ok=True)

    print("\nDone. Profiles are now in Tigris and ready for gpt-extractor-uk workers.")


if __name__ == "__main__":
    main()
