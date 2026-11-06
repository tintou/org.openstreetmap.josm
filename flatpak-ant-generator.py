#!/usr/bin/env python3
"""Generate a flatpak-builder sources file for the Ant/Ivy dependencies of JOSM.

Like flatpak-gradle-generator, it lists every artifact needed by an offline
build as a pinned flatpak-builder `file` source, laid out as a Maven
repository under `m2/`. The Ant build then uses it with
`-Djosm.repo.root=file://$PWD/m2/` (see josm-offline-build.patch).

Usage:
    git clone --branch <tag> https://github.com/openstreetmap/josm.git
    git -C josm apply ../josm-offline-build.patch
    ./flatpak-ant-generator.py josm -o flatpak-sources.json

The checkout is copied to a temporary directory and built there with a fresh
Ivy cache, so everything Ivy resolves is recorded.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

# Preferred first; the JOSM repository is used when Central lacks the file
# or serves different content than what Ivy fetched.
MIRRORS = [
    "https://repo1.maven.org/maven2/",
    "https://josm.openstreetmap.de/repository/public/",
]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.read()
    except urllib.error.HTTPError:
        return None


def cached_artifacts(cache):
    """Yield (maven path, file content) for each jar/pom found in an Ivy cache."""
    for org in sorted(os.listdir(cache)):
        org_dir = os.path.join(cache, org)
        if not os.path.isdir(org_dir):
            continue
        for name in sorted(os.listdir(org_dir)):
            module_dir = os.path.join(org_dir, name)
            for root, _, files in os.walk(module_dir):
                for filename in sorted(files):
                    path = os.path.join(root, filename)
                    rel = os.path.relpath(path, module_dir).split(os.sep)
                    if (len(rel) == 1 and filename.startswith("ivy-")
                            and filename.endswith(".xml.original")):
                        version = filename[len("ivy-"):-len(".xml.original")]
                        ext = "pom"
                    elif (filename.startswith(name + "-") and filename.endswith(".jar")
                          and rel[0] in ("jars", "bundles")):
                        version, ext = filename[len(name) + 1:-4], "jar"
                    else:
                        continue
                    maven = "%s/%s/%s/%s-%s.%s" % (
                        org.replace(".", "/"), name, version, name, version, ext)
                    with open(path, "rb") as f:
                        yield maven, f.read()


def make_source(maven_path, content):
    digest = sha256(content)
    for mirror in MIRRORS:
        remote = fetch(mirror + maven_path)
        if remote is not None and sha256(remote) == digest:
            url = mirror + maven_path
            break
    else:
        sys.exit("No mirror serves %s with the expected content" % maven_path)
    return {
        "type": "file",
        "url": url,
        "sha256": digest,
        "dest": "m2/" + os.path.dirname(maven_path),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("josm_dir", help="JOSM checkout with josm-offline-build.patch applied")
    parser.add_argument("-o", "--output", default="flatpak-sources.json")
    parser.add_argument("--ant", default="ant", help="ant executable")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "josm")
        shutil.copytree(args.josm_dir, work)
        env = dict(os.environ, ANT_OPTS="-Duser.home=" + os.path.join(tmp, "home"))
        subprocess.run([args.ant, "-DflatpakBuild=true", "-DnoErrorProne=true", "dist"], cwd=work, env=env, check=True)

        sources = []
        cache = os.path.join(tmp, "home", ".ivy2", "cache")
        for maven_path, content in cached_artifacts(cache):
            sources.append(make_source(maven_path, content))

        # Ivy itself, which build.xml downloads on first run
        ivy_dir = os.path.join(work, "tools", "ivy")
        for filename in sorted(os.listdir(ivy_dir)):
            if filename.startswith("ivy-") and filename.endswith(".jar"):
                version = filename[len("ivy-"):-len(".jar")]
                with open(os.path.join(ivy_dir, filename), "rb") as f:
                    source = make_source(
                        "org/apache/ivy/ivy/%s/%s" % (version, filename), f.read())
                source["dest"] = "tools/ivy"
                sources.append(source)

    with open(args.output, "w") as f:
        json.dump(sources, f, indent=4)
        f.write("\n")
    print("Wrote %d sources to %s" % (len(sources), args.output))


if __name__ == "__main__":
    main()
