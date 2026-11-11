"""Load the plugin as the package ``session_cassettes`` for tests.

The repo root is the plugin package, and its folder name (``hermes-cassettes``)
is not a valid import name. This loads the root ``__init__.py`` the way Hermes
and ``hermes plugins validate`` do: from its file location, with the root as
the package's search path, so the relative imports resolve.

Run the tests with either ``python -m pytest tests`` or
``python -m unittest discover -s tests``.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "session_cassettes"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _pkg
    _spec.loader.exec_module(_pkg)

plugin = sys.modules[PKG]
store = importlib.import_module(f"{PKG}.store")
snapshot = importlib.import_module(f"{PKG}.snapshot")
verdict = importlib.import_module(f"{PKG}.verdict")
arms = importlib.import_module(f"{PKG}.arms")
