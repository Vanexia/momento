"""Run Mementos; the legacy ``python -m momento`` entry point also works."""

from momento.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
