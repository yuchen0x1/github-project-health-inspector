import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "github-project-health-inspector" / "scripts" / "github-inspector.py"
SPEC = importlib.util.spec_from_file_location("github_inspector", SCRIPT)
inspector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspector)


def response(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


def dep(name="package", version="1.0", scope="project"):
    return {"name": name, "version": version, "pinned": bool(version), "ecosystem": "PyPI",
            "scope": scope, "source_file": "requirements.txt"}


class RepositoryURLTests(unittest.TestCase):
    def test_repo_suffix_is_not_stripped_character_by_character(self):
        for name in ("latest", "testing", "digit", "repo.git"):
            with self.subTest(name=name):
                expected = name[:-4] if name.endswith(".git") else name
                self.assertEqual(inspector.parse_repo_url("owner/" + name)["repo"], expected)

    def test_rejects_non_github_hosts_and_unsafe_repository_segments(self):
        for url in ("https://gitlab.com/owner/repo", "https://github.com/a/%2e%2e",
                    "https://github.com/a/x%2Fy", "owner/.git", "../repo"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                inspector.parse_repo_url(url)

    def test_resolves_slash_branch_before_source_subpath(self):
        parsed = inspector.parse_repo_url("https://github.com/owner/repo/tree/feature/fix/src")
        api = mock.Mock()
        api.get.side_effect = [{"status": 404}, {"status": 200}]
        self.assertEqual(inspector.resolve_repo_ref(parsed, api), "feature/fix")
        self.assertTrue(api.get.call_args_list[1].args[0].endswith("feature%2Ffix"))

    def test_api_never_sends_token_to_another_origin(self):
        with mock.patch.object(inspector.urllib.request, "urlopen") as network:
            result = inspector.GitHubAPI(token="synthetic-token").get("https://elsewhere.invalid/data")
        network.assert_not_called()
        self.assertIsNone(result["data"])


class SourceAcquisitionTests(unittest.TestCase):
    def zip_blob(self, names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name, content in names:
                archive.writestr(name, content)
        return buf.getvalue()

    def test_existing_workdir_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "source"
            dest.mkdir()
            marker = dest / "my-file.txt"
            marker.write_text("keep", encoding="utf-8")
            with mock.patch.object(inspector.urllib.request, "urlopen") as network:
                result = inspector.fetch_source({"owner": "owner", "repo": "repo"}, dest)
            self.assertFalse(result["ok"])
            self.assertEqual(marker.read_text(), "keep")
            network.assert_not_called()

    def test_zip_fallback_supports_tag_and_preserves_old_unzip_directory(self):
        blob = self.zip_blob([("repo-v1/src.py", "print('ok')")])
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "source"
            old = Path(root) / "source-unzip"
            old.mkdir()
            with mock.patch.object(inspector, "have_cmd", return_value=False), \
                    mock.patch.object(inspector.urllib.request, "urlopen", return_value=io.BytesIO(blob)) as network:
                result = inspector.fetch_source({"owner": "owner", "repo": "repo"}, dest, ref="v1.0.0")
            self.assertTrue(result["ok"], result)
            self.assertTrue(old.is_dir())
            self.assertTrue((dest / "src.py").is_file())
            self.assertTrue(network.call_args.args[0].full_url.endswith("/zip/v1.0.0"))

    def test_zip_traversal_is_rejected_without_writing_source(self):
        for name in ("../outside", "repo/../../outside", "C:/outside", "repo\\..\\outside"):
            blob = self.zip_blob([(name, "bad")])
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                dest = Path(root) / "source"
                with mock.patch.object(inspector, "have_cmd", return_value=False), \
                        mock.patch.object(inspector.urllib.request, "urlopen", return_value=io.BytesIO(blob)):
                    result = inspector.fetch_source({"owner": "owner", "repo": "repo"}, dest)
                self.assertFalse(result["ok"])
                self.assertFalse(dest.exists())
                self.assertEqual(list(Path(root).iterdir()), [])


class FileTraversalTests(unittest.TestCase):
    def test_binary_files_obey_file_count_budget(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for name in ("a.png", "b.jpg", "c.gif"):
                (root / name).write_bytes(b"data")
            files, stats = inspector.walk_files(root, max_files=2)
            self.assertEqual(len(files), 2)
            self.assertTrue(stats["truncated"])

    def test_nested_output_directory_is_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            output = root / "results"
            output.mkdir()
            (output / "old.json").write_text("{}")
            (root / "source.py").write_text("pass")
            files, _ = inspector.walk_files(root, exclude_paths=(output,))
            self.assertEqual([file["path"] for file in files], ["source.py"])

    def test_external_file_symlink_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root = Path(root)
            target = Path(outside) / "private.py"
            target.write_text("secret = 'do-not-read'")
            try:
                (root / "link.py").symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic-link permission unavailable")
            files, _ = inspector.walk_files(root)
            self.assertEqual(files, [])

    def test_read_text_handles_bom_and_reads_only_limit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "text"
            path.write_bytes(b"\xef\xbb\xbfhello world")
            self.assertEqual(inspector.read_text(path, limit=8), "hello")


class DependencyParsingTests(unittest.TestCase):
    def test_http_packages_and_bare_dependencies_are_kept(self):
        for name in ("httpx", "httpcore", "requests"):
            self.assertEqual(inspector._pep508(name)[0], name)

    def test_wildcard_versions_are_never_pinned(self):
        parsed = inspector._pep508("requests==2.32.*")
        self.assertFalse(parsed[2])
        self.assertEqual(parsed[1], "")
        self.assertEqual(parsed[3], "==2.32.*")

    def test_python_markers_extras_and_epoch_versions(self):
        parsed = inspector._pep508('package[extra]==1!2.0; python_version >= "3.8"')
        self.assertEqual(parsed[:3], ("package", "1!2.0", True))

    def test_pyproject_metadata_is_not_dependencies(self):
        text = '[project]\nname="demo"\nclassifiers=["Fake :: Classifier"]\ndependencies=["httpx>=0.27", "requests==2.32.0"]\n'
        result = inspector.parse_pyproject_deps(text)
        self.assertEqual([entry["name"] for entry in result], ["httpx", "requests"])

    def test_pyproject_fallback_keeps_multiline_arrays_with_extras(self):
        text = '[project]\ndependencies = [\n"requests[socks]==2.32.0",\n"httpx>=0.27",\n]\n'
        result = inspector._pyproject_deps_fallback(text)
        self.assertEqual([entry["name"] for entry in result], ["requests", "httpx"])

    def test_pyproject_fallback_does_not_count_include_group_as_dependency(self):
        text = '[dependency-groups]\ntest = [{include-group="lint"}, "pytest==8.0.0"]\n'
        result = inspector._pyproject_deps_fallback(text)
        self.assertEqual([entry["name"] for entry in result], ["pytest"])

    def test_poetry_constraints_are_not_concatenated_to_package_name(self):
        text = '[tool.poetry.dependencies]\npython="^3.8"\nhttpx="^0.27"\nrequests="2.32.0"\n'
        result = inspector.parse_pyproject_deps(text)
        self.assertEqual([entry["name"] for entry in result], ["httpx", "requests"])
        self.assertTrue(result[1]["pinned"])
        self.assertEqual(result[1]["version"], "2.32.0")

    def test_example_lock_does_not_become_project_version(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "requirements.txt").write_text("requests>=2.0")
            (root / "examples").mkdir()
            (root / "examples" / "requirements.txt").write_text("requests==1.0")
            files, _ = inspector.walk_files(root)
            result = inspector.collect_manifest_deps(root, files)
            self.assertEqual(len(result), 2)
            project = next(item for item in result if item["scope"] == "project")
            self.assertFalse(project["pinned"])
            self.assertEqual(project["version"], "")

    def test_multiple_installed_versions_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            content = {"packages": {"node_modules/pkg": {"version": "1.0.0"},
                                    "node_modules/other/node_modules/pkg": {"version": "2.0.0"}}}
            (root / "package-lock.json").write_text(json.dumps(content))
            files, _ = inspector.walk_files(root)
            result = inspector.collect_manifest_deps(root, files)
            self.assertEqual({entry["version"] for entry in result}, {"1.0.0", "2.0.0"})


class OSVAuditTests(unittest.TestCase):
    def test_network_failure_does_not_claim_clean(self):
        with mock.patch.object(inspector.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            result = inspector.osv_query([dep()])
        self.assertEqual(result["status"], "unverified_incomplete")
        self.assertEqual(result["queried"], 0)
        self.assertFalse(result["complete"])

    def test_incomplete_response_does_not_claim_clean(self):
        with mock.patch.object(inspector.urllib.request, "urlopen", return_value=response({"results": []})):
            result = inspector.osv_query([dep()])
        self.assertEqual(result["status"], "unverified_incomplete")

    def test_query_cap_is_declared(self):
        with mock.patch.object(inspector.urllib.request, "urlopen", return_value=response({"results": [{}]})):
            result = inspector.osv_query([dep("first"), dep("second")], max_pkgs=1)
        self.assertEqual(result["status"], "unverified_incomplete")
        self.assertEqual(result["project_queried_count"], 1)

    def test_project_dependencies_get_query_priority(self):
        with mock.patch.object(inspector.urllib.request, "urlopen", return_value=response({"results": [{}]})) as network:
            inspector.osv_query([dep("example", scope="example"), dep("project")], max_pkgs=1)
        request = json.loads(network.call_args.args[0].data)
        self.assertEqual(request["queries"][0]["package"]["name"], "project")

    def test_complete_clean_result_has_actual_query_count(self):
        with mock.patch.object(inspector.urllib.request, "urlopen", return_value=response({"results": [{}]})):
            result = inspector.osv_query([dep()])
        self.assertEqual(result["status"], "clean_on_queried_scope")
        self.assertTrue(result["complete"])

    def test_vulnerability_totals_are_not_truncated_with_display_rows(self):
        results = {"results": [{"vulns": [{"id": "GHSA-synthetic"}]}] * 61}
        with mock.patch.object(inspector.urllib.request, "urlopen", return_value=response(results)):
            result = inspector.osv_query([dep("package%d" % n) for n in range(61)])
        self.assertEqual(result["project_vulnerable_count"], 61)
        self.assertEqual(len(result["vulnerable_packages"]), 60)


class SecurityScanningTests(unittest.TestCase):
    def test_real_source_names_are_not_skipped_and_evidence_is_redacted(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            token = "ghp_" + "aB3" * 12
            for name in ("inspector.py", "latest.py"):
                (root / name).write_text('token = "' + token + '"')
            files, _ = inspector.walk_files(root)
            result = inspector.analyze_security(root, files, {})
            self.assertEqual(len(result["secrets"]), 2)
            self.assertNotIn(token, json.dumps(result))

    def test_private_key_inside_multiline_assignment_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "config.py").write_text('key = """-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----"""')
            files, _ = inspector.walk_files(root)
            result = inspector.analyze_security(root, files, {})
            self.assertTrue(any(item["type"] == "Private Key Block" for item in result["secrets"]))


class MainWorkflowTests(unittest.TestCase):
    def call_main(self, args):
        with mock.patch.object(inspector.sys, "argv", [str(SCRIPT)] + args), contextlib.redirect_stdout(io.StringIO()):
            return inspector.main()

    def test_local_mode_is_offline_by_default_and_excludes_nested_output(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "requirements.txt").write_text("requests==2.0")
            output = root / "audit"
            output.mkdir()
            (output / "previous.py").write_text("invalid python !")
            with mock.patch.object(inspector.urllib.request, "urlopen") as network:
                status = self.call_main(["--local", str(root), "--out", str(output), "--keep-clone"])
            network.assert_not_called()
            data = json.loads((output / "inspector-data.json").read_text(encoding="utf-8"))
            self.assertEqual(status, 0)
            self.assertEqual(data["access"]["files"]["file_count"], 1)
            self.assertEqual(data["security"]["dependency_audit"]["status"], "skipped")

    def test_existing_workdir_is_preserved_by_main(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            existing = root / "my-source"
            existing.mkdir()
            (existing / "keep.txt").write_text("keep")
            with mock.patch.object(inspector, "analyze_github", return_value={"meta": {"default_branch": "main"}}), \
                    mock.patch.object(inspector, "fetch_source", return_value={"ok": False, "error": "offline"}) as fetch:
                status = self.call_main(["owner/repo", "--stage", "clone", "--workdir", str(existing), "--out", str(root / "out")])
            self.assertEqual(status, 0)
            self.assertEqual((existing / "keep.txt").read_text(), "keep")
            self.assertNotEqual(fetch.call_args.args[1], existing)

    def test_metadata_stage_only_uses_one_api_request(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(inspector.GitHubAPI, "get", return_value={"status": 200, "data": {"full_name": "owner/repo"}}) as get, \
                    mock.patch.object(inspector, "fetch_source") as fetch:
                status = self.call_main(["owner/repo", "--stage", "repo", "--out", root])
            self.assertEqual(status, 0)
            self.assertEqual(get.call_count, 1)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
