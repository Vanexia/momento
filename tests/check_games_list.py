"""Count + dedupe-check the DEFAULT_KNOWN_GAMES list."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import DEFAULT_KNOWN_GAMES  # noqa: E402

total = len(DEFAULT_KNOWN_GAMES)
unique = len({g.lower() for g in DEFAULT_KNOWN_GAMES})
print(f"Total entries     : {total}")
print(f"Unique (case-ins) : {unique}")
if unique < total:
    seen: set[str] = set()
    dupes: list[str] = []
    for g in DEFAULT_KNOWN_GAMES:
        lo = g.lower()
        if lo in seen:
            dupes.append(g)
        seen.add(lo)
    print(f"Duplicates ({len(dupes)}):")
    for d in dupes:
        print(f"  - {d}")
