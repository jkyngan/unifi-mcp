"""Synthetic-only candidate gate; no credentials and no network in containers."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

image, source_arg, output_arg = sys.argv[1:]
source, output = Path(source_arg).resolve(), Path(output_arg).resolve()
recipe = Path(__file__).resolve().parent
for tool in ("docker",):
    assert shutil.which(tool), (
        f"Preinstalled {tool} required; do not install implicitly"
    )
output.mkdir(parents=True, exist_ok=False)
# Synthetic outputs only; allow remapped container root to write and host to read.
output.chmod(0o777)
shutil.copyfile(recipe / "container-roundtrip.py", output / "container-roundtrip.py")
(output / "container-roundtrip.py").chmod(0o644)
shutil.copyfile(recipe / "decode-outputs.py", output / "decode-outputs.py")
(output / "decode-outputs.py").chmod(0o644)
base = [
    "docker",
    "run",
    "--rm",
    "--network",
    "none",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,size=128m",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "-e",
    "PYTHONDONTWRITEBYTECODE=1",
]


def run(args):
    subprocess.run(args, check=True, timeout=180)


run(base + [image, "python", "-m", "pip", "check"])
run(
    base
    + [
        image,
        "python",
        "-c",
        "import unifi_protect_mcp.main; from unifi_protect_mcp.runtime import clip_artifact_store; assert clip_artifact_store is None",
    ]
)
mounts = [
    "-e",
    "PYTHONPATH=/verification/examples/python:/verification/tests",
    "--mount",
    f"type=bind,src={source},dst=/verification,readonly",
]
tests = "import unittest; suite=unittest.defaultTestLoader.discover('/verification/tests',pattern='test_private_clips*.py'); result=unittest.TextTestRunner(verbosity=2).run(suite); assert result.wasSuccessful() and result.testsRun == 38 and len(result.skipped) == 2"
run(base + mounts + [image, "python", "-c", tests])
run(
    base
    + mounts
    + [
        "--mount",
        f"type=bind,src={output},dst=/out",
        image,
        "python",
        "/out/container-roundtrip.py",
    ]
)
run(
    base
    + mounts
    + [
        "--mount",
        f"type=bind,src={output},dst=/out",
        image,
        "python",
        "/out/decode-outputs.py",
    ]
)
run(
    base
    + [
        "--mount",
        f"type=bind,src={output},dst=/out",
        image,
        "python",
        "-c",
        'from pathlib import Path; [p.chmod(0o644) for p in Path("/out").iterdir() if p.suffix != ".py"]',
    ]
)


results = []
decoded_report = json.loads((output / "decoded.json").read_text())
clips = sorted(output.glob("*.mp4"))
assert len(clips) == len(decoded_report["results"]) == 6
for path in clips:
    original = source / "tests/fixtures/clips" / path.name.split("-", 1)[1]
    assert path.read_bytes() == original.read_bytes()
    decoded = next(r for r in decoded_report["results"] if r["file"] == path.name)
    assert decoded["decoded_equal"]
    results.append({**decoded, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
image_id = subprocess.check_output(
    ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
).strip()
report = {
    "image_config_id": image_id,
    "tests_run": 38,
    "passed": 36,
    "skipped": 2,
    "roundtrips": results,
    "live_nvr_tested": False,
    "hiis_wrapper_in_this_build": "not run: no private HIIS checkout in public CI",
}
(output / "verified.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
