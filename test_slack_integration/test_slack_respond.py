"""
Unit tests for slack_respond.py - Slack response sender module.

Tests cover:
- Kanban task fetching
- Message matching (by task_id and text)
- Slack response sending
- Environment validation
- Error handling
- Duplicate prevention

Uses mocking to avoid external dependencies (Slack API, subprocess calls).
Note: These tests require slack_sdk and python-dotenv to be installed.
Requires Python 3.9+ due to type hints in the source module.
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

# Add the scripts directory to path to import slack_respond
scripts_dir = Path(__file__).parent.parent.parent / '.juno_task' / 'scripts'
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from slack_respond import (
    get_kanban_tasks,
    find_matching_message,
    send_slack_response,
    find_kanban_script,
    validate_slack_environment,
    normalize_text,
    compute_text_similarity,
)
from slack_state import SlackStateManager
from slack_sdk.errors import SlackApiError


class TestGetKanbanTasks:
    """Tests for get_kanban_tasks function."""

    @patch('subprocess.run')
    def test_fetches_tasks_successfully(self, mock_run):
        """Should fetch and parse kanban tasks."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {'id': 'task_1', 'body': 'Bug 1', 'agent_response': 'Fixed'},
                {'id': 'task_2', 'body': 'Bug 2', 'agent_response': 'Done'},
            ])
        )

        result = get_kanban_tasks('/path/to/kanban.sh')

        assert len(result) == 2
        assert result[0]['id'] == 'task_1'
        assert result[1]['agent_response'] == 'Done'

    @patch('subprocess.run')
    def test_filters_by_tag(self, mock_run):
        """Should pass tag filter to kanban command."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[]'
        )

        get_kanban_tasks('/path/to/kanban.sh', tag='slack-input')

        call_args = mock_run.call_args[0][0]
        assert '--tag' in call_args
        assert 'slack-input' in call_args

    @patch('subprocess.run')
    def test_filters_by_status(self, mock_run):
        """Should pass status filter to kanban command."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[]'
        )

        get_kanban_tasks('/path/to/kanban.sh', status='done')

        call_args = mock_run.call_args[0][0]
        assert '--status' in call_args
        assert 'done' in call_args

    @patch('subprocess.run')
    def test_handles_subprocess_failure(self, mock_run):
        """Should return empty list on subprocess failure."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr='Error'
        )

        result = get_kanban_tasks('/path/to/kanban.sh')

        assert result == []

    @patch('subprocess.run')
    def test_handles_invalid_json(self, mock_run):
        """Should return empty list on invalid JSON output."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='not valid json'
        )

        result = get_kanban_tasks('/path/to/kanban.sh')

        assert result == []

    @patch('subprocess.run')
    def test_handles_timeout(self, mock_run):
        """Should return empty list on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired('kanban', 30)

        result = get_kanban_tasks('/path/to/kanban.sh')

        assert result == []


class TestNormalizeText:
    """Tests for normalize_text function - handles Slack-specific formatting."""

    def test_empty_string(self):
        """Should return empty string for empty input."""
        assert normalize_text('') == ''
        assert normalize_text(None) == ''

    def test_plain_text_unchanged(self):
        """Plain text without formatting should be unchanged (except whitespace)."""
        assert normalize_text('Hello world') == 'Hello world'
        assert normalize_text('Bug report: something is broken') == 'Bug report: something is broken'

    def test_slack_link_with_label(self):
        """Should extract label from Slack links: <url|label> -> label."""
        assert normalize_text('<https://example.com|Click here>') == 'Click here'
        assert normalize_text('Check <https://github.com/user/repo|this repo> for details') == \
            'Check this repo for details'

    def test_slack_bare_link(self):
        """Should remove angle brackets from bare links: <url> -> url."""
        assert normalize_text('<https://example.com>') == 'https://example.com'
        assert normalize_text('Visit <https://docs.example.com/api> for API docs') == \
            'Visit https://docs.example.com/api for API docs'

    def test_slack_mailto_links(self):
        """Should handle mailto links."""
        assert normalize_text('<mailto:user@example.com|user@example.com>') == 'user@example.com'
        assert normalize_text('<mailto:admin@test.com>') == 'admin@test.com'

    def test_slack_user_mentions(self):
        """Should normalize user mentions: <@U123> -> @user."""
        assert normalize_text('Hi <@U12345ABC>!') == 'Hi @user!'
        assert normalize_text('<@UABCD1234> mentioned <@U9876ZYXW>') == '@user mentioned @user'

    def test_slack_channel_mentions(self):
        """Should normalize channel mentions."""
        # The normalization extracts the channel name label from <#ID|name> format
        assert normalize_text('Check <#C123456|general> for updates') == 'Check general for updates'
        assert normalize_text('Post to <#C999888>') == 'Post to #channel'

    def test_slack_markdown_bold(self):
        """Should remove bold formatting: *text* -> text."""
        assert normalize_text('This is *important*') == 'This is important'
        assert normalize_text('*Bold* at start and *bold* at end') == 'Bold at start and bold at end'

    def test_slack_markdown_italic(self):
        """Should remove italic formatting: _text_ -> text."""
        assert normalize_text('This is _emphasized_') == 'This is emphasized'

    def test_slack_markdown_strikethrough(self):
        """Should remove strikethrough: ~text~ -> text."""
        assert normalize_text('This was ~removed~') == 'This was removed'

    def test_mixed_formatting(self):
        """Should handle multiple formatting types together."""
        text = '*Bold* and _italic_ with <https://example.com|link>'
        assert normalize_text(text) == 'Bold and italic with link'

    def test_whitespace_normalization(self):
        """Should collapse multiple whitespace to single space."""
        assert normalize_text('Hello    world') == 'Hello world'
        assert normalize_text('Line1\n\nLine2') == 'Line1 Line2'
        assert normalize_text('Tab\there') == 'Tab here'
        assert normalize_text('  leading and trailing  ') == 'leading and trailing'

    def test_json_escaped_quotes(self):
        """Should normalize escaped quotes in JSON-like content."""
        assert normalize_text('Error: {\\"code\\": 500}') == 'Error: {"code": 500}'
        assert normalize_text("It\\'s working") == "It's working"

    def test_complex_message_sample(self):
        """Test the sample message from the user's issue."""
        # This is similar to the sample that caused matching to fail
        sample = """**Task ID: gvRl4t** is not completed

Error 500 on Jobs page on gavix_io

Request URL
http://localhost:3003/api/v1/gavix/jobs?page=1&limit=20Request Method
GETStatus Code
500 Internal Server Error

{
    "error": "Internal server error",
    "detail": "'Settings' object has no attribute 'ADMIN_EMAIL_LIST'"
}"""
        normalized = normalize_text(sample)
        # Should normalize whitespace but preserve meaningful content
        assert 'Task ID: gvRl4t' in normalized
        assert 'Error 500' in normalized
        assert 'Internal server error' in normalized
        assert '  ' not in normalized  # No double spaces


