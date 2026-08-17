"""Scan every reachable Git object and ref identity before public release."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from privacy_scan import (  # noqa: E402
    binary_contains_blocked_identity,
    contains_blocked_identity,
    contains_private_windows_user_path,
)

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_EMAIL = re.compile(
    r"^(?:214237761\+vanexia@users\.noreply\.github\.com|noreply@github\.com)$",
    re.IGNORECASE,
)


def _git(*args: str, input_data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        input=input_data,
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _reachable_objects() -> tuple[list[str], bytes]:
    listing = _git("rev-list", "--objects", "--all")
    object_ids = []
    for line in listing.splitlines():
        object_id = line.split(b" ", 1)[0].decode("ascii")
        if object_id:
            object_ids.append(object_id)
    return list(dict.fromkeys(object_ids)), listing


def _object_payloads(object_ids: list[str]):
    request = ("\n".join(object_ids) + "\n").encode("ascii")
    response = _git("cat-file", "--batch", input_data=request)
    offset = 0
    for expected in object_ids:
        header_end = response.find(b"\n", offset)
        if header_end < 0:
            raise RuntimeError("git cat-file returned a truncated header")
        header = response[offset:header_end].decode("ascii", errors="replace")
        offset = header_end + 1
        parts = header.split()
        if len(parts) != 3 or parts[0] != expected:
            raise RuntimeError(f"unexpected git cat-file header: {header}")
        size = int(parts[2])
        payload = response[offset : offset + size]
        if len(payload) != size:
            raise RuntimeError(f"git cat-file truncated object {expected}")
        offset += size
        if response[offset : offset + 1] != b"\n":
            raise RuntimeError(f"git cat-file framing failed for {expected}")
        offset += 1
        yield expected, parts[1], payload


def _payload_is_private(payload: bytes) -> bool:
    if binary_contains_blocked_identity(payload):
        return True
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        text = payload.decode(encoding, errors="ignore")
        if contains_blocked_identity(text) or contains_private_windows_user_path(text):
            return True
    return False


def main() -> int:
    failures: list[str] = []
    try:
        object_ids, listing = _reachable_objects()
        if _payload_is_private(listing):
            failures.append("reachable object paths contain private identity data")
        for object_id, object_type, payload in _object_payloads(object_ids):
            if _payload_is_private(payload):
                failures.append(
                    f"reachable {object_type} {object_id[:12]} contains private identity data"
                )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        failures.append(f"history scan could not complete: {exc}")
        object_ids = []

    try:
        metadata = _git(
            "log",
            "--all",
            "--format=%H%x09%ae%x09%ce",
        ).decode("utf-8", errors="replace")
        for line in metadata.splitlines():
            commit, author_email, committer_email = line.split("\t", 2)
            for role, email in (("author", author_email), ("committer", committer_email)):
                if not ALLOWED_EMAIL.fullmatch(email.strip()):
                    failures.append(
                        f"commit {commit[:12]} has non-public {role} email metadata"
                    )
        taggers = _git(
            "for-each-ref",
            "refs/tags",
            "--format=%(refname)%09%(taggeremail:trim)",
        ).decode("utf-8", errors="replace")
        for line in taggers.splitlines():
            ref, _, email = line.partition("\t")
            if email and not ALLOWED_EMAIL.fullmatch(email.strip("<> ")):
                failures.append(f"tag {ref} has non-public tagger email metadata")
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        failures.append(f"Git metadata scan could not complete: {exc}")

    if failures:
        for failure in failures[:20]:
            print(f"FAIL - {failure}")
        if len(failures) > 20:
            print(f"FAIL - {len(failures) - 20} additional history findings omitted")
        return 1
    print(f"PASS - {len(object_ids)} reachable Git objects contain no private identity data")
    print("PASS - every commit and tag uses an approved public no-reply identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
