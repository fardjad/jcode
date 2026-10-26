"""Isolation shared with the standalone repository bootstrap wrapper."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_spec = spec_from_file_location(
    "_jcode_isolation", Path(__file__).resolve().parents[4] / "tools/scripts/isolate_config.py"
)
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
isolated_environment = _module.isolated_environment