class TestComputeTextSimilarity:
    """Tests for compute_text_similarity function."""

    def test_identical_texts(self):
        """Should return 1.0 for identical texts."""
        assert compute_text_similarity('Hello world', 'Hello world') == 1.0

    def test_empty_texts(self):
        """Should return 0.0 for empty texts."""
        assert compute_text_similarity('', '') == 0.0
        assert compute_text_similarity('text', '') == 0.0
        assert compute_text_similarity('', 'text') == 0.0

    def test_similar_texts(self):
        """Should return high similarity for similar texts."""
        # Minor difference should still be high similarity
        sim = compute_text_similarity('Hello world', 'Hello World')
        assert sim > 0.8  # Case difference, should be similar

    def test_different_texts(self):
        """Should return low similarity for different texts."""
        sim = compute_text_similarity('Hello world', 'Goodbye universe')
        assert sim < 0.5

    def test_partial_match(self):
        """Should handle texts where one contains the other."""
        sim = compute_text_similarity('Bug report', 'Bug report with extra details')
        # Should be reasonably similar but not identical
        assert 0.3 < sim < 0.8


class TestFindMatchingMessage:
    """Tests for find_matching_message function."""

    def test_finds_by_task_id(self):
        """Should find message by task_id lookup."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            state_mgr.mark_processed('ts1', 'task_1', {
                'text': 'Bug report',
                'author': 'user1',
                'channel_id': 'C123'
            })

            task = {'id': 'task_1', 'body': 'Bug report'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
            assert result['task_id'] == 'task_1'
        finally:
            os.unlink(state_file)

    def test_finds_by_text_match_exact(self):
        """Should find message by exact text match as fallback."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            state_mgr.mark_processed('ts1', 'different_task', {
                'text': 'Bug report',
                'channel_id': 'C123'
            })

            # Task with different ID but matching body
            task = {'id': 'unknown_task', 'body': 'Bug report'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_finds_by_text_prefix_match(self):
        """Should find message by text prefix match."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'Bug report: something is broken',
                'channel_id': 'C123'
            })

            # Task body starts with message text
            task = {'id': 'unknown', 'body': 'Bug report: something is broken and more details here'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_returns_none_if_no_match(self):
        """Should return None if no matching message found."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            state_mgr.mark_processed('ts1', 'task_1', {
                'text': 'First message',
                'channel_id': 'C123'
            })

            task = {'id': 'task_999', 'body': 'Completely different text'}

            result = find_matching_message(task, state_mgr)

            assert result is None
        finally:
            os.unlink(state_file)

    def test_matches_with_slack_link_formatting(self):
        """Should match when Slack links differ in formatting."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            # Message stored with Slack link format
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'Check <https://example.com|this link> for details',
                'channel_id': 'C123'
            })

            # Task body with plain text (link label extracted)
            task = {'id': 'unknown', 'body': 'Check this link for details'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_matches_with_json_content(self):
        """Should match when JSON formatting differs (escaped vs unescaped)."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            # Message with escaped JSON
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'Error: {\\"code\\": 500, \\"msg\\": \\"Server error\\"}',
                'channel_id': 'C123'
            })

            # Task body with normal JSON
            task = {'id': 'unknown', 'body': 'Error: {"code": 500, "msg": "Server error"}'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_matches_with_whitespace_differences(self):
        """Should match when whitespace formatting differs."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            # Message with multiple newlines and spaces
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'Bug report:\n\n  Multiple lines\n\n  And spaces',
                'channel_id': 'C123'
            })

            # Task body with normalized whitespace
            task = {'id': 'unknown', 'body': 'Bug report: Multiple lines And spaces'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_matches_with_slack_markdown(self):
        """Should match when Slack markdown formatting differs."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            # Message with Slack markdown
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'This is *important* and _urgent_',
                'channel_id': 'C123'
            })

            # Task body without markdown
            task = {'id': 'unknown', 'body': 'This is important and urgent'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_matches_with_user_mentions(self):
        """Should match when user mentions differ."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            # Message with Slack user mention
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'Hey <@U12345ABC> check this',
                'channel_id': 'C123'
            })

            # Task body with normalized mention
            task = {'id': 'unknown', 'body': 'Hey @user check this'}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_fuzzy_match_similar_texts(self):
        """Should match via fuzzy matching when texts are similar but not identical."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            # Original message
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'Bug report: login page shows error 500 when clicking submit',
                'channel_id': 'C123'
            })

            # Task body with minor typo correction
            task = {
                'id': 'unknown',
                'body': 'Bug report: login page shows error 500 when clicking Submit'
            }

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_no_match_below_similarity_threshold(self):
        """Should not match when similarity is below threshold."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': 'Bug report: login page shows error 500',
                'channel_id': 'C123'
            })

            # Completely different body
            task = {
                'id': 'unknown',
                'body': 'Feature request: add dark mode support to the UI'
            }

            result = find_matching_message(task, state_mgr)

            assert result is None
        finally:
            os.unlink(state_file)

    def test_complex_formatting_sample_from_issue(self):
        """Test the actual sample from the user's reported issue (HPT2bi)."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            # Original message as it might appear in Slack (with formatting)
            slack_message = """**Task ID: gvRl4t** is not completed

Error 500 on Jobs page on gavix_io

Request URL
http://localhost:3003/api/v1/gavix/jobs?page=1&limit=20Request Method
GETStatus Code
500 Internal Server Error

{
    "error": "Internal server error",
    "detail": "'Settings' object has no attribute 'ADMIN_EMAIL_LIST'"
}"""
            state_mgr.mark_processed('ts1', 'some_task', {
                'text': slack_message,
                'channel_id': 'C123'
            })

            # Task body might have whitespace normalized
            task_body = """**Task ID: gvRl4t** is not completed Error 500 on Jobs page on gavix_io Request URL http://localhost:3003/api/v1/gavix/jobs?page=1&limit=20Request Method GETStatus Code 500 Internal Server Error { "error": "Internal server error", "detail": "'Settings' object has no attribute 'ADMIN_EMAIL_LIST'" }"""

            task = {'id': 'unknown', 'body': task_body}

            result = find_matching_message(task, state_mgr)

            # Should match through normalized text comparison
            assert result is not None
            assert result['ts'] == 'ts1'
        finally:
            os.unlink(state_file)

    def test_empty_body_task_uses_task_id_only(self):
        """Tasks with empty body should only try task_id lookup."""
        with tempfile.NamedTemporaryFile(suffix='.ndjson', delete=False) as f:
            state_file = f.name

        try:
            state_mgr = SlackStateManager(state_file)
            state_mgr.mark_processed('ts1', 'task_1', {
                'text': 'Some message',
                'channel_id': 'C123'
            })

            # Task with matching ID but empty body
            task = {'id': 'task_1', 'body': ''}

            result = find_matching_message(task, state_mgr)

            assert result is not None
            assert result['ts'] == 'ts1'

            # Task with non-matching ID and empty body
            task2 = {'id': 'task_999', 'body': ''}
            result2 = find_matching_message(task2, state_mgr)
            assert result2 is None
        finally:
            os.unlink(state_file)


