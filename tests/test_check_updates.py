import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import urllib.error


SCRIPT = (Path(__file__).resolve().parents[1] / "github-project-health-inspector"
          / "scripts" / "check-updates.py")
SPEC = importlib.util.spec_from_file_location("check_updates", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def release(tag="v1.0.2", **changes):
    payload = {"tag_name": tag, "draft": False, "prerelease": False,
               "html_url": "https://github.com/%s/releases/tag/%s"
                           % (checker.REPOSITORY, tag), "assets": []}
    payload.update(changes)
    return payload


def response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class CheckUpdatesTests(unittest.TestCase):
    def test_installed_version(self):
        expected = (SCRIPT.parent.parent / "VERSION").read_text(encoding="ascii").strip()
        self.assertEqual(checker.read_installed_version(), expected)

    def test_numeric_version_comparison(self):
        result = checker.check_updates("1.0.9", opener=Mock(return_value=response(release("v1.0.10"))))
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(result["latest_version"], "1.0.10")

    def test_current_and_ahead_versions(self):
        for installed, expected in [("1.0.2", "up_to_date"), ("1.0.3", "ahead")]:
            with self.subTest(installed=installed):
                result = checker.check_updates(installed, opener=Mock(return_value=response(release())))
                self.assertEqual(result["status"], expected)

    def test_network_failure_is_unverified(self):
        for error in (urllib.error.URLError("offline"), TimeoutError(),
                      http.client.IncompleteRead(b"partial")):
            with self.subTest(error=error):
                opener = Mock(side_effect=error)
                result = checker.check_updates("1.0.1", opener=opener)
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(result["error"], "network_error")
                self.assertIsNone(result["latest_version"])

    def test_invalid_releases_are_rejected(self):
        invalid = [[], {}, release("v1.0.2-beta.1"), release(draft=True),
                   release(prerelease=True), release(html_url="https://example.com/update"),
                   release(assets=[{"browser_download_url": "https://example.com/update.zip"}]),
                   release(assets="bad"), release(assets=[None])]
        for payload in invalid:
            with self.subTest(payload=payload):
                result = checker.check_updates("1.0.1", opener=Mock(return_value=response(payload)))
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(result["error"], "invalid_release_response")

    def test_official_asset_is_accepted(self):
        url = "https://github.com/%s/releases/download/v1.0.2/skill.zip" % checker.REPOSITORY
        result = checker.check_updates("1.0.1", opener=Mock(return_value=response(
            release(assets=[{"browser_download_url": url}]))))
        self.assertEqual(result["status"], "update_available")

    def test_bad_json_and_large_responses_are_unverified(self):
        for body in (b"not json", b"\xff", b"[" * 2000 + b"]" * 2000,
                     b"x" * (checker.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(length=len(body)):
                result = checker.check_updates("1.0.1", opener=Mock(return_value=io.BytesIO(body)))
                self.assertEqual(result["error"], "invalid_release_response")

    def test_invalid_local_version_does_not_request_network(self):
        opener = Mock()
        result = checker.check_updates("1.0.1-dev", opener=opener)
        self.assertEqual(result["error"], "invalid_installed_version")
        opener.assert_not_called()

    def test_official_endpoint_and_timeout(self):
        opener = Mock(return_value=response(release()))
        checker.check_updates("1.0.1", timeout=3, opener=opener)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, checker.API_URL)
        self.assertEqual(opener.call_args.kwargs["timeout"], 3)
        self.assertNotIn("Authorization", request.headers)

    def test_redirect_to_external_host_is_rejected(self):
        handler = checker.OfficialRedirectHandler()
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://example.com")

    def test_json_cli_and_failure_exit_status(self):
        result = {"status": "unverified", "current_version": "1.0.1",
                  "latest_version": None, "release_url": checker.RELEASE_URL,
                  "error": "network_error"}
        stream = io.StringIO()
        with patch.object(checker, "check_updates", return_value=result), contextlib.redirect_stdout(stream):
            self.assertEqual(checker.main(["--json"]), 0)
        self.assertEqual(json.loads(stream.getvalue()), result)

    def test_version_cli_does_not_request_network(self):
        stream = io.StringIO()
        with patch.object(checker, "check_updates") as check, contextlib.redirect_stdout(stream):
            self.assertEqual(checker.main(["--version"]), 0)
        check.assert_not_called()
        self.assertEqual(stream.getvalue().strip(), checker.read_installed_version())

    def test_missing_local_version_is_unverified(self):
        stream = io.StringIO()
        with patch.object(checker, "read_installed_version", side_effect=OSError), contextlib.redirect_stdout(stream):
            self.assertEqual(checker.main(["--json"]), 0)
        self.assertEqual(json.loads(stream.getvalue())["error"], "invalid_installed_version")


if __name__ == "__main__":
    unittest.main()
