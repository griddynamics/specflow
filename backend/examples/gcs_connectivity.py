"""Check access to the GCS object storage bucket with Application Default Credentials.

Run from the repository root:

    uv --directory backend run python examples/gcs_connectivity.py
    uv --directory backend run python examples/gcs_connectivity.py --signed-url

Reads STORAGE_BUCKET and STORAGE_PREFIX from the
environment or the root .env.local. Credentials come from ADC: a personal
`gcloud auth application-default login`, or GOOGLE_APPLICATION_CREDENTIALS
pointing at a service-account key. Writes one small object under
{STORAGE_PREFIX}diagnostics/connectivity/ and never deletes anything.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, cast

import google.auth
import google.auth.credentials
from dotenv import load_dotenv
from google.api_core import exceptions
from google.auth.transport.requests import Request
from google.cloud import storage

PERMISSIONS = [
    "storage.objects.list",
    "storage.objects.get",
    "storage.objects.create",
    "storage.objects.delete",
]


def create_only_rewrite_refused(bucket: storage.Bucket, key: str) -> bool:
    """True when rewriting an existing object with if_generation_match=0 fails with 412."""

    try:
        bucket.blob(key).upload_from_string(b"overwrite", if_generation_match=0)
    except exceptions.PreconditionFailed:
        return True
    return False


def signed_url_check(blob: storage.Blob, credentials: google.auth.credentials.Credentials) -> int:
    # A key file signs locally. Workload Identity credentials hold no key, so
    # signing goes through IAM signBlob with the service account's email and token.
    sign_with: dict[str, Any] = {}
    if not isinstance(credentials, google.auth.credentials.Signing) and hasattr(credentials, "service_account_email"):
        cast(Any, credentials).refresh(Request())
        sign_with = {"service_account_email": credentials.service_account_email, "access_token": credentials.token}
    try:
        url = blob.generate_signed_url(
            version="v4", expiration=datetime.timedelta(minutes=5), method="GET", **sign_with
        )
        print(f"signed URL created (host {url.split('/')[2]})")
    except AttributeError as error:
        print(f"signed URL not possible with these credentials: {str(error).splitlines()[0][:120]}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--signed-url", action="store_true", help="also try a V4 signed GET URL")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env.local")
    missing = [v for v in ("STORAGE_BUCKET", "STORAGE_PREFIX") if not os.getenv(v)]
    if missing:
        print(f"missing variables: {', '.join(missing)}")
        return 2
    bucket_name, prefix = os.environ["STORAGE_BUCKET"], os.environ["STORAGE_PREFIX"]
    if not prefix.endswith("/"):
        print("STORAGE_PREFIX must end with '/', for example 'dev/'")
        return 2

    credentials, _ = google.auth.default()
    print(f"credentials: {type(credentials).__module__}.{type(credentials).__name__}")

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    granted = set(bucket.test_iam_permissions(PERMISSIONS))
    for permission in PERMISSIONS:
        print(f"  {permission:24} {'yes' if permission in granted else 'NO'}")
    if not {"storage.objects.create", "storage.objects.get"} <= granted:
        print("cannot write and read objects in this bucket")
        return 1

    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"{prefix}diagnostics/connectivity/{stamp}.md"
    data = f"# Connectivity check\n\nWritten at {stamp}.\n".encode()
    blob = bucket.blob(key)
    blob.upload_from_string(data, content_type="text/markdown; charset=utf-8", if_generation_match=0)
    blob.reload()
    print(f"wrote gs://{bucket_name}/{key} ({blob.size} bytes, {blob.storage_class})")

    if not create_only_rewrite_refused(bucket, key):
        print("create-only rewrite was NOT refused")
        return 1
    print("create-only rewrite refused with 412, as expected")

    read_back = bucket.blob(key).download_as_bytes()
    if hashlib.sha256(read_back).digest() != hashlib.sha256(data).digest():
        print("read-back SHA-256 does NOT match")
        return 1
    print("read-back SHA-256 matches")

    listed = [b.name for b in client.list_blobs(bucket_name, prefix=f"{prefix}diagnostics/connectivity/")]
    print(f"listed {len(listed)} object(s) under {prefix}diagnostics/connectivity/")

    return signed_url_check(blob, credentials) if args.signed_url else 0


if __name__ == "__main__":
    sys.exit(main())