class TestSendSlackResponse:
    """Tests for send_slack_response function."""

    def test_dry_run_returns_ts(self):
        """Dry run should return placeholder timestamp without API call."""
        client = MagicMock()

        result = send_slack_response(
            client,
            'C123456',
            'ts1',
            'task_1',
            'This is the response',
            dry_run=True
        )

        assert result == 'dry-run-ts'
        client.chat_postMessage.assert_not_called()

    def test_sends_threaded_response(self):
        """Should send response as threaded reply."""
        client = MagicMock()
        client.chat_postMessage.return_value = {
            'ok': True,
            'ts': 'response_ts_123'
        }

        result = send_slack_response(
            client,
            'C123456',
            'ts1',
            'task_1',
            'This is the response',
            dry_run=False
        )

        assert result == 'response_ts_123'
        client.chat_postMessage.assert_called_once()

        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs['channel'] == 'C123456'
        assert call_kwargs['thread_ts'] == 'ts1'
        assert 'task_1' in call_kwargs['text']
        assert 'This is the response' in call_kwargs['text']

    def test_handles_api_error(self):
        """Should return None on API error."""
        client = MagicMock()
        client.chat_postMessage.side_effect = SlackApiError(
            message="channel_not_found",
            response={'error': 'channel_not_found'}
        )

        result = send_slack_response(
            client,
            'C123456',
            'ts1',
            'task_1',
            'Response',
            dry_run=False
        )

        assert result is None

    def test_handles_rate_limit(self):
        """Should return None on rate limit."""
        client = MagicMock()
        error_response = MagicMock()
        error_response.headers = {'Retry-After': '60'}
        error_response.get.return_value = 'ratelimited'
        client.chat_postMessage.side_effect = SlackApiError(
            message="ratelimited",
            response=error_response
        )

        result = send_slack_response(
            client,
            'C123456',
            'ts1',
            'task_1',
            'Response',
            dry_run=False
        )

        assert result is None


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
            token, errors = validate_slack_environment()

            assert token == 'xoxb-ok'
            assert errors == []

    def test_missing_token(self):
        """Should error if token missing."""
        orig_token = os.environ.pop('SLACK_BOT_TOKEN', None)
        try:
            token, errors = validate_slack_environment()

            assert token is None
            assert len(errors) > 0
            assert 'SLACK_BOT_TOKEN not found' in errors[0]
        finally:
            if orig_token:
                os.environ['SLACK_BOT_TOKEN'] = orig_token

    def test_invalid_token_format(self):
        """Should error if token doesn't start with xoxb-."""
        with patch.dict(os.environ, {'SLACK_BOT_TOKEN': 'invalid-token'}, clear=False):
            token, errors = validate_slack_environment()

            assert len(errors) > 0
            assert 'xoxb-' in errors[0]


