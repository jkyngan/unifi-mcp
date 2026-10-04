"""Verify public pinned source and exported requirements before a clean build."""

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import tomllib

SHA = "7fa82e05040ddf1c8a6d1916ce4f399417e8e25d"
LOCK = "cda0a3755393415db46df1545f2b7e34221b7a9d98c2e3e46808d17fa0979674"
source, destination = map(Path, sys.argv[1:])
recipe = Path(__file__).resolve().parent
assert (
    subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    == SHA
)
raw = (source / "uv.lock").read_bytes()
assert hashlib.sha256(raw).hexdigest() == LOCK
packages = {
    p["name"]: p
    for p in tomllib.loads(raw.decode())["package"]
    if "registry" in p.get("source", {})
}
for line in (recipe / "runtime-requirements.txt").read_text().splitlines():
    if not line or line.startswith("#"):
        continue
    match = re.match(r"([\w-]+)==([^ ;]+)", line)
    assert match, line
    name, version = match.groups()
    locked = packages[name]
    assert locked["version"] == version
    hashes = set(re.findall(r"--hash=(sha256:[0-9a-f]{64})", line))
    allowed = {w["hash"] for w in locked.get("wheels", [])}
    if "sdist" in locked:
        allowed.add(locked["sdist"]["hash"])
    assert hashes and hashes <= allowed
assert not destination.exists(), "Use a new context directory"
destination.mkdir(parents=True)
archive = subprocess.check_output(
    ["git", "-C", str(source), "archive", "--format=tar", SHA]
)
(destination / "source").mkdir()
# git archive contains only the authorized public repository tree.
subprocess.run(
    ["tar", "-xf", "-", "-C", str(destination / "source")], input=archive, check=True
)
shutil.copytree(recipe, destination / "recipe")
# The context contains only public source/recipe files; remapped Docker users
# need read/traverse access to the source bind mount used by verification.
for path in (destination / "source", *(destination / "source").rglob("*")):
    path.chmod(path.stat().st_mode | (0o555 if path.is_dir() else 0o444))
print(
    "Source and runtime requirement versions/hashes match the pinned lock; context prepared."
)
