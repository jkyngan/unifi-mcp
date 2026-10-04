"""Run private-clip tests without installing dependencies or booting a server.

Only namespace-package initializers are bypassed. The artifact, manager, client,
and (when available) real MCP serialization implementations execute unchanged.
Controller calls are synthetic. This is not the repository's full pytest gate.
"""

import sys
import types
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for name, path in {
    "unifi_core": root / "packages/unifi-core/src/unifi_core",
    "unifi_mcp_shared": root / "packages/unifi-mcp-shared/src/unifi_mcp_shared",
    "unifi_protect_mcp": root / "apps/protect/src/unifi_protect_mcp",
}.items():
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
sys.path.insert(0, str(root / "examples/python"))
sys.path.insert(0, str(root / "tests"))
if __name__ == "__main__":
    pattern = sys.argv[1] if len(sys.argv) > 1 else "test_private_clips*.py"
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
