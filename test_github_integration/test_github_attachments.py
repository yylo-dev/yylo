#!/usr/bin/env python3
"""
Tests for GitHub file attachment handling in github.py.

These tests verify:
- URL extraction from issue body and comments
- Download functionality (mocked)
- Task text formatting with attachments
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add the scripts directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src' / 'templates' / 'scripts'))

from github import (
    extract_attachment_urls,
    download_github_attachments,
    sanitize_tag,
)

# Import attachment helpers if available
try:
    from attachment_downloader import format_attachments_section
    ATTACHMENTS_AVAILABLE = True
except ImportError:
    ATTACHMENTS_AVAILABLE = False


class TestExtractAttachmentUrls:
    """Tests for extract_attachment_urls function."""

    def test_extract_user_attachments_url(self):
        """Test extracting new-format user attachments URLs."""
        body = """
        Here's a screenshot:
        https://github.com/user-attachments/assets/abc123-def456-789/screenshot.png
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 1
        assert 'screenshot.png' in urls[0]
        assert 'user-attachments' in urls[0]

    def test_extract_user_attachments_files_url(self):
        """Test extracting user-attachments/files URLs with numeric IDs (file uploads)."""
        body = """
        Analyze the log file:
        [logs_result (1).csv](https://github.com/user-attachments/files/24988025/logs_result.1.csv)
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 1
        assert 'logs_result.1.csv' in urls[0]
        assert 'user-attachments/files' in urls[0]

    def test_extract_user_attachments_files_multiple(self):
        """Test extracting multiple user-attachments/files URLs."""
        body = """
        Here are the files:
        https://github.com/user-attachments/files/12345678/report.pdf
        https://github.com/user-attachments/files/87654321/data.json
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 2
        assert any('report.pdf' in url for url in urls)
        assert any('data.json' in url for url in urls)

    def test_extract_user_images_url(self):
        """Test extracting user-images.githubusercontent.com URLs."""
        body = """
        Bug reproduction:
        ![image](https://user-images.githubusercontent.com/12345/abc123-screenshot.png)
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 1
        assert 'user-images.githubusercontent.com' in urls[0]

    def test_extract_private_user_images(self):
        """Test extracting private-user-images URLs."""
        body = """
        Private screenshot:
        https://private-user-images.githubusercontent.com/12345/abc-image.jpg
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 1
        assert 'private-user-images' in urls[0]

    def test_extract_multiple_urls(self):
        """Test extracting multiple URLs from issue body."""
        body = """
        Before: https://github.com/user-attachments/assets/abc/before.png
        After: https://github.com/user-attachments/assets/def/after.png
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 2

    def test_extract_from_comments(self):
        """Test extracting URLs from comments."""
        body = "Original issue"
        comments = [
            {'body': 'Here: https://github.com/user-attachments/assets/123/fix.png'}
        ]

        urls = extract_attachment_urls(body, comments)
        assert len(urls) == 1
        assert 'fix.png' in urls[0]

    def test_extract_from_body_and_comments(self):
        """Test extracting URLs from both body and comments."""
        body = "Issue with: https://github.com/user-attachments/assets/111/issue.png"
        comments = [
            {'body': 'Reply with: https://github.com/user-attachments/assets/222/reply.png'},
            {'body': 'Another: https://user-images.githubusercontent.com/333/another.gif'}
        ]

        urls = extract_attachment_urls(body, comments)
        assert len(urls) == 3

    def test_deduplicate_urls(self):
        """Test that duplicate URLs are removed."""
        body = """
        Same image twice:
        https://github.com/user-attachments/assets/abc/image.png

        Referenced again:
        https://github.com/user-attachments/assets/abc/image.png
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 1

    def test_no_attachments(self):
        """Test issue without attachments."""
        body = "Just plain text, no images."
        urls = extract_attachment_urls(body)
        assert len(urls) == 0

    def test_ignores_regular_github_urls(self):
        """Test that regular GitHub URLs are not extracted."""
        body = """
        See issue at https://github.com/owner/repo/issues/123
        And PR at https://github.com/owner/repo/pull/456
        Also https://github.com/owner/repo/blob/main/README.md
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 0

    def test_handles_markdown_image_syntax(self):
        """Test extraction from markdown image syntax."""
        body = """
        ![Screenshot](https://user-images.githubusercontent.com/1234/screenshot.png)
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 1

    def test_handles_urls_in_parentheses(self):
        """Test extraction stops at parentheses."""
        body = """
        Check this out (https://github.com/user-attachments/assets/abc/file.pdf)
        """

        urls = extract_attachment_urls(body)
        assert len(urls) == 1
        # URL should not include closing parenthesis
        assert not urls[0].endswith(')')

    def test_empty_body(self):
        """Test with empty body."""
        urls = extract_attachment_urls("")
        assert len(urls) == 0

    def test_none_body(self):
        """Test with None body."""
        urls = extract_attachment_urls(None)
        assert len(urls) == 0

    def test_empty_comments(self):
        """Test with empty comments list."""
        body = "https://github.com/user-attachments/assets/abc/file.png"
        urls = extract_attachment_urls(body, [])
        assert len(urls) == 1


class TestDownloadGitHubAttachments:
    """Tests for download_github_attachments function."""

    @pytest.fixture
    def mock_downloader(self, tmp_path):
        """Create a mock downloader."""
        mock = MagicMock()
        mock.base_dir = tmp_path
        return mock

    def test_download_success(self, mock_downloader, tmp_path):
        """Test successful file download."""
        urls = ['https://github.com/user-attachments/assets/abc123/screenshot.png']

        # Mock successful download
        expected_path = str(tmp_path / 'github' / 'owner_repo' / 'issue_1_screenshot.png')
        mock_downloader.download_file.return_value = (expected_path, None)

        paths = download_github_attachments(
            urls=urls,
            token='ghp_ok',
            repo='owner/repo',
            issue_number=1,
            downloader=mock_downloader
        )

        assert len(paths) == 1
        assert paths[0] == expected_path
        mock_downloader.download_file.assert_called_once()

    def test_download_multiple_files(self, mock_downloader, tmp_path):
        """Test downloading multiple files."""
        urls = [
            'https://github.com/user-attachments/assets/abc/file1.png',
            'https://github.com/user-attachments/assets/def/file2.png'
        ]

        # Mock successful downloads
        mock_downloader.download_file.side_effect = [
            (str(tmp_path / 'file1.png'), None),
            (str(tmp_path / 'file2.png'), None)
        ]

        paths = download_github_attachments(
            urls=urls,
            token='ghp_ok',
            repo='owner/repo',
            issue_number=1,
            downloader=mock_downloader
        )

        assert len(paths) == 2
        assert mock_downloader.download_file.call_count == 2

    def test_download_handles_failure(self, mock_downloader):
        """Test handling of download failures."""
        urls = ['https://github.com/user-attachments/assets/abc/file.png']

        # Mock failed download
        mock_downloader.download_file.return_value = (None, "HTTP error: 403")

        paths = download_github_attachments(
            urls=urls,
            token='ghp_ok',
            repo='owner/repo',
            issue_number=1,
            downloader=mock_downloader
        )

        assert len(paths) == 0

    def test_download_empty_list(self, mock_downloader):
        """Test with empty URL list."""
        paths = download_github_attachments(
            urls=[],
            token='ghp_ok',
            repo='owner/repo',
            issue_number=1,
            downloader=mock_downloader
        )

        assert len(paths) == 0
        mock_downloader.download_file.assert_not_called()

    def test_download_includes_auth_header(self, mock_downloader, tmp_path):
        """Test that authorization header is included."""
        urls = ['https://github.com/user-attachments/assets/abc/file.png']

        mock_downloader.download_file.return_value = (str(tmp_path / 'file.png'), None)

        download_github_attachments(
            urls=urls,
            token='ghp_ok',
            repo='owner/repo',
            issue_number=1,
            downloader=mock_downloader
        )

        call_args = mock_downloader.download_file.call_args
        headers = call_args.kwargs.get('headers', {})
        assert 'Authorization' in headers
        assert 'ghp_ok' in headers['Authorization']

    def test_download_uses_issue_prefix(self, mock_downloader, tmp_path):
        """Test that filename prefix includes issue number."""
        urls = ['https://github.com/user-attachments/assets/abc/file.png']

        mock_downloader.download_file.return_value = (str(tmp_path / 'file.png'), None)

        download_github_attachments(
            urls=urls,
            token='ghp_ok',
            repo='owner/repo',
            issue_number=42,
            downloader=mock_downloader
        )

        call_args = mock_downloader.download_file.call_args
        prefix = call_args.kwargs.get('filename_prefix', '')
        assert 'issue_42' in prefix

    def test_download_creates_repo_subdirectory(self, mock_downloader, tmp_path):
        """Test that files are saved in repo-specific subdirectory."""
        urls = ['https://github.com/user-attachments/assets/abc/file.png']

        mock_downloader.download_file.return_value = (str(tmp_path / 'file.png'), None)

        download_github_attachments(
            urls=urls,
            token='ghp_ok',
            repo='myorg/myrepo',
            issue_number=1,
            downloader=mock_downloader
        )

        call_args = mock_downloader.download_file.call_args
        target_dir = call_args.kwargs.get('target_dir', call_args[1].get('target_dir', ''))
        # Repo directory should use underscore instead of slash
        assert 'myorg_myrepo' in str(target_dir)


@pytest.mark.skipif(not ATTACHMENTS_AVAILABLE, reason="attachment_downloader not available")
class TestFormatAttachmentsSection:
    """Tests for formatting attachment sections in task text."""

    def test_format_with_github_files(self):
        """Test formatting with GitHub files."""
        paths = [
            './.juno_task/attachments/github/owner_repo/issue_1_screenshot.png',
            './.juno_task/attachments/github/owner_repo/issue_1_error_log.txt'
        ]

        result = format_attachments_section(paths)
        assert '[attached files]' in result
        assert '- ./.juno_task/attachments/github/owner_repo/issue_1_screenshot.png' in result
        assert '- ./.juno_task/attachments/github/owner_repo/issue_1_error_log.txt' in result

    def test_format_empty_list(self):
        """Test formatting with no files."""
        result = format_attachments_section([])
        assert result == ""


class TestSanitizeTag:
    """Tests for tag sanitization."""

    def test_sanitize_simple(self):
        """Test basic tag sanitization."""
        assert sanitize_tag('author_john') == 'author_john'

    def test_sanitize_with_spaces(self):
        """Test tag with spaces."""
        assert sanitize_tag('label bug fix') == 'label_bug_fix'

    def test_sanitize_with_special_chars(self):
        """Test tag with special characters."""
        result = sanitize_tag('label:critical/high')
        assert ':' not in result
        assert '/' not in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
