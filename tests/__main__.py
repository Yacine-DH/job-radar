"""Run the whole suite:  python -m tests

Plain `python -W error::ResourceWarning -m unittest ...` does NOT fail on a leaked resource: CPython reports a
ResourceWarning raised inside a destructor through sys.unraisablehook ("Exception ignored in ...") and carries on,
so the exit code stays 0. This runner records those unraisable exceptions and fails the run if there is one.
CI runs exactly this.
"""
import gc
import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    unraisable: list = []
    original = sys.unraisablehook

    def hook(info):
        unraisable.append(info)
        original(info)  # keep Python's normal report visible

    sys.unraisablehook = hook
    warnings.simplefilter("error", ResourceWarning)
    try:
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        ok = result.wasSuccessful()
        del suite, result  # drop the test instances so whatever they still hold is finalized (and reported) now
        gc.collect()
    finally:
        sys.unraisablehook = original
    if unraisable:
        print(f"\nFAILED: {len(unraisable)} unraisable exception(s): usually a leaked connection or file "
              "(see 'Exception ignored' above)", file=sys.stderr)
    return 0 if ok and not unraisable else 1


if __name__ == "__main__":
    sys.exit(main())
