"""CLI entrypoint for references package."""

import sys
from .crawler import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
