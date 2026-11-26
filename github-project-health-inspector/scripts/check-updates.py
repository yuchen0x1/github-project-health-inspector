#!/usr/bin/env python3
"""Check the official release without downloading or installing updates."""

import argparse
import http.client
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPOSITORY = "yuchen0x1/github-project-health-inspector"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPOSITORY
RELEASE_URL = "https://github.com/%s/releases/latest" % REPOSITORY
MAX_RESPONSE_BYTES = 256 * 1024
VERSION_RE = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def parse_version(value):
    if not isinstance(value, str):
        raise ValueError("version must be a stable semantic version")
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError("version must be a stable semantic version")
    return tuple(int(part) for part in match.groups())


def read_installed_version():
    version = (Path(__file__).resolve().parent.parent / "VERSION").read_text(
        encoding="utf-8").strip()
    return ".".join(str(part) for part in parse_version(version))


class OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl != API_URL:
            raise urllib.error.URLError("non-official release redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _valid_release(data):
    if not isinstance(data, dict):
        return False
    if data.get("draft") is not False or data.get("prerelease") is not False:
        return False
    tag = data.get("tag_name")
    parse_version(tag)
    official_root = "https://github.com/%s/releases/" % REPOSITORY
    if data.get("html_url") != official_root + "tag/" + tag:
        return False
    assets = data.get("assets", [])
    if not isinstance(assets, list):
        return False
    for asset in assets:
        if not isinstance(asset, dict):
            return False
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            return False
        prefix = official_root + "download/" + tag + "/"
        parsed = urllib.parse.urlsplit(url)
        if (not url.startswith(prefix) or not url[len(prefix):]
                or parsed.query or parsed.fragment):
            return False
    return True


def check_updates(current_version, timeout=5, opener=None):
    result = {"status": "unverified", "current_version": current_version,
              "latest_version": None, "release_url": RELEASE_URL}
    try:
        current = parse_version(current_version)
    except (ValueError, TypeError):
        result.update(current_version=None, error="invalid_installed_version")
        return result
    if opener is None:
        opener = urllib.request.build_opener(OfficialRedirectHandler()).open
    request = urllib.request.Request(API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-project-health-inspector/" + current_version,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("release response too large")
        data = json.loads(body)
        if not _valid_release(data):
            raise ValueError("invalid release response")
        latest = parse_version(data["tag_name"])
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        result["error"] = "invalid_release_response"
        return result
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        result["error"] = "network_error"
        return result
    result["latest_version"] = ".".join(str(part) for part in latest)
    result["status"] = ("update_available" if latest > current else
                        "up_to_date" if latest == current else "ahead")
    return result


def _timeout(value):
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number")
    if not 0 < timeout <= 30:
        raise argparse.ArgumentTypeError("timeout must be between 0 and 30 seconds")
    return timeout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--version", action="store_true", help="print installed version")
    parser.add_argument("--timeout", type=_timeout, default=5,
                        help="request timeout in seconds (default: 5, maximum: 30)")
    args = parser.parse_args(argv)
    try:
        current = read_installed_version()
    except (OSError, ValueError, UnicodeError):
        current = None
    if args.version:
        print(current or "unverified: missing or invalid VERSION file")
        return 0 if current else 2
    result = check_updates(current, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        status = result["status"]
        if status == "update_available":
            print("Update available: %s -> %s" % (current, result["latest_version"]))
        elif status == "up_to_date":
            print("Installed version %s is the latest stable release." % current)
        elif status == "ahead":
            print("Installed version %s is newer than the latest stable release %s."
                  % (current, result["latest_version"]))
        else:
            print("Update check unverified: %s." % result.get("error", "unknown_error"))
        print("Official releases and upgrade instructions: " + RELEASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
