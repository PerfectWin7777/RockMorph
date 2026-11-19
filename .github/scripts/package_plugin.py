"""
Package RockMorph for the QGIS plugin repository, check its metadata, and
upload the zip to plugins.qgis.org.

    python .github/scripts/package_plugin.py --check
    python .github/scripts/package_plugin.py --output RockMorph.zip
    python .github/scripts/package_plugin.py --upload RockMorph.zip   # needs OSGEO_USERNAME / OSGEO_PASSWORD

The zip holds one folder, "RockMorph/", with the files tracked by git minus
what users do not need (tests, CI, journal, README images). Upload uses the
XML-RPC endpoint of plugins.qgis.org (same as QGIS's plugin_upload.py); the
credentials are read from the environment only, never from the command line.
"""

from __future__ import annotations

import argparse
import configparser
import os
import subprocess
import sys
import urllib.parse
import xmlrpc.client
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = "RockMorph"
EXCLUDED_PREFIXES = (".github/", "tests/", "doc/", "docs/images/", ".gitignore", ".gitattributes")
REQUIRED = ("name", "qgisMinimumVersion", "description", "about", "version", "author", "email", "repository")
ENDPOINT = "https://plugins.qgis.org/plugins/RPC2/"


def read_metadata() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with open(os.path.join(ROOT, "metadata.txt"), encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser["general"]


def check() -> list:
    """Problems that plugins.qgis.org would reject."""
    meta = read_metadata()
    problems = [f"metadata.txt: '{key}' is missing" for key in REQUIRED if not meta.get(key, "").strip()]
    if not os.path.exists(os.path.join(ROOT, "LICENSE")):
        problems.append("LICENSE file is missing")
    if not os.path.exists(os.path.join(ROOT, meta.get("icon", "icon.png"))):
        problems.append("icon file is missing")
    return problems


def tracked_files() -> list:
    files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    return [f for f in files if not f.startswith(EXCLUDED_PREFIXES) and "__pycache__" not in f]


def build(output: str) -> None:
    problems = check()
    if problems:
        sys.exit("\n".join(problems))
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in tracked_files():
            archive.write(os.path.join(ROOT, path), f"{PLUGIN}/{path}")
    print(f"{output}: {len(tracked_files())} files, {os.path.getsize(output) // 1024} KB")


def upload(path: str) -> None:
    user, password = os.environ.get("OSGEO_USERNAME"), os.environ.get("OSGEO_PASSWORD")
    if not user or not password:
        sys.exit("Set OSGEO_USERNAME and OSGEO_PASSWORD (repository secrets).")
    server = xmlrpc.client.ServerProxy(ENDPOINT.replace(
        "https://", f"https://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}@"))
    with open(path, "rb") as handle:
        plugin_id, version_id = server.plugin.upload(xmlrpc.client.Binary(handle.read()))
    print(f"Uploaded: plugin {plugin_id}, version {version_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--upload")
    args = parser.parse_args()
    if args.check:
        problems = check()
        print("\n".join(problems) or "metadata.txt OK")
        sys.exit(1 if problems else 0)
    if args.output:
        build(args.output)
    if args.upload:
        upload(args.upload)


if __name__ == "__main__":
    main()
