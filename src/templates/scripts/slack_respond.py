#!/usr/bin/env python3
"""
Slack Respond - Send kanban agent responses back to Slack.

This script reads completed kanban tasks and sends their agent responses
back to Slack as threaded replies to the original messages.

Features:
- Matches kanban tasks with original Slack messages
- Sends responses as threaded replies
- Tracks sent responses to avoid duplicates
- Supports filtering by tag (e.g., slack-input)

Usage:
    python slack_respond.py                          # Process all tasks with responses
    python slack_respond.py --tag slack-input        # Only slack-input tagged tasks
    python slack_respond.py --dry-run --verbose      # Test mode

Environment Variables:
    SLACK_BOT_TOKEN         Slack bot token (required, starts with xoxb-)
    LOG_LEVEL               DEBUG, INFO, WARNING, ERROR (default: INFO)
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    from dotenv import load_dotenv
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError as e:
    print(f"Error: Missing required dependencies: {e}")
    print("Please run: pip install slack_sdk python-dotenv")
    sys.exit(1)

# Import local state managers
try:
    from slack_state import SlackStateManager, ResponseStateManager
except ImportError:
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from slack_state import SlackStateManager, ResponseStateManager


# Configure logging
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the application."""
    log_level = logging.DEBUG if verbose else logging.INFO

    env_log_level = os.getenv('LOG_LEVEL', '').upper()
    if env_log_level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        log_level = getattr(logging, env_log_level)

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def get_kanban_tasks(
    kanban_script: str,
    tag: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 10000
) -> List[Dict[str, Any]]:
    """
    Get kanban tasks from the kanban.sh script.

    Args:
        kanban_script: Path to kanban.sh script
        tag: Optional tag to filter by
        status: Optional status to filter by
        limit: Maximum number of tasks to retrieve (default: 10000 to ensure all tasks)

    Returns:
        List of task dicts
    """
    cmd = [kanban_script, 'list', '--limit', str(limit)]

    if tag:
        cmd.extend(['--tag', tag])
    if status:
        cmd.extend(['--status', status])

    logger.debug(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            logger.error(f"Kanban command failed: {result.stderr}")
            return []

        try:
            tasks = json.loads(result.stdout)
            if isinstance(tasks, list):
                return tasks
            logger.warning(f"Unexpected kanban output format: {type(tasks)}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse kanban output: {e}")
            return []

    except subprocess.TimeoutExpired:
        logger.error("Kanban command timed out")
        return []
    except Exception as e:
        logger.error(f"Error running kanban command: {e}")
        return []


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison by removing Slack-specific formatting.

    Handles:
    - Slack link formatting: <url|label> -> label, <url> -> url
    - Slack user/channel mentions: <@U123> -> @user, <#C123|channel> -> #channel
    - Markdown formatting: *bold*, _italic_, ~strike~, `code`
    - Multiple whitespace: collapses to single space
    - JSON-like content: normalizes quotes and escapes
    - Trailing/leading whitespace

    Args:
        text: The text to normalize

    Returns:
        Normalized text suitable for comparison
    """
    if not text:
        return ''

    # Remove Slack link formatting: <url|label> -> label
    text = re.sub(r'<([^|>]+)\|([^>]+)>', r'\2', text)
    # Remove bare Slack links: <url> -> url (remove angle brackets)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    # Remove mailto links: <mailto:email|email> -> email
    text = re.sub(r'<mailto:([^|>]+)\|([^>]+)>', r'\2', text)
    text = re.sub(r'<mailto:([^>]+)>', r'\1', text)

    # Remove Slack user mentions: <@U123ABC> -> @user
    text = re.sub(r'<@[A-Z0-9]+>', '@user', text)
    # Remove Slack channel mentions: <#C123|channel> -> #channel
    text = re.sub(r'<#[A-Z0-9]+\|([^>]+)>', r'#\1', text)
    text = re.sub(r'<#[A-Z0-9]+>', '#channel', text)

    # Remove Slack markdown: *bold* -> bold, _italic_ -> italic
    # Note: We don't remove backticks from code as they affect meaning
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    text = re.sub(r'~([^~]+)~', r'\1', text)

    # Normalize JSON-like content: handle escaped quotes
    # This helps match when JSON is stored differently
    text = text.replace('\\"', '"')
    text = text.replace("\\'", "'")

    # Normalize whitespace: collapse multiple spaces/newlines to single space
    text = re.sub(r'\s+', ' ', text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text


def compute_text_similarity(text1: str, text2: str) -> float:
    """
    Compute similarity ratio between two texts.

    Uses a simple character-based comparison that works well for
    detecting if two texts are essentially the same with minor
    formatting differences.

    Args:
        text1: First text
        text2: Second text

    Returns:
        Similarity ratio between 0.0 and 1.0
    """
    if not text1 or not text2:
        return 0.0

    if text1 == text2:
        return 1.0

    # Use difflib for sequence matching
    from difflib import SequenceMatcher
    return SequenceMatcher(None, text1, text2).ratio()


def find_matching_message(
    task: Dict[str, Any],
    slack_state: SlackStateManager,
    similarity_threshold: float = 0.85
) -> Optional[Dict[str, Any]]:
    """
    Find the Slack message that corresponds to a kanban task.

    Uses multiple strategies with fallback:
    1. Look up by task_id in state (preferred, exact match)
    2. Exact text match (fast path)
    3. Normalized text match (handles formatting differences)
    4. Prefix match with normalization
    5. Fuzzy match with similarity threshold (catches minor edits)

    Args:
        task: Kanban task dict
        slack_state: SlackStateManager with processed messages
        similarity_threshold: Minimum similarity ratio for fuzzy matching (0.0-1.0)

    Returns:
        Message data dict or None if not found
    """
    task_id = task.get('id')
    task_body = task.get('body', '')

    if not task_body:
        logger.debug(f"Task {task_id} has empty body, skipping text match")
        # Only try task_id lookup for empty body tasks
        message = slack_state.get_message_for_task(task_id)
        if message:
            logger.debug(f"Found message for task {task_id} by task_id lookup (empty body)")
            return message
        return None

    # Strategy 1: Look up by task_id (most reliable)
    message = slack_state.get_message_for_task(task_id)
    if message:
        logger.debug(f"Found message for task {task_id} by task_id lookup")
        return message

    # Pre-compute normalized task body for comparison strategies
    normalized_task_body = normalize_text(task_body)
    normalized_task_body_short = normalized_task_body[:500] if len(normalized_task_body) > 500 else normalized_task_body

    # Track best fuzzy match for Strategy 5
    best_match: Optional[Dict[str, Any]] = None
    best_similarity: float = 0.0

    for msg in slack_state.messages:
        msg_text = msg.get('text', '')
        if not msg_text:
            continue

        # Strategy 2: Exact text match (fast path)
        if msg_text == task_body:
            logger.debug(f"Found message for task {task_id} by exact text match")
            return msg

        # Strategy 3: Normalized text match
        normalized_msg_text = normalize_text(msg_text)

        if normalized_msg_text == normalized_task_body:
            logger.debug(f"Found message for task {task_id} by normalized text match")
            return msg

        # Strategy 4: Prefix match with normalization
        # Check if task body starts with message text (agent may have appended)
        if normalized_task_body.startswith(normalized_msg_text):
            logger.debug(f"Found message for task {task_id} by normalized prefix match (task starts with msg)")
            return msg

        # Check if message text starts with task body (message may be longer)
        normalized_msg_short = normalized_msg_text[:500] if len(normalized_msg_text) > 500 else normalized_msg_text
        if normalized_msg_text.startswith(normalized_task_body_short):
            logger.debug(f"Found message for task {task_id} by normalized prefix match (msg starts with task)")
            return msg

        # Strategy 5: Fuzzy match - compute similarity for later
        # Only compute if texts are reasonably close in length (within 2x)
        len_ratio = len(normalized_msg_text) / max(len(normalized_task_body), 1)
        if 0.5 <= len_ratio <= 2.0:
            similarity = compute_text_similarity(normalized_msg_text, normalized_task_body)
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = msg

    # Strategy 5: Return best fuzzy match if above threshold
    if best_match and best_similarity >= similarity_threshold:
        logger.debug(
            f"Found message for task {task_id} by fuzzy match "
            f"(similarity={best_similarity:.2f}, threshold={similarity_threshold})"
        )
        return best_match

    logger.debug(f"No matching message found for task {task_id}")
    if best_match:
        logger.debug(f"  Best fuzzy match had similarity={best_similarity:.2f}, below threshold={similarity_threshold}")
    return None


def send_slack_response(
    client: WebClient,
    channel_id: str,
    thread_ts: str,
    task_id: str,
    response_text: str,
    dry_run: bool = False
) -> Optional[str]:
    """
    Send a response to Slack as a threaded reply.

    Args:
        client: Slack WebClient instance
        channel_id: Slack channel ID
        thread_ts: Thread timestamp to reply to
        task_id: Kanban task ID (for prefixing)
        response_text: The response text to send
        dry_run: If True, don't actually send

    Returns:
        Response message timestamp if sent, None if failed
    """
    # Format the response with task ID
    formatted_response = f"**[task_id]{task_id}[/task_id]**\n\n{response_text}"

    if dry_run:
        logger.info(f"[DRY RUN] Would send to channel {channel_id}, thread {thread_ts}:")
        logger.info(f"[DRY RUN] {formatted_response[:200]}...")
        return "dry-run-ts"

    try:
        result = client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=formatted_response
        )

        if result.get('ok'):
            response_ts = result.get('ts')
            logger.info(f"Sent response for task {task_id} (ts: {response_ts})")
            return response_ts
        else:
            logger.error(f"Slack API returned ok=False: {result}")
            return None

    except SlackApiError as e:
        error = e.response.get('error', 'unknown_error')
        logger.error(f"Slack API error: {error}")

        if error == 'ratelimited':
            retry_after = int(e.response.headers.get('Retry-After', 60))
            logger.warning(f"Rate limited, should retry after {retry_after} seconds")
        elif error == 'channel_not_found':
            logger.error("Channel not found - check channel configuration")
        elif error == 'invalid_auth':
            logger.error("Invalid auth token - check SLACK_BOT_TOKEN")

        return None


def find_kanban_script(project_dir: Path) -> Optional[str]:
    """Find the kanban.sh script in the project."""
    candidates = [
        project_dir / '.juno_task' / 'scripts' / 'kanban.sh',
        project_dir / 'scripts' / 'kanban.sh',
    ]

    for path in candidates:
        if path.exists():
            return str(path)

    logger.error("Could not find kanban.sh script")
    return None


SLACK_TOKEN_DOCS_URL = "https://api.slack.com/tutorials/tracks/getting-a-token"


def validate_slack_environment() -> tuple[Optional[str], list[str]]:
    """
    Validate Slack environment variables are properly configured.

    Checks for SLACK_BOT_TOKEN in environment or .env file.
    The token should start with 'xoxb-' for bot tokens.

    Returns:
        Tuple of (bot_token, errors)
        - bot_token: The Slack bot token if found, None otherwise
        - errors: List of error messages if validation failed
    """
    errors = []

    # Check for bot token
    bot_token = os.getenv('SLACK_BOT_TOKEN')
    if not bot_token:
        errors.append(
            "SLACK_BOT_TOKEN not found.\n"
            "  Set it via environment variable or in a .env file:\n"
            "    export SLACK_BOT_TOKEN=xoxb-your-token-here\n"
            "  Or add to .env file:\n"
            "    SLACK_BOT_TOKEN=xoxb-your-token-here\n"
            f"\n  To generate a Slack bot token, visit:\n"
            f"    {SLACK_TOKEN_DOCS_URL}\n"
            "\n  Required OAuth scopes for bot token:\n"
            "    - channels:history (read messages from public channels)\n"
            "    - channels:read (list public channels)\n"
            "    - groups:history (read messages from private channels)\n"
            "    - groups:read (list private channels)\n"
            "    - users:read (get user info for message authors)\n"
            "    - chat:write (send messages as the bot)"
        )
    elif not bot_token.startswith('xoxb-'):
        errors.append(
            f"SLACK_BOT_TOKEN appears invalid (should start with 'xoxb-').\n"
            f"  Current value starts with: {bot_token[:10]}...\n"
            f"  Bot tokens from Slack start with 'xoxb-'.\n"
            f"  To generate a valid bot token, visit:\n"
            f"    {SLACK_TOKEN_DOCS_URL}"
        )

    return bot_token, errors


def print_env_help() -> None:
    """Print help message about configuring Slack environment variables."""
    print("\n" + "=" * 70)
    print("Slack Integration - Environment Configuration")
    print("=" * 70)
    print("""
Required Environment Variables:
  SLACK_BOT_TOKEN       Your Slack bot token (starts with xoxb-)

Optional Environment Variables:
  LOG_LEVEL             DEBUG, INFO, WARNING, ERROR (default: INFO)

Configuration Methods:
  1. Environment variables:
     export SLACK_BOT_TOKEN=xoxb-your-token-here

  2. .env file (in project root):
     SLACK_BOT_TOKEN=xoxb-your-token-here

Generating a Slack Bot Token:
  1. Go to https://api.slack.com/apps and create a new app
  2. Under "OAuth & Permissions", add the required scopes:
     - channels:history, channels:read (public channels)
     - groups:history, groups:read (private channels)
     - users:read (user info)
     - chat:write (send messages)
  3. Install the app to your workspace
  4. Copy the "Bot User OAuth Token" (starts with xoxb-)

  Full tutorial: """ + SLACK_TOKEN_DOCS_URL + """

Example .env file:
  SLACK_BOT_TOKEN=xoxb-YOUR-BOT-TOKEN-HERE
  LOG_LEVEL=INFO
""")
    print("=" * 70 + "\n")


def main_loop(args: argparse.Namespace) -> int:
    """Main processing loop."""
    # Load environment variables from .env file
    # load_dotenv() looks for .env in current directory and parent directories
    load_dotenv()

    # Also try loading from project root .env if running from a subdirectory
    project_root = Path.cwd()
    env_file = project_root / '.env'
    if env_file.exists():
        load_dotenv(env_file)

    # Also check .juno_task/.env for project-specific config
    juno_env_file = project_root / '.juno_task' / '.env'
    if juno_env_file.exists():
        load_dotenv(juno_env_file)

    # Setup logging
    setup_logging(verbose=args.verbose)

    logger.info("=" * 70)
    logger.info("Slack Respond - Sending agent responses to Slack")
    logger.info("=" * 70)

    # Validate environment
    bot_token, errors = validate_slack_environment()

    if errors:
        for error in errors:
            logger.error(error)
        print_env_help()
        return 1

    # Find project root and kanban script
    project_dir = Path.cwd()
    kanban_script = find_kanban_script(project_dir)
    if not kanban_script:
        logger.error("Cannot find kanban.sh script. Is the project initialized?")
        return 1

    # Initialize Slack client
    logger.info("Initializing Slack client...")
    client = WebClient(token=bot_token)

    # Test connection
    try:
        auth_response = client.auth_test()
        team_name = auth_response.get('team')
        logger.info(f"Connected to Slack workspace: {team_name}")
    except SlackApiError as e:
        logger.error(f"Failed to connect to Slack: {e.response['error']}")
        return 1

    # Initialize state managers
    state_dir = project_dir / '.juno_task' / 'slack'
    slack_state_file = state_dir / 'slack.ndjson'
    response_state_file = state_dir / 'responses_sent.ndjson'

    logger.info(f"Loading Slack message state: {slack_state_file}")
    slack_state = SlackStateManager(str(slack_state_file))

    logger.info(f"Loading response state: {response_state_file}")
    response_state = ResponseStateManager(str(response_state_file))

    if args.reset_tracker:
        confirm = input("WARNING: This will reset the response tracker. Type 'yes' to confirm: ")
        if confirm.lower() == 'yes':
            response_state.reset_state()
            logger.info("Response tracker reset")
        else:
            logger.info("Reset cancelled")
        return 0

    if args.dry_run:
        logger.info("Running in DRY RUN mode - no responses will be sent")

    logger.info(f"Slack messages loaded: {slack_state.get_message_count()}")
    logger.info(f"Responses already sent: {response_state.get_sent_count()}")
    logger.info("-" * 70)

    # Get kanban tasks
    tasks = get_kanban_tasks(kanban_script, tag=args.tag)
    logger.info(f"Found {len(tasks)} kanban tasks")

    # Process tasks
    total_tasks = 0
    matched_tasks = 0
    sent_responses = 0
    already_sent = 0
    errors = 0

    for task in tasks:
        task_id = task.get('id')
        task_body = task.get('body', '')
        agent_response = task.get('agent_response', '')

        total_tasks += 1

        # Skip if no response
        if not agent_response or agent_response == 'null':
            logger.debug(f"Task {task_id}: No agent response, skipping")
            continue

        # Find matching Slack message
        message = find_matching_message(task, slack_state)
        if not message:
            logger.debug(f"Task {task_id}: No matching Slack message found")
            continue

        matched_tasks += 1

        message_ts = message.get('ts')
        thread_ts = message.get('thread_ts', message_ts)
        channel_id = message.get('channel_id')
        author = message.get('author_name', 'unknown')

        logger.debug(f"Task {task_id}: Found match - ts={message_ts}, author={author}")

        # Check if already sent
        if response_state.was_response_sent(task_id, message_ts):
            logger.info(f"Task {task_id}: Already sent response to {author} (skipping)")
            already_sent += 1
            continue

        # Send response
        logger.info(f"Task {task_id}: Sending response to {author}")

        response_ts = send_slack_response(
            client,
            channel_id,
            thread_ts,
            task_id,
            agent_response,
            dry_run=args.dry_run
        )

        if response_ts:
            sent_responses += 1

            # Record sent
            if not args.dry_run:
                response_state.record_sent(task_id, message_ts, channel_id, response_ts)

            logger.info(f"  ✓ Response sent successfully")
        else:
            errors += 1
            logger.error(f"  ✗ Failed to send response")

    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("Summary:")
    logger.info(f"  Total tasks processed: {total_tasks}")
    logger.info(f"  Tasks matched with Slack messages: {matched_tasks}")
    logger.info(f"  Responses sent: {sent_responses}")
    logger.info(f"  Already sent (skipped): {already_sent}")
    if errors > 0:
        logger.error(f"  Errors: {errors}")

    if args.dry_run:
        logger.info("(Dry run mode - no messages were actually sent)")

    return 0 if errors == 0 else 1


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Send kanban agent responses back to Slack',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Process all tasks
  %(prog)s --tag slack-input            # Only slack-input tagged tasks
  %(prog)s --dry-run --verbose          # Test mode
  %(prog)s --reset-tracker              # Reset sent tracker (WARNING: re-sends all)

Environment Variables:
  SLACK_BOT_TOKEN          Slack bot token (required)
  LOG_LEVEL               DEBUG, INFO, WARNING, ERROR (default: INFO)

Notes:
  - Only sends responses for tasks with non-empty agent_response
  - Matches tasks to Slack messages by task_id or body text
  - Responses are prefixed with "**[task_id]{id}[/task_id]**"
  - Tracks sent responses in .juno_task/slack/responses_sent.ndjson
        """
    )

    parser.add_argument(
        '--tag',
        help='Filter tasks by tag (e.g., slack-input)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be sent without actually sending'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable DEBUG level logging'
    )

    parser.add_argument(
        '--reset-tracker',
        action='store_true',
        help='Reset response tracker (WARNING: will re-send all responses)'
    )

    args = parser.parse_args()

    try:
        return main_loop(args)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
