"""Fetch fresh pinned wheels on the runner; Docker build itself stays offline."""

import concurrent.futures
import hashlib
import itertools
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import tomllib

BASE = "python@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f"
context = Path(sys.argv[1]).resolve()
recipe = context / "recipe"
request = {
    kind: (recipe / f"{kind}-requirements.txt").read_text()
    for kind in ("build", "runtime")
}
# Resolve markers and supported wheel tags using the actual target runtime,
# without installing tools in the host or making network calls in containers.
code = """
import json,re,sys
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.tags import sys_tags
request=json.load(sys.stdin)
result={'tags':[str(t) for t in sys_tags()], 'packages':[]}
for kind,text in request.items():
 for line in text.splitlines():
  if not line.strip() or line.startswith('#'): continue
  req=Requirement(line.split('--hash=',1)[0].strip())
  if req.marker and not req.marker.evaluate(): continue
  spec=list(req.specifier)
  assert len(spec)==1 and spec[0].operator=='=='
  result['packages'].append({'kind':kind,'name':req.name,'version':spec[0].version,'hashes':re.findall(r'--hash=sha256:([0-9a-f]{64})',line)})
json.dump(result,sys.stdout)
"""
resolved = json.loads(
    subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            BASE,
            "python",
            "-c",
            code,
        ],
        input=json.dumps(request).encode(),
    )
)
tags = {tag: index for index, tag in enumerate(resolved["tags"])}
inputs = context / "inputs"
inputs.mkdir(exist_ok=False)
for kind in ("build", "runtime"):
    (inputs / kind).mkdir()


def fetch(url):
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.hostname in {
        "pypi.org",
        "files.pythonhosted.org",
    }
    with urlopen(url, timeout=120) as response:
        return response.read()


def download(package):
    name, version = package["name"], package["version"]
    metadata = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
    candidates = []
    for item in metadata["urls"]:
        filename = item["filename"]
        if (
            not filename.endswith(".whl")
            or item["digests"]["sha256"] not in package["hashes"]
        ):
            continue
        py, abi, platform = filename[:-4].rsplit("-", 3)[-3:]
        ranks = [
            tags[t]
            for parts in itertools.product(
                py.split("."), abi.split("."), platform.split(".")
            )
            if (t := "-".join(parts)) in tags
        ]
        if ranks:
            candidates.append((min(ranks), filename, item))
    assert candidates, (
        f"No hash-locked target-compatible binary wheel for {name}=={version}; stop, no source fallback"
    )
    _, filename, item = min(candidates, key=lambda x: (x[0], x[1]))
    data = fetch(item["url"])
    digest = hashlib.sha256(data).hexdigest()
    assert digest == item["digests"]["sha256"] and digest in package["hashes"]
    path = inputs / package["kind"] / filename
    path.write_bytes(data)
    return {
        "file": str(path.relative_to(inputs)),
        "name": name,
        "version": version,
        "sha256": digest,
        "url": item["url"],
    }


with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    records = list(pool.map(download, resolved["packages"]))
lock = tomllib.loads((context / "source/uv.lock").read_text())
antlr = next(p for p in lock["package"] if p["name"] == "antlr4-python3-runtime")
assert antlr["version"] == "4.9.3"
sdist = antlr["sdist"]
data = fetch(sdist["url"])
assert hashlib.sha256(data).hexdigest() == sdist["hash"].removeprefix("sha256:")
(inputs / "antlr.tar.gz").write_bytes(data)
records.append(
    {
        "file": "antlr.tar.gz",
        "name": antlr["name"],
        "version": antlr["version"],
        "sha256": sdist["hash"].removeprefix("sha256:"),
        "url": sdist["url"],
    }
)
(inputs / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")
print(
    f"Fresh downloads verified: {len(records) - 1} binary wheels and approved ANTLR 4.9.3 source; no cache used"
)
