"""Count + dedupe-check the DEFAULT_KNOWN_GAMES list."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import DEFAULT_KNOWN_GAMES  # noqa: E402


def main() -> int:
    total = len(DEFAULT_KNOWN_GAMES)
    unique = len({g.lower() for g in DEFAULT_KNOWN_GAMES})
    print(f"Total entries     : {total}")
    print(f"Unique (case-ins) : {unique}")
    if unique == total:
        print("PASS")
        return 0

    seen: set[str] = set()
    dupes: list[str] = []
    for game in DEFAULT_KNOWN_GAMES:
        folded = game.lower()
        if folded in seen:
            dupes.append(game)
        seen.add(folded)
    print(f"Duplicates ({len(dupes)}):")
    for duplicate in dupes:
        print(f"  - {duplicate}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
