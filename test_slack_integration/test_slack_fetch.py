"""
Unit tests for slack_fetch.py - Slack message fetcher module.

Tests cover:
- Channel ID resolution
- User info retrieval
- Message fetching and filtering
- Kanban task creation
- Environment validation
- Tag sanitization
- Main loop logic

Uses mocking to avoid external dependencies (Slack API, subprocess calls).
Note: These tests require slack_sdk and python-dotenv to be installed.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock

import pytest

# Skip all tests if slack_sdk is not installed
slack_sdk = pytest.importorskip("slack_sdk", reason="slack_sdk required for these tests")

# Add the scripts directory to path to import slack_fetch
scripts_dir = Path(__file__).parent.parent.parent / '.juno_task' / 'scripts'
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from slack_fetch import (
    get_channel_id,
    get_user_info,
    fetch_channel_messages,
    sanitize_tag,
    create_kanban_task,
    process_messages,
    find_kanban_script,
    validate_slack_environment,
)
from slack_sdk.errors import SlackApiError


class TestSanitizeTag:
    """Tests for sanitize_tag function."""

    def test_sanitize_basic_tag(self):
        """Basic alphanumeric tags should pass through."""
        assert sanitize_tag('valid-tag') == 'valid-tag'
        assert sanitize_tag('valid_tag') == 'valid_tag'
        assert sanitize_tag('ValidTag123') == 'ValidTag123'

    def test_sanitize_replaces_colons(self):
        """Colons should be replaced with underscores."""
        assert sanitize_tag('author:john') == 'author_john'
        assert sanitize_tag('tag:with:colons') == 'tag_with_colons'

    def test_sanitize_replaces_spaces(self):
        """Spaces should be replaced with underscores."""
        assert sanitize_tag('tag with spaces') == 'tag_with_spaces'
        assert sanitize_tag('John Doe') == 'John_Doe'

    def test_sanitize_removes_special_chars(self):
        """Special characters should be removed."""
        assert sanitize_tag('tag@#$%') == 'tag'
        assert sanitize_tag('author!test') == 'author_test'

    def test_sanitize_collapses_underscores(self):
        """Multiple underscores should be collapsed to one."""
        assert sanitize_tag('tag__double') == 'tag_double'
        assert sanitize_tag('a___b____c') == 'a_b_c'

    def test_sanitize_strips_edge_underscores(self):
        """Leading/trailing underscores should be removed."""
        assert sanitize_tag('_tag_') == 'tag'
        assert sanitize_tag('___tag___') == 'tag'


class TestGetChannelId:
    """Tests for get_channel_id function."""

    def test_returns_channel_id_if_already_id(self):
        """If input looks like a channel ID, return as-is."""
        client = MagicMock()

        # Channel IDs start with C and are 9+ chars
        result = get_channel_id(client, 'C1234567890')

        assert result == 'C1234567890'
        # Should not make API call
        client.conversations_list.assert_not_called()

    def test_strips_hash_prefix(self):
        """Should strip # prefix from channel name."""
        client = MagicMock()
        client.conversations_list.return_value = {
            'channels': [
                {'name': 'general', 'id': 'C111111111'}
            ]
        }

        result = get_channel_id(client, '#general')

        assert result == 'C111111111'

    def test_finds_public_channel(self):
        """Should find public channel by name."""
        client = MagicMock()
        client.conversations_list.return_value = {
            'channels': [
                {'name': 'bugs', 'id': 'C222222222'},
                {'name': 'features', 'id': 'C333333333'},
            ]
        }

        result = get_channel_id(client, 'bugs')

        assert result == 'C222222222'

    def test_finds_private_channel(self):
        """Should find private channel if public search fails."""
        client = MagicMock()
        # First call (public channels) returns empty
        # Second call (private channels) returns match
        client.conversations_list.side_effect = [
            {'channels': []},
            {'channels': [{'name': 'secret', 'id': 'G444444444'}]}
        ]

        result = get_channel_id(client, 'secret')

        assert result == 'G444444444'
        assert client.conversations_list.call_count == 2

    def test_returns_none_if_not_found(self):
        """Should return None if channel not found."""
        client = MagicMock()
        client.conversations_list.side_effect = [
            {'channels': []},
            {'channels': []}
        ]

        result = get_channel_id(client, 'nonexistent')

        assert result is None


class TestGetUserInfo:
    """Tests for get_user_info function."""

    def test_returns_display_name(self):
        """Should return user's display name."""
        client = MagicMock()
        client.users_info.return_value = {
            'user': {
                'name': 'jdoe',
                'profile': {
                    'display_name': 'John Doe',
                    'real_name': 'John D.'
                }
            }
        }

        result = get_user_info(client, 'U123456')

        assert result == 'John Doe'

    def test_falls_back_to_real_name(self):
        """Should fall back to real_name if display_name empty."""
        client = MagicMock()
        client.users_info.return_value = {
            'user': {
                'name': 'jdoe',
                'profile': {
                    'display_name': '',
                    'real_name': 'John D.'
                }
            }
        }

        result = get_user_info(client, 'U123456')

        assert result == 'John D.'

    def test_falls_back_to_user_id_on_error(self):
        """Should return user_id if API call fails."""
        client = MagicMock()
        client.users_info.side_effect = SlackApiError(
            message="user_not_found",
            response={'error': 'user_not_found'}
        )

        result = get_user_info(client, 'U123456')

        assert result == 'U123456'


