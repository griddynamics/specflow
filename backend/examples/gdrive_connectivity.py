"""Check read access to a Google Drive folder with Application Default Credentials.

Run from backend/ (locally; --env-file only matters if .env.local sets
GOOGLE_APPLICATION_CREDENTIALS):

    uv run --env-file ../.env.local python examples/gdrive_connectivity.py "<folder link>"
    uv run --env-file ../.env.local python examples/gdrive_connectivity.py "<folder link>" --refresh

Run inside an api or worker pod (the image has the libraries but not examples/,
so the script comes in on stdin):

    kubectl exec -i <pod> -- python - "<folder link>" < examples/gdrive_connectivity.py

Credentials come from ADC with the read-only Drive scope: locally a personal
`gcloud auth application-default login` that includes the Drive scope, in GKE
the pod's service account through Workload Identity. Drive answers only for
items shared with that identity. Prints who Drive sees, the folder, and its
direct children; reads metadata only and never changes anything.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from http import HTTPStatus
from typing import Any, cast

import google.auth
import google.auth.credentials
import google.auth.exceptions
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,}")
LOGIN_HINT = (
    "gcloud auth application-default login --scopes="
    "https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive.readonly"
)
CREDENTIAL_KINDS = {
    "google.oauth2.credentials.Credentials": "a personal account (gcloud login)",
    "google.auth.compute_engine.credentials.Credentials": "the GKE metadata server (Workload Identity)",
    "google.auth.impersonated_credentials.Credentials": "an impersonated service account",
    "google.oauth2.service_account.Credentials": "a service-account key file",
}


def folder_id_from_link(link: str) -> str | None:
    """Return the Drive id in a folder or file link, or the input itself when it is a bare id."""
    link = link.strip()
    if DRIVE_ID.fullmatch(link):
        return link
    parsed = urllib.parse.urlparse(link)
    if parsed.hostname not in {"drive.google.com", "docs.google.com"}:
        return None
    match = re.search(r"/(?:folders|d)/([A-Za-z0-9_-]{10,})", parsed.path)
    if match:
        return match.group(1)
    ids = urllib.parse.parse_qs(parsed.query).get("id")
    return ids[0] if ids and DRIVE_ID.fullmatch(ids[0]) else None


def explain(error: HttpError, identity: str) -> str:
    text = error.content.decode("utf-8", "replace")
    if error.status_code == HTTPStatus.NOT_FOUND:
        return f"not found: the item does not exist, or it is not shared with {identity}"
    if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text or "insufficient authentication scopes" in text:
        return f"the login has no Drive scope; run:\n  {LOGIN_HINT}"
    if "SERVICE_DISABLED" in text or "accessNotConfigured" in text:
        return (
            f"Drive API is not enabled for the quota project ({error.reason}); run:\n"
            "  gcloud auth application-default set-quota-project <your-quota-project>"
        )
    return f"Drive returned {error.status_code}: {error.reason}"


def kind(mime_type: str) -> str:
    return mime_type.removeprefix("application/vnd.google-apps.")


def expiry(credentials: google.auth.credentials.Credentials) -> str:
    return f"{credentials.expiry:%Y-%m-%d %H:%M:%S} UTC" if credentials.expiry else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("link", help="Drive folder link, for example https://drive.google.com/drive/folders/<id>")
    parser.add_argument("--max", type=int, default=100, help="list at most this many items (default 100)")
    parser.add_argument("--refresh", action="store_true", help="also force a new access token and call Drive again")
    args = parser.parse_args()

    folder_id = folder_id_from_link(args.link)
    if not folder_id:
        print("not a Drive folder link or id")
        return 2

    try:
        credentials, _ = google.auth.default(scopes=DRIVE_SCOPES)
    except google.auth.exceptions.DefaultCredentialsError as error:
        print(f"no Google credentials found ({str(error).splitlines()[0]}); run:\n  {LOGIN_HINT}")
        return 2
    class_name = f"{type(credentials).__module__}.{type(credentials).__name__}"
    print(f"credentials: {class_name}, from {CREDENTIAL_KINDS.get(class_name, 'an unknown source')}")

    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    identity = "this identity"
    try:
        user = drive.about().get(fields="user(emailAddress, displayName)").execute()["user"]
        identity = user.get("emailAddress", identity)
        print(f"Drive sees: {identity} ({user.get('displayName', '')})")
        print(f"access token valid until {expiry(credentials)}; the library renews it by itself")

        folder = (
            drive.files().get(fileId=folder_id, fields="id, name, mimeType, driveId", supportsAllDrives=True).execute()
        )
        where = f"shared drive {folder['driveId']}" if folder.get("driveId") else "My Drive"
        print(f"item: {folder['name']} ({kind(folder['mimeType'])}, in {where})")
        if folder["mimeType"] != FOLDER_MIME:
            print("this link is not a folder")
            return 1

        listed, page_token = 0, None
        while listed < args.max:
            page = (
                drive.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(name, mimeType, modifiedTime)",
                    orderBy="folder,name",
                    pageSize=min(100, args.max - listed),
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in page.get("files", []):
                print(f"  {kind(item['mimeType']):12} {item['modifiedTime'][:10]}  {item['name']}")
                listed += 1
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        print(f"listed {listed} item(s){' (stopped at --max; more exist)' if page_token else ''}")

        if args.refresh:
            before = expiry(credentials)
            cast(Any, credentials).refresh(Request())
            drive.about().get(fields="user(emailAddress)").execute()
            print(f"forced a new token (valid until {before} -> {expiry(credentials)}); Drive still answers")
    except HttpError as error:
        print(explain(error, identity))
        return 1
    except google.auth.exceptions.RefreshError as error:
        print(f"cannot get an access token ({str(error).splitlines()[0][:160]}); run:\n  {LOGIN_HINT}")
        return 1
    print(f"to give this identity a folder, share it with {identity} as Viewer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
