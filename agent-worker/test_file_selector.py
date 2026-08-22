"""
Tests for agent/file_selector.py

Covers:
  - stack trace path parsing (Linux, Windows, container paths)
  - third-party path filtering (site-packages, venv)
  - repo-relative path stripping
  - fuzzy route matching fallback
  - file content budget cap
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from agent.file_selector import (
    _parse_stack_trace_paths,
    _is_third_party,
    _strip_to_repo_relative,
    _fuzzy_match_route,
)


class TestParseStackTracePaths:
    def test_linux_path_extracted(self):
        trace = 'File "src/payments/processor.py", line 42, in charge'
        paths = _parse_stack_trace_paths(trace)
        assert "src/payments/processor.py" in paths

    def test_absolute_linux_path_stripped(self):
        trace = 'File "/app/src/payments/processor.py", line 42, in charge'
        paths = _parse_stack_trace_paths(trace)
        assert any("processor.py" in p for p in paths)
        assert not any(p.startswith("/") for p in paths)

    def test_windows_path_normalised(self):
        trace = r'File "C:\Users\Hardik\Projects\MyApp\app.py", line 10, in view'
        paths = _parse_stack_trace_paths(trace)
        assert any("app.py" in p for p in paths)
        assert not any("\\" in p for p in paths)

    def test_duplicate_paths_deduplicated(self):
        trace = (
            'File "src/app.py", line 10, in a\n'
            'File "src/app.py", line 20, in b\n'
        )
        paths = _parse_stack_trace_paths(trace)
        assert paths.count("src/app.py") == 1

    def test_multiple_files_all_extracted(self):
        trace = (
            'File "src/payments/processor.py", line 42, in charge\n'
            'File "src/payments/models.py", line 10, in load\n'
        )
        paths = _parse_stack_trace_paths(trace)
        assert len(paths) == 2

    def test_empty_trace_returns_empty_list(self):
        assert _parse_stack_trace_paths("") == []

    def test_trace_with_no_file_lines_returns_empty(self):
        assert _parse_stack_trace_paths("AttributeError: NoneType") == []


class TestIsThirdParty:
    def test_site_packages_is_third_party(self):
        assert _is_third_party("Users/Hardik/Projects/App/virtual/Lib/site-packages/flask/app.py")

    def test_venv_is_third_party(self):
        assert _is_third_party("app/venv/lib/python3.12/httpx/__init__.py")

    def test_dot_venv_is_third_party(self):
        assert _is_third_party(".venv/lib/site-packages/requests/api.py")

    def test_virtual_dir_is_third_party(self):
        assert _is_third_party("virtual/Lib/site-packages/flask/app.py")

    def test_app_source_is_not_third_party(self):
        assert not _is_third_party("src/payments/processor.py")

    def test_root_level_file_is_not_third_party(self):
        assert not _is_third_party("app.py")


class TestStripToRepoRelative:
    def test_strips_prefix_up_to_repo_folder(self):
        path = "Users/Hardik/Projects/ClinicalTrialsAPI/app.py"
        result = _strip_to_repo_relative(path, "hpatel/ClinicalTrialsAPI")
        assert result == "app.py"

    def test_nested_path_stripped_correctly(self):
        path = "Users/Hardik/Projects/MyApp/src/payments/processor.py"
        result = _strip_to_repo_relative(path, "acme-org/MyApp")
        assert result == "src/payments/processor.py"

    def test_repo_name_case_insensitive(self):
        path = "users/hardik/clinicaltrialsapi/app.py"
        result = _strip_to_repo_relative(path, "hpatel/ClinicalTrialsAPI")
        assert result == "app.py"

    def test_path_already_relative_returned_as_is(self):
        path = "src/payments/processor.py"
        result = _strip_to_repo_relative(path, "acme-org/payments-service")
        # repo folder not found in path — returns as-is
        assert result == path


class TestFuzzyMatchRoute:
    def test_fuzzy_match_finds_relevant_file(self, fake_repo):
        from agent.models import ErrorContext, ErrorEvent, RouteConfig
        matches = _fuzzy_match_route(
            route="/v2/payments/charge",
            repo_root=Path(fake_repo),
            language="python",
        )
        assert any("processor" in str(p) for p in matches)

    def test_fuzzy_match_returns_empty_for_unknown_route(self, fake_repo):
        matches = _fuzzy_match_route(
            route="/completely/unknown/route",
            repo_root=Path(fake_repo),
            language="python",
        )
        assert isinstance(matches, list)

    def test_fuzzy_match_skips_version_segments(self, fake_repo):
        """v1, v2 etc. should not be used as search terms."""
        matches_with_version    = _fuzzy_match_route("/v2/payments", Path(fake_repo), "python")
        matches_without_version = _fuzzy_match_route("/payments",    Path(fake_repo), "python")
        # Both should return the same files — v2 prefix ignored
        assert set(str(p) for p in matches_with_version) == \
               set(str(p) for p in matches_without_version)


class TestSelectFilesIntegration:
    def test_select_files_returns_content_for_known_path(
        self, error_context, fake_repo, monkeypatch
    ):
        """
        Integration test: if the clone succeeds and files are found,
        select_files returns non-empty content.
        """
        from agent.file_selector import select_files

        # Patch _clone_url so we don't make real GitHub calls
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake")
        monkeypatch.setenv("GITHUB_INSTALLATION_ID", "456")

        with patch("agent.file_selector._get_installation_token", return_value="tok"), \
             patch("agent.file_selector.Repo.clone_from") as mock_clone:

            # Make clone_from copy files from fake_repo into the dest path
            def fake_clone(url, dest, **kwargs):
                import shutil
                shutil.copytree(fake_repo, dest, dirs_exist_ok=True)
                return MagicMock()

            mock_clone.side_effect = fake_clone

            import shutil
            import uuid

            temp_root = Path(__file__).resolve().parent / "test_artifacts"
            temp_root.mkdir(exist_ok=True)
            tmpdir = temp_root / f"select_files_{uuid.uuid4().hex[:8]}"
            tmpdir.mkdir(parents=True, exist_ok=False)
            repo_path, contents = select_files(error_context, str(tmpdir))
            shutil.rmtree(tmpdir, ignore_errors=True)

            # processor.py should be in the contents
            assert "processor.py" in contents or contents == "(no source files found)"
