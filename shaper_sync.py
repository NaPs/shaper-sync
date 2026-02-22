#!/usr/bin/env python3
"""
Synchronize a local directory to Shaper Hub (hub.shapertools.com).

Usage:
    python shaper_sync.py <directory> [--email EMAIL] [--password PASSWORD]
                                      [--remote-path /remote/path]
                                      [--dry-run] [--watch] [--verbose]

Credentials can also be provided via the environment variables
SHAPER_EMAIL and SHAPER_PASSWORD.
"""

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from os import environ
from pathlib import Path

import inotify.adapters
import requests

logger = logging.getLogger("shaper_sync")

AUTH_URL = "https://auth.shapertools.com"
API_URL = "https://api.shapertools.com"
HUB_ORIGIN = "https://hub.shapertools.com"

# Headers required by the Shaper API.
# - Origin is checked server-side for CORS validation.
# - X-ApiVersion matches the version used by the official Shaper Studio app.
COMMON_HEADERS = {
    "Origin": HUB_ORIGIN,
    "Referer": f"{HUB_ORIGIN}/",
    "X-ApiVersion": "3.0.0",
}


class ShaperHubClient:
    """Client for the undocumented Shaper Hub API, reverse-engineered from
    the Shaper Studio web app (studio.shapertools.com)."""

    def __init__(self, email: str, password: str):
        self.session = requests.Session()
        self.session.headers.update(COMMON_HEADERS)
        self._authenticate(email, password)

    def _tree_url(self, path: str, name: str) -> str:
        """Build the URL for a file/folder entry in the userspace tree."""
        parent = path if path.endswith("/") else path + "/"
        return f"{API_URL}/files/userspace/tree/{parent}{name}"

    def _authenticate(self, email: str, password: str) -> None:
        """Obtain a JWT via POST /token and set it as Bearer token."""
        logger.info("Authenticating...")
        resp = self.session.post(
            f"{AUTH_URL}/token",
            json={
                "client_id": "000000000000000000000000",
                "grant_type": "password",
                "username": email,
                "password": password,
                "scope": "*",
                "acceptTC": False,
            },
            headers={"X-ApiVersion": "2.0.0"},
        )
        if resp.status_code != 200:
            body = resp.text
            try:
                body = resp.json().get("message", body)
            except Exception:
                pass
            logger.error("Authentication error (%d): %s", resp.status_code, body)
            sys.exit(1)

        data = resp.json()
        # Response format: {"access_token": {"token": "jwt"}, "expires": "...", ...}
        try:
            token = data["access_token"]["token"]
        except (KeyError, TypeError):
            logger.error("Unable to extract token. Response: %s", data)
            sys.exit(1)

        self.session.headers["Authorization"] = f"Bearer {token}"
        logger.info("Authentication successful.")

    def list_files(
        self, path: str = "/", file_type: str | None = None, limit: int = 200
    ) -> list[dict]:
        """List files in the user's personal space at the given path."""
        params = {
            "spaceType": "userspace",
            "limit": str(limit),
            "path": path if path.endswith("/") else path + "/",
            "sort": "modified:-1",
        }
        if file_type:
            params["type"] = file_type
        resp = self.session.get(f"{API_URL}/files/userspace/search", params=params)
        resp.raise_for_status()
        return resp.json().get("results", [])

    def create_folder(self, path: str, name: str) -> dict:
        """Create a folder in the user's personal space."""
        resp = self.session.post(self._tree_url(path, name), json={"type": "folder"})
        resp.raise_for_status()
        return resp.json()

    def create_file_entry(self, path: str, name: str, blob_id: str) -> dict:
        """Create a file entry linked to a blob in the user's personal space."""
        resp = self.session.post(
            self._tree_url(path, name),
            json={"type": "file", "blobs": [blob_id]},
        )
        resp.raise_for_status()
        return resp.json()

    def delete_file(self, path: str, name: str) -> None:
        """Delete a file entry from the user's personal space."""
        resp = self.session.delete(self._tree_url(path, name))
        resp.raise_for_status()

    def upload_blob(self, file_path: Path) -> str:
        """Upload raw file bytes to blob storage and return the blob ID."""
        with open(file_path, "rb") as f:
            resp = self.session.post(
                f"{API_URL}/blobs/",
                data=f,
                headers={"Content-Type": "application/octet-stream"},
            )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("Blob upload response: %s", data)
        return data["blobs"][0]

    def _upload_file(self, remote_path: str, entry: Path) -> str:
        """Upload blob + create file entry. Returns the blob ID."""
        blob_id = self.upload_blob(entry)
        self.create_file_entry(remote_path, entry.name, blob_id)
        return blob_id

    def sync_file(self, local_file: Path, remote_path: str) -> None:
        """Upload or update a single file on Shaper Hub.

        Checks whether the file already exists remotely and either uploads
        it as new or deletes + re-uploads if the local version is newer.
        """
        remote_path = remote_path.rstrip("/") + "/" if remote_path != "/" else "/"
        self.ensure_remote_path(remote_path)
        remote_files = self.get_remote_files(remote_path)

        if local_file.name in remote_files:
            logger.info("Updating: %s...", local_file.name)
            self.delete_file(remote_path, local_file.name)
        else:
            logger.info("Uploading: %s...", local_file.name)

        blob_id = self._upload_file(remote_path, local_file)
        logger.info("OK: %s (blob: %s)", local_file.name, blob_id)

    def ensure_remote_path(self, remote_path: str) -> None:
        """Recursively create remote folders if they don't exist yet."""
        if remote_path == "/":
            return
        parts = [p for p in remote_path.strip("/").split("/") if p]
        current = "/"
        for part in parts:
            existing = {
                item["name"] for item in self.list_files(current, file_type="folder")
            }
            if part not in existing:
                logger.debug("Creating folder: %s%s/", current, part)
                self.create_folder(current, part)
            current = f"{current}{part}/"

    def get_remote_files(self, remote_path: str) -> dict[str, datetime]:
        """Return a mapping of filename -> modified datetime for a remote folder."""
        return {
            f["name"]: datetime.fromisoformat(f["modified"].replace("Z", "+00:00"))
            for f in self.list_files(remote_path, file_type="file")
        }

    def sync_directory(
        self,
        local_dir: Path,
        remote_path: str = "/",
        *,
        dry_run: bool = False,
        recursive: bool = True,
    ) -> Counter:
        """Synchronize a local directory to Shaper Hub.

        Returns a Counter with keys: uploaded, updated, skipped, errors.
        """
        stats: Counter = Counter()
        remote_path = remote_path.rstrip("/") + "/" if remote_path != "/" else "/"

        if not dry_run:
            self.ensure_remote_path(remote_path)
            remote_files = self.get_remote_files(remote_path)
        else:
            remote_files = {}

        for entry in sorted(local_dir.iterdir()):
            # Skip hidden files/directories
            if entry.name.startswith("."):
                continue

            if entry.is_dir() and recursive:
                logger.info("Directory: %s/", entry.name)
                stats += self.sync_directory(
                    entry,
                    f"{remote_path}{entry.name}/",
                    dry_run=dry_run,
                    recursive=True,
                )
                continue

            if not entry.is_file():
                continue

            local_mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)

            if entry.name in remote_files:
                if local_mtime <= remote_files[entry.name]:
                    logger.info("Skipped (up to date): %s", entry.name)
                    stats["skipped"] += 1
                    continue
                action = "updated"
            else:
                action = "uploaded"

            if dry_run:
                logger.info(
                    "[dry-run] Would %s: %s",
                    "update" if action == "updated" else "upload",
                    entry.name,
                )
                stats[action] += 1
                continue

            try:
                logger.info(
                    "%s: %s...",
                    "Updating" if action == "updated" else "Uploading",
                    entry.name,
                )
                if action == "updated":
                    self.delete_file(remote_path, entry.name)
                blob_id = self._upload_file(remote_path, entry)
                logger.info("OK: %s (blob: %s)", entry.name, blob_id)
                stats[action] += 1
            except Exception as e:
                logger.error("ERROR for %s: %s", entry.name, e)
                stats["errors"] += 1

        return stats


    def watch_directory(
        self,
        local_dir: Path,
        remote_path: str = "/",
        *,
        recursive: bool = True,
    ) -> None:
        """Watch a local directory for changes and sync them to Shaper Hub.

        Performs an initial full sync, then uses inotify to watch for file
        creation and modification events. Runs until interrupted with Ctrl+C.
        """
        # Initial sync
        logger.info("Initial sync...")
        stats = self.sync_directory(
            local_dir, remote_path, recursive=recursive,
        )
        logger.info(
            "Initial sync done: %d uploaded, %d updated, %d skipped, %d error(s).",
            stats["uploaded"], stats["updated"], stats["skipped"], stats["errors"],
        )

        if recursive:
            ino = inotify.adapters.InotifyTree(str(local_dir))
        else:
            ino = inotify.adapters.Inotify()
            ino.add_watch(str(local_dir))

        logger.info("Watching for changes... (Ctrl+C to stop)")

        try:
            for event in ino.event_gen(yield_nones=False):
                _, type_names, watch_path, filename = event

                if not filename or filename.startswith("."):
                    continue

                # Only react to file writes and moves
                if not ({"IN_CLOSE_WRITE", "IN_MOVED_TO"} & set(type_names)):
                    continue

                full_path = Path(watch_path) / filename
                if not full_path.is_file():
                    continue

                # Compute remote path from watched root
                rel = full_path.parent.relative_to(local_dir)
                rpath = remote_path.rstrip("/") + "/" if remote_path != "/" else "/"
                if str(rel) != ".":
                    rpath += str(rel) + "/"

                try:
                    self.sync_file(full_path, rpath)
                except Exception as e:
                    logger.error("ERROR for %s: %s", filename, e)
        except KeyboardInterrupt:
            logger.info("\nStopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize a local directory to Shaper Hub.",
    )
    parser.add_argument("directory", type=Path, help="Local directory to synchronize.")
    parser.add_argument(
        "--email",
        default=environ.get("SHAPER_EMAIL"),
        required="SHAPER_EMAIL" not in environ,
        help="Shaper account email (default: $SHAPER_EMAIL).",
    )
    parser.add_argument(
        "--password",
        default=environ.get("SHAPER_PASSWORD"),
        required="SHAPER_PASSWORD" not in environ,
        help="Shaper account password (default: $SHAPER_PASSWORD).",
    )
    parser.add_argument(
        "--remote-path", default="/", help="Remote destination path (default: /)."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate without uploading anything."
    )
    parser.add_argument(
        "--no-recursive", action="store_true", help="Do not synchronize subdirectories."
    )
    parser.add_argument(
        "--watch", "-w", action="store_true",
        help="Watch directory for changes and sync continuously.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show more details."
    )

    args = parser.parse_args()
    logging.basicConfig(
        format="%(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    if not args.directory.is_dir():
        logger.error("%s is not a directory.", args.directory)
        sys.exit(1)

    if args.dry_run:
        logger.info("[Dry-run mode enabled -- no changes will be made]")

    client = ShaperHubClient(args.email, args.password)

    if args.watch:
        client.watch_directory(
            args.directory,
            args.remote_path,
            recursive=not args.no_recursive,
        )
    else:
        stats = client.sync_directory(
            args.directory,
            args.remote_path,
            dry_run=args.dry_run,
            recursive=not args.no_recursive,
        )
        logger.info("")
        logger.info(
            "Done: %d uploaded, %d updated, %d skipped, %d error(s).",
            stats["uploaded"],
            stats["updated"],
            stats["skipped"],
            stats["errors"],
        )


if __name__ == "__main__":
    main()
