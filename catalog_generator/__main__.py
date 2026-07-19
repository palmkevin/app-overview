"""Entry point for ``python -m catalog_generator``."""

import sys

from catalog_generator.cli import main

if __name__ == "__main__":
    sys.exit(main())
