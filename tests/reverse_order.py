"""Run test modules with every class's methods in reverse order, to catch tests that depend
on what an earlier test left in a shared fixture. Used by the nightly CI job.

Usage (from tests/): python3 reverse_order.py test_module [test_module ...]
"""
import sys
import unittest


def main(names):
    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = lambda left, right: (left < right) - (left > right)
    suite = loader.loadTestsFromNames(names)
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.path.insert(0, ".")
    sys.exit(main(sys.argv[1:]))