class TestFetchChannelMessages:
    """Tests for fetch_channel_messages function."""

    def test_fetches_messages(self):
        """Should fetch messages from channel."""
        client = MagicMock()
        client.conversations_history.return_value = {
            'messages': [
                {'type': 'message', 'text': 'Hello', 'ts': '1234.5678'},
                {'type': 'message', 'text': 'World', 'ts': '1234.5679'},
            ]
        }

        result = fetch_channel_messages(client, 'C123456')

        assert len(result) == 2
        assert result[0]['text'] == 'Hello'

    def test_filters_bot_messages(self):
        """Should filter out bot messages and system messages."""
        client = MagicMock()
        client.conversations_history.return_value = {
            'messages': [
                {'type': 'message', 'text': 'User msg', 'ts': '1234.0001'},
                {'type': 'message', 'text': 'Bot msg', 'ts': '1234.0002', 'subtype': 'bot_message'},
                {'type': 'message', 'text': 'System', 'ts': '1234.0003', 'subtype': 'channel_join'},
            ]
        }

        result = fetch_channel_messages(client, 'C123456')

        assert len(result) == 1
        assert result[0]['text'] == 'User msg'

    def test_passes_oldest_parameter(self):
        """Should pass oldest_ts to API call."""
        client = MagicMock()
        client.conversations_history.return_value = {'messages': []}

        fetch_channel_messages(client, 'C123456', oldest_ts='1234.5678')

        client.conversations_history.assert_called_once_with(
            channel='C123456',
            limit=100,
            oldest='1234.5678'
        )

    def test_handles_api_error(self):
        """Should return empty list on API error."""
        client = MagicMock()
        client.conversations_history.side_effect = SlackApiError(
            message="channel_not_found",
            response={'error': 'channel_not_found'}
        )

        result = fetch_channel_messages(client, 'C123456')

        assert result == []


class TestCreateKanbanTask:
    """Tests for create_kanban_task function."""

    def test_dry_run_returns_id(self):
        """Dry run should return placeholder ID without subprocess."""
        result = create_kanban_task(
            'Test message',
            'testuser',
            ['tag1', 'tag2'],
            '/path/to/kanban.sh',
            dry_run=True
        )

        assert result == 'dry-run-task-id'

    @patch('subprocess.run')
    def test_creates_task_successfully(self, mock_run):
        """Should create task and return ID."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"id": "abc123"}]'
        )

        result = create_kanban_task(
            'Test message',
            'testuser',
            ['tag1'],
            '/path/to/kanban.sh',
            dry_run=False
        )

        assert result == 'abc123'
        mock_run.assert_called_once()

    @patch('subprocess.run')
    def test_handles_subprocess_failure(self, mock_run):
        """Should return None on subprocess failure."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr='Error creating task'
        )

        result = create_kanban_task(
            'Test message',
            'testuser',
            ['tag1'],
            '/path/to/kanban.sh',
            dry_run=False
        )

        assert result is None

    @patch('subprocess.run')
    def test_handles_timeout(self, mock_run):
        """Should return None on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired('kanban', 30)

        result = create_kanban_task(
            'Test message',
            'testuser',
            ['tag1'],
            '/path/to/kanban.sh',
            dry_run=False
        )

        assert result is None


class TestProcessMessages:
    """Tests for process_messages function."""

    def test_processes_new_messages(self):
        """Should process new messages and create tasks."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            from slack_state import SlackStateManager
            state_mgr = SlackStateManager(state_file)
            client = MagicMock()
            client.users_info.return_value = {
                'user': {'profile': {'display_name': 'John'}}
            }

            messages = [
                {'ts': 'ts1', 'user': 'U123', 'text': 'Bug report'}
            ]

            with patch('slack_fetch.create_kanban_task') as mock_create:
                mock_create.return_value = 'task_123'

                result = process_messages(
                    messages,
                    'bugs',
                    'C123456',
                    client,
                    state_mgr,
                    '/path/to/kanban.sh',
                    dry_run=False
                )

            assert result == 1
            assert state_mgr.is_processed('ts1') is True
        finally:
            os.unlink(state_file)

    def test_skips_already_processed(self):
        """Should skip already processed messages."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            from slack_state import SlackStateManager
            state_mgr = SlackStateManager(state_file)
            state_mgr.mark_processed('ts1', 'existing_task', {'text': 'Old'})

            client = MagicMock()
            messages = [
                {'ts': 'ts1', 'user': 'U123', 'text': 'Already seen'}
            ]

            with patch('slack_fetch.create_kanban_task') as mock_create:
                result = process_messages(
                    messages,
                    'bugs',
                    'C123456',
                    client,
                    state_mgr,
                    '/path/to/kanban.sh',
                    dry_run=False
                )

            assert result == 0
            mock_create.assert_not_called()
        finally:
            os.unlink(state_file)

    def test_skips_empty_messages(self):
        """Should skip empty messages."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            from slack_state import SlackStateManager
            state_mgr = SlackStateManager(state_file)
            client = MagicMock()

            messages = [
                {'ts': 'ts1', 'user': 'U123', 'text': ''},
                {'ts': 'ts2', 'user': 'U123', 'text': '   '},
            ]

            with patch('slack_fetch.create_kanban_task') as mock_create:
                result = process_messages(
                    messages,
                    'bugs',
                    'C123456',
                    client,
                    state_mgr,
                    '/path/to/kanban.sh',
                    dry_run=False
                )

            assert result == 0
            mock_create.assert_not_called()
        finally:
            os.unlink(state_file)


