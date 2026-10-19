"""Guard test for the supply-chain attack fixed in ``scripts/check_assets.py``.

The repository previously shipped an obfuscated payload disguised as
``public/fonts/fa-solid-500.woff2`` plus a ``.vscode/tasks.json`` that ran it on
folder open. This keeps that from ever landing again.
"""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_assets.py"
_spec = importlib.util.spec_from_file_location("check_assets", _SCRIPT)
check_assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_assets)


def test_repository_has_no_malicious_assets_or_autorun_tasks():
    assert check_assets.find_violations() == []
