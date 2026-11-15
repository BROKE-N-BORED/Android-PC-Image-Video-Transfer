#!/usr/bin/env python3
"""Test runner for the Android Photo Transfer Linux port.

Usage (from the port/ directory):
    python3 tests/run_tests.py              # engine tests (no Qt needed)
    python3 tests/run_tests.py --gui        # engine + Qt GUI smoke tests
    python3 tests/run_tests.py -k transfer  # only modules matching "transfer"
    python3 tests/run_tests.py -v           # verbose

The GUI tests need Xvfb + the Qt test python (system python3 has no
PySide6) and must run with the DEFAULT HOME (Qt/xcb breaks under a fake
HOME) and TMPDIR=/tmp:

    LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu \\
    TMPDIR=/tmp xvfb-run -a \\
    ~/workspace/forge-v2-testenv/bin/python tests/run_tests.py --gui

Plain unittest discovery also works:
    python3 -m unittest discover -s tests -v
    (add test_gui_smoke explicitly under xvfb for the GUI tests)

Tests skip cleanly -- with a note saying what is missing -- when the
module under test has not been built yet, so the suite is safe to run
while the parallel builders are still working.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest

PORT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORT_DIR)
sys.path.insert(0, os.path.join(PORT_DIR, "tests"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the android_photo_transfer tests.")
    parser.add_argument("--gui", action="store_true",
                        help="also run the Qt GUI smoke tests (needs xvfb)")
    parser.add_argument("-k", "--match", default=None,
                        help="only run test modules whose name contains this text")
    parser.add_argument("-v", "--verbose", action="count", default=1,
                        help="verbosity (repeat for more)")
    args = parser.parse_args(argv)

    loader = unittest.TestLoader()
    start_dir = os.path.join(PORT_DIR, "tests")
    suite = unittest.TestSuite()
    for name in sorted(os.listdir(start_dir)):
        if not name.startswith("test_") or not name.endswith(".py"):
            continue
        module = name[:-3]
        if module == "test_gui_smoke" and not args.gui:
            continue
        if args.match and args.match not in module:
            continue
        suite.addTests(loader.loadTestsFromName(module))

    runner = unittest.TextTestRunner(verbosity=args.verbose)
    result = runner.run(suite)
    print()
    print(f"Ran {result.testsRun} test(s): "
          f"{len(result.failures)} failed, {len(result.errors)} errors, "
          f"{len(result.skipped)} skipped")
    if result.skipped:
        print("Skipped (module not built yet, or needs a real phone / Qt):")
        for test, reason in result.skipped:
            print(f"  - {test.id()}: {reason}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
