"""Privacy checks shared by source-tree, bundle, and archive smoke tests."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable


# Digests keep private identifiers out of the public scanner itself. Candidates
# are derived from ordinary tokens, adjacent token pairs, and complete emails.
BLOCKED_DIGESTS = frozenset(
    {
        "e154ca594d58ae02ca5703283182eafe02e19af6240f83de10f50c8055d67443",
        "db5e274ae58cbad96d5cdaa23d3ff723b1827a01229811f8dcef816b912cf775",
        "a95394daff8d9ebd10520a1f088274600a737d0015c851461daeccc2cd737da9",
        "b3ea0d242ca359574af24bf8115728544c9a232264474482be52aac46a62ccfe",
        "af5d1eeb1561b953df6d35f3fb608e37da9b71a2753cceff8a3013d866ce19b3",
        "785a7c068b3e00f76b4b3425faae7a7c8ca9bd69b9fd133ca534c37d6b793310",
        "14beb3a3365599f58a7e73ba88acaa7d1d1a1b371fe884b265ddd8577b0c6344",
        "b90544c596a251c379af9dd4171c9553b444a9f60a72cb7ca7ec2dd9bf3b9621",
        "e07f2055d5eef189e9d51a9d5ed9a6f8109e469d142519c5f4236f05101bf0db",
    }
)
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}")
WORD = re.compile(r"[a-z0-9]+")
WINDOWS_USER_PATH = re.compile(r"[a-z]:[\\/]users[\\/][^\\/\s\"']+", re.IGNORECASE)
ASCII_RUN = re.compile(rb"[\x20-\x7e]{4,}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()


def _candidates(text: str) -> Iterable[str]:
    lowered = text.casefold()
    yield from EMAIL.findall(lowered)
    for line in lowered.splitlines():
        words = WORD.findall(line)
        yield from words
        for index in range(len(words) - 1):
            yield words[index] + words[index + 1]


def contains_blocked_identity(text: str) -> bool:
    return any(_digest(candidate) in BLOCKED_DIGESTS for candidate in _candidates(text))


def contains_private_windows_user_path(text: str) -> bool:
    return bool(WINDOWS_USER_PATH.search(text))


def binary_contains_blocked_identity(data: bytes) -> bool:
    for run in ASCII_RUN.findall(data):
        if contains_blocked_identity(run.decode("ascii", errors="ignore")):
            return True
    return False