class TestFindKanbanScript:
    """Tests for find_kanban_script function."""

    def test_finds_script_in_juno_task(self):
        """Should find kanban.sh in .juno_task/scripts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            scripts_dir = project_dir / '.juno_task' / 'scripts'
            scripts_dir.mkdir(parents=True)
            kanban_script = scripts_dir / 'kanban.sh'
            kanban_script.touch()

            result = find_kanban_script(project_dir)

            assert result == str(kanban_script)

    def test_returns_none_if_not_found(self):
        """Should return None if script not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            result = find_kanban_script(project_dir)

            assert result is None


class TestValidateSlackEnvironment:
    """Tests for validate_slack_environment function."""

    def test_valid_token(self):
        """Should pass with valid bot token."""
        with patch.dict(os.environ, {'SLACK_BOT_TOKEN': 'xoxb-ok'}, clear=False):
            token, channel, errors = validate_slack_environment()

            assert token == 'xoxb-ok'
            assert errors == []

    def test_missing_token(self):
        """Should error if token missing."""
        # Save and clear SLACK_BOT_TOKEN
        orig_token = os.environ.pop('SLACK_BOT_TOKEN', None)
        try:
            token, channel, errors = validate_slack_environment()

            assert token is None
            assert len(errors) > 0
            assert 'SLACK_BOT_TOKEN not found' in errors[0]
        finally:
            # Restore if it existed
            if orig_token:
                os.environ['SLACK_BOT_TOKEN'] = orig_token

    def test_invalid_token_format(self):
        """Should error if token doesn't start with xoxb-."""
        with patch.dict(os.environ, {'SLACK_BOT_TOKEN': 'invalid-token'}, clear=False):
            token, channel, errors = validate_slack_environment()

            assert len(errors) > 0
            assert 'xoxb-' in errors[0]

    def test_channel_from_env(self):
        """Should read channel from environment."""
        with patch.dict(os.environ, {
            'SLACK_BOT_TOKEN': 'xoxb-ok',
            'SLACK_CHANNEL': 'bug-reports'
        }, clear=False):
            token, channel, errors = validate_slack_environment()

            assert channel == 'bug-reports'
            assert errors == []


class TestSlackFetchIntegration:
    """Integration tests simulating end-to-end flow with mocking."""

    @patch('subprocess.run')
    def test_full_fetch_workflow(self, mock_run):
        """Test full workflow: fetch messages -> create tasks -> update state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup mock kanban creation
            task_counter = [0]
            def create_task(*args, **kwargs):
                task_counter[0] += 1
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps([{'id': f'task_{task_counter[0]}'}])
                )
            mock_run.side_effect = create_task

            # Setup mock Slack client
            mock_client = MagicMock()
            mock_client.users_info.side_effect = lambda user: {
                'user': {'profile': {'display_name': f'User_{user}'}}
            }

            # Setup state files
            state_dir = Path(tmpdir) / '.juno_task' / 'slack'
            state_dir.mkdir(parents=True)

            from slack_state import SlackStateManager
            state_mgr = SlackStateManager(str(state_dir / 'slack.ndjson'))

            # Simulated messages
            messages = [
                {'type': 'message', 'text': 'Bug #1', 'ts': '1000.001', 'user': 'U001'},
                {'type': 'message', 'text': 'Bug #2', 'ts': '1000.002', 'user': 'U002'},
            ]

            # Process messages
            result = process_messages(
                messages,
                'bugs',
                'C123456',
                mock_client,
                state_mgr,
                '/path/to/kanban.sh',
                dry_run=False
            )

            # Verify
            assert result == 2
            assert state_mgr.get_message_count() == 2
            assert state_mgr.is_processed('1000.001') is True
            assert state_mgr.is_processed('1000.002') is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
