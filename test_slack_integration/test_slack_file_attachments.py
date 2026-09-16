#!/usr/bin/env python3
"""
Tests for Slack file attachment handling in slack_fetch.py.

These tests verify:
- File extraction from message structures
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

from slack_fetch import (
    extract_files_from_message,
    download_message_files,
    sanitize_tag,
)

# Import attachment helpers if available
try:
    from attachment_downloader import format_attachments_section
    ATTACHMENTS_AVAILABLE = True
except ImportError:
    ATTACHMENTS_AVAILABLE = False


class TestExtractFilesFromMessage:
    """Tests for extract_files_from_message function."""

    def test_extract_single_file(self):
        """Test extracting a single file from message."""
        message = {
            'type': 'message',
            'text': 'Here is the report',
            'files': [
                {
                    'id': 'F07ABCDEF',
                    'name': 'report.pdf',
                    'title': 'Q4 Report',
                    'url_private_download': 'https://files.slack.com/files-pri/T123/F07ABCDEF-download/report.pdf',
                    'url_private': 'https://files.slack.com/files-pri/T123/F07ABCDEF/report.pdf',
                    'size': 125000,
                    'mimetype': 'application/pdf',
                    'filetype': 'pdf'
                }
            ]
        }

        files = extract_files_from_message(message)
        assert len(files) == 1
        assert files[0]['id'] == 'F07ABCDEF'
        assert files[0]['name'] == 'report.pdf'
        assert files[0]['size'] == 125000
        assert files[0]['mimetype'] == 'application/pdf'
        assert 'url_private_download' in files[0]

    def test_extract_multiple_files(self):
        """Test extracting multiple files from message."""
        message = {
            'type': 'message',
            'text': 'Screenshots attached',
            'files': [
                {
                    'id': 'F001',
                    'name': 'screenshot1.png',
                    'url_private_download': 'https://files.slack.com/files-pri/T123/F001/screenshot1.png',
                    'size': 50000,
                    'mimetype': 'image/png'
                },
                {
                    'id': 'F002',
                    'name': 'screenshot2.png',
                    'url_private_download': 'https://files.slack.com/files-pri/T123/F002/screenshot2.png',
                    'size': 60000,
                    'mimetype': 'image/png'
                }
            ]
        }

        files = extract_files_from_message(message)
        assert len(files) == 2
        assert files[0]['id'] == 'F001'
        assert files[1]['id'] == 'F002'

    def test_extract_no_files(self):
        """Test message without files returns empty list."""
        message = {
            'type': 'message',
            'text': 'Just a text message'
        }

        files = extract_files_from_message(message)
        assert len(files) == 0

    def test_extract_empty_files_array(self):
        """Test message with empty files array."""
        message = {
            'type': 'message',
            'text': 'Message with empty files',
            'files': []
        }

        files = extract_files_from_message(message)
        assert len(files) == 0

    def test_extract_skips_external_files(self):
        """Test that external file links are skipped."""
        message = {
            'type': 'message',
            'text': 'External link',
            'files': [
                {
                    'id': 'F123',
                    'name': 'external.pdf',
                    'mode': 'external',
                    'url_private': 'https://example.com/file.pdf'
                }
            ]
        }

        files = extract_files_from_message(message)
        assert len(files) == 0

    def test_extract_skips_tombstone_files(self):
        """Test that deleted (tombstone) files are skipped."""
        message = {
            'type': 'message',
            'text': 'Deleted file',
            'files': [
                {
                    'id': 'F999',
                    'name': 'deleted.pdf',
                    'mode': 'tombstone'
                }
            ]
        }

        files = extract_files_from_message(message)
        assert len(files) == 0

    def test_extract_mixed_files(self):
        """Test extracting with mix of valid and invalid files."""
        message = {
            'type': 'message',
            'text': 'Mixed files',
            'files': [
                {
                    'id': 'F001',
                    'name': 'valid.pdf',
                    'url_private_download': 'https://files.slack.com/valid.pdf',
                    'size': 1000,
                    'mimetype': 'application/pdf'
                },
                {
                    'id': 'F002',
                    'name': 'external.pdf',
                    'mode': 'external'
                },
                {
                    'id': 'F003',
                    'name': 'valid2.png',
                    'url_private': 'https://files.slack.com/valid2.png',
                    'size': 2000,
                    'mimetype': 'image/png'
                }
            ]
        }

        files = extract_files_from_message(message)
        assert len(files) == 2
        assert files[0]['id'] == 'F001'
        assert files[1]['id'] == 'F003'

    def test_extract_handles_missing_fields(self):
        """Test extraction with minimal file info."""
        message = {
            'type': 'message',
            'text': 'Minimal file info',
            'files': [
                {
                    'id': 'F123',
                    'url_private': 'https://files.slack.com/file'
                }
            ]
        }

        files = extract_files_from_message(message)
        assert len(files) == 1
        assert files[0]['id'] == 'F123'
        # Should have default values for missing fields
        assert 'name' in files[0]
        assert 'size' in files[0]
        assert 'mimetype' in files[0]


class TestDownloadMessageFiles:
    """Tests for download_message_files function."""

    @pytest.fixture
    def mock_downloader(self, tmp_path):
        """Create a mock downloader."""
        mock = MagicMock()
        mock.base_dir = tmp_path
        return mock

    def test_download_success(self, mock_downloader, tmp_path):
        """Test successful file download."""
        files = [
            {
                'id': 'F001',
                'name': 'report.pdf',
                'url_private_download': 'https://files.slack.com/report.pdf',
                'size': 1000,
                'mimetype': 'application/pdf'
            }
        ]

        # Mock successful download
        expected_path = str(tmp_path / 'slack' / 'C123' / '1234567890_report.pdf')
        mock_downloader.download_file.return_value = (expected_path, None)

        paths = download_message_files(
            files=files,
            bot_token='xoxb-ok',
            channel_id='C123',
            message_ts='1234567890.123456',
            downloader=mock_downloader
        )

        assert len(paths) == 1
        assert paths[0] == expected_path
        mock_downloader.download_file.assert_called_once()

    def test_download_multiple_files(self, mock_downloader, tmp_path):
        """Test downloading multiple files."""
        files = [
            {
                'id': 'F001',
                'name': 'file1.pdf',
                'url_private_download': 'https://files.slack.com/file1.pdf',
                'size': 1000,
                'mimetype': 'application/pdf'
            },
            {
                'id': 'F002',
                'name': 'file2.png',
                'url_private_download': 'https://files.slack.com/file2.png',
                'size': 2000,
                'mimetype': 'image/png'
            }
        ]

        # Mock successful downloads
        mock_downloader.download_file.side_effect = [
            (str(tmp_path / 'file1.pdf'), None),
            (str(tmp_path / 'file2.png'), None)
        ]

        paths = download_message_files(
            files=files,
            bot_token='xoxb-ok',
            channel_id='C123',
            message_ts='1234567890.123456',
            downloader=mock_downloader
        )

        assert len(paths) == 2
        assert mock_downloader.download_file.call_count == 2

    def test_download_handles_failure(self, mock_downloader):
        """Test handling of download failures."""
        files = [
            {
                'id': 'F001',
                'name': 'file.pdf',
                'url_private_download': 'https://files.slack.com/file.pdf',
                'size': 1000,
                'mimetype': 'application/pdf'
            }
        ]

        # Mock failed download
        mock_downloader.download_file.return_value = (None, "HTTP error: 403")

        paths = download_message_files(
            files=files,
            bot_token='xoxb-ok',
            channel_id='C123',
            message_ts='1234567890.123456',
            downloader=mock_downloader
        )

        assert len(paths) == 0

    def test_download_empty_list(self, mock_downloader):
        """Test with empty file list."""
        paths = download_message_files(
            files=[],
            bot_token='xoxb-ok',
            channel_id='C123',
            message_ts='1234567890.123456',
            downloader=mock_downloader
        )

        assert len(paths) == 0
        mock_downloader.download_file.assert_not_called()

    def test_download_skips_no_url(self, mock_downloader):
        """Test that files without URLs are skipped."""
        files = [
            {
                'id': 'F001',
                'name': 'no-url-file.pdf',
                'size': 1000
                # No url_private_download or url_private
            }
        ]

        paths = download_message_files(
            files=files,
            bot_token='xoxb-ok',
            channel_id='C123',
            message_ts='1234567890.123456',
            downloader=mock_downloader
        )

        assert len(paths) == 0
        mock_downloader.download_file.assert_not_called()

    def test_download_uses_fallback_url(self, mock_downloader, tmp_path):
        """Test fallback to url_private when url_private_download missing."""
        files = [
            {
                'id': 'F001',
                'name': 'file.pdf',
                'url_private': 'https://files.slack.com/fallback-url/file.pdf',
                'size': 1000,
                'mimetype': 'application/pdf'
            }
        ]

        expected_path = str(tmp_path / 'file.pdf')
        mock_downloader.download_file.return_value = (expected_path, None)

        paths = download_message_files(
            files=files,
            bot_token='xoxb-ok',
            channel_id='C123',
            message_ts='1234567890.123456',
            downloader=mock_downloader
        )

        assert len(paths) == 1
        # Verify the fallback URL was used
        call_args = mock_downloader.download_file.call_args
        assert 'fallback-url' in call_args.kwargs.get('url', call_args[1].get('url', ''))

    def test_download_includes_auth_header(self, mock_downloader, tmp_path):
        """Test that authorization header is included."""
        files = [
            {
                'id': 'F001',
                'name': 'file.pdf',
                'url_private_download': 'https://files.slack.com/file.pdf',
                'size': 1000,
                'mimetype': 'application/pdf'
            }
        ]

        mock_downloader.download_file.return_value = (str(tmp_path / 'file.pdf'), None)

        download_message_files(
            files=files,
            bot_token='xoxb-ok',
            channel_id='C123',
            message_ts='1234567890.123456',
            downloader=mock_downloader
        )

        call_args = mock_downloader.download_file.call_args
        headers = call_args.kwargs.get('headers', {})
        assert 'Authorization' in headers
        assert 'Bearer xoxb-ok' in headers['Authorization']


@pytest.mark.skipif(not ATTACHMENTS_AVAILABLE, reason="attachment_downloader not available")
class TestFormatAttachmentsSection:
    """Tests for formatting attachment sections in task text."""

    def test_format_with_files(self):
        """Test formatting with multiple files."""
        paths = [
            './.juno_task/attachments/slack/C123/1234_report.pdf',
            './.juno_task/attachments/slack/C123/1234_image.png'
        ]

        result = format_attachments_section(paths)
        assert '[attached files]' in result
        assert '- ./.juno_task/attachments/slack/C123/1234_report.pdf' in result
        assert '- ./.juno_task/attachments/slack/C123/1234_image.png' in result

    def test_format_empty_list(self):
        """Test formatting with no files."""
        result = format_attachments_section([])
        assert result == ""


class TestSanitizeTag:
    """Tests for tag sanitization (used for author tags)."""

    def test_sanitize_simple(self):
        """Test basic tag sanitization."""
        assert sanitize_tag('author_john') == 'author_john'

    def test_sanitize_with_spaces(self):
        """Test tag with spaces."""
        assert sanitize_tag('author John Doe') == 'author_John_Doe'

    def test_sanitize_with_special_chars(self):
        """Test tag with special characters."""
        result = sanitize_tag('author:john@example.com')
        assert ':' not in result
        assert '@' not in result
        assert '.' not in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
