"""Load custom_components/layers/logic as the standalone package ``layers_logic``.

The logic package has no Home Assistant imports and uses only relative imports,
so it can be loaded on its own. That keeps these tests runnable on any Python
3.12+ without Home Assistant installed, and lets an offline backtest load the
very same code. Run these separately from the Home Assistant tests:

    pytest tests/logic
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

LOGIC = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "layers" / "logic"


def _load() -> None:
    if "layers_logic" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "layers_logic", LOGIC / "__init__.py", submodule_search_locations=[str(LOGIC)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["layers_logic"] = module
    spec.loader.exec_module(module)


_load()
