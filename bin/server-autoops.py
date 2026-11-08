"""Isolated entry point: imports server_autoops only from the install directory.

Started with ``python3 -I`` so neither the current directory, PYTHON* environment
variables nor user site-packages can inject modules into a root process.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
from server_autoops.cli import main  # noqa: E402

raise SystemExit(main())
