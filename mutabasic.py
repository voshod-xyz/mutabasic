#!/usr/bin/env python3
"""Compatibility CLI facade; implementation lives in :mod:`mutabasic_pkg`."""
from mutabasic_pkg.core import *  # noqa: F401,F403
from mutabasic_pkg.core import main


if __name__ == "__main__":
    raise SystemExit(main())