class TestSlackRespondIntegration:
    """Integration tests simulating respond workflow."""

    def test_full_respond_workflow(self):
        """Test full workflow: match task -> send response -> track sent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from slack_state import ResponseStateManager

            # Setup state files
            state_dir = Path(tmpdir) / '.juno_task' / 'slack'
            state_dir.mkdir(parents=True)

            slack_state = SlackStateManager(str(state_dir / 'slack.ndjson'))
            response_state = ResponseStateManager(str(state_dir / 'responses.ndjson'))

            # Add some messages
            slack_state.mark_processed('ts1', 'task_1', {
                'text': 'Bug #1',
                'channel_id': 'C123456',
                'thread_ts': 'ts1'
            })
            slack_state.mark_processed('ts2', 'task_2', {
                'text': 'Bug #2',
                'channel_id': 'C123456',
                'thread_ts': 'ts2'
            })

            # Simulate tasks with responses
            tasks = [
                {'id': 'task_1', 'body': 'Bug #1', 'agent_response': 'Fixed bug #1'},
                {'id': 'task_2', 'body': 'Bug #2', 'agent_response': 'Fixed bug #2'},
            ]

            # Mock client
            client = MagicMock()
            client.chat_postMessage.return_value = {
                'ok': True,
                'ts': 'response_ts'
            }

            # Process tasks
            sent_count = 0
            for task in tasks:
                msg = find_matching_message(task, slack_state)
                if msg and not response_state.was_response_sent(task['id'], msg['ts']):
                    result = send_slack_response(
                        client,
                        msg['channel_id'],
                        msg.get('thread_ts', msg['ts']),
                        task['id'],
                        task['agent_response'],
                        dry_run=False
                    )
                    if result:
                        response_state.record_sent(
                            task['id'],
                            msg['ts'],
                            msg['channel_id'],
                            result
                        )
                        sent_count += 1

            assert sent_count == 2
            assert response_state.get_sent_count() == 2

            # Running again should not re-send
            sent_again = 0
            for task in tasks:
                msg = find_matching_message(task, slack_state)
                if msg and not response_state.was_response_sent(task['id'], msg['ts']):
                    sent_again += 1

            assert sent_again == 0

    def test_workflow_with_no_response(self):
        """Tasks without agent_response should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from slack_state import ResponseStateManager

            state_dir = Path(tmpdir) / '.juno_task' / 'slack'
            state_dir.mkdir(parents=True)

            slack_state = SlackStateManager(str(state_dir / 'slack.ndjson'))
            response_state = ResponseStateManager(str(state_dir / 'responses.ndjson'))

            slack_state.mark_processed('ts1', 'task_1', {
                'text': 'Bug #1',
                'channel_id': 'C123456'
            })

            # Task with no response
            tasks = [
                {'id': 'task_1', 'body': 'Bug #1', 'agent_response': ''},
                {'id': 'task_2', 'body': 'Bug #2', 'agent_response': 'null'},
            ]

            processed = 0
            for task in tasks:
                if not task['agent_response'] or task['agent_response'] == 'null':
                    continue
                msg = find_matching_message(task, slack_state)
                if msg:
                    processed += 1

            assert processed == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
