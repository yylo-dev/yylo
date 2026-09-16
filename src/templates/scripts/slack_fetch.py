#!/usr/bin/env python3
"""
Slack Fetch - Fetch messages from Slack and create kanban tasks.

This script monitors a Slack channel for new messages and creates
kanban tasks from them. It uses persistent state tracking to avoid
processing the same message twice.

Features:
- Channel monitoring with configurable intervals
- Default --once mode for cron/scheduled jobs
- Persistent state tracking (no duplicate processing)
- Automatic kanban task creation with slack-input tag
- File attachment downloading (saves to .juno_task/attachments/slack/)
- Graceful shutdown on SIGINT/SIGTERM

Usage:
    python slack_fetch.py --channel bug-reports --once
    python slack_fetch.py --channel feature-requests --continuous
    python slack_fetch.py --channel general --dry-run --verbose
    python slack_fetch.py --channel uploads --download-attachments

Environment Variables:
    SLACK_BOT_TOKEN             Slack bot token (required, starts with xoxb-)
    SLACK_CHANNEL               Default channel to monitor
    CHECK_INTERVAL_SECONDS      Polling interval in seconds (default: 60)
    LOG_LEVEL                   DEBUG, INFO, WARNING, ERROR (default: INFO)
    JUNO_DOWNLOAD_ATTACHMENTS   Enable/disable file downloads (default: true)
    JUNO_MAX_ATTACHMENT_SIZE    Max file size in bytes (default: 50MB)
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

try:
    from dotenv import load_dotenv
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError as e:
    print(f"Error: Missing required dependencies: {e}")
    print("Please run: pip install slack_sdk python-dotenv")
    sys.exit(1)

# Import local modules
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

try:
    from slack_state import SlackStateManager
except ImportError:
    # Fallback: try importing from same directory
    from slack_state import SlackStateManager

# Import attachment downloader for file handling
try:
    from attachment_downloader import (
        AttachmentDownloader,
        format_attachments_section,
        is_attachments_enabled
    )
    ATTACHMENTS_AVAILABLE = True
except ImportError:
    ATTACHMENTS_AVAILABLE = False
    # Define stub functions if attachment_downloader not available
    def is_attachments_enabled():
        return False
    def format_attachments_section(paths):
        return ""


# Global shutdown flag
shutdown_requested = False

# Configure logging
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
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

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
        )
        logging.getLogger().addHandler(file_handler)


def signal_handler(signum: int, frame) -> None:
    """Handle shutdown signals gracefully."""
    global shutdown_requested
    signal_name = signal.Signals(signum).name
    logger.info(f"Received {signal_name}, initiating graceful shutdown...")
    shutdown_requested = True


def get_channel_id(client: WebClient, channel_name: str) -> Optional[str]:
    """
    Resolve channel name to channel ID.

    Args:
        client: Slack WebClient instance
        channel_name: Channel name (with or without #) or channel ID

    Returns:
        Channel ID or None if not found
    """
    # If already looks like a channel ID, return as-is
    if channel_name.startswith('C') and len(channel_name) >= 9:
        logger.debug(f"Channel '{channel_name}' appears to be a channel ID")
        return channel_name

    # Strip # prefix if present
    channel_name = channel_name.lstrip('#')
    logger.info(f"Resolving channel name '{channel_name}' to ID...")

    try:
        # Try public channels
        result = client.conversations_list(types="public_channel")
        for channel in result.get('channels', []):
            if channel.get('name') == channel_name:
                channel_id = channel.get('id')
                logger.info(f"Found channel '{channel_name}' with ID: {channel_id}")
                return channel_id

        # Try private channels
        result = client.conversations_list(types="private_channel")
        for channel in result.get('channels', []):
            if channel.get('name') == channel_name:
                channel_id = channel.get('id')
                logger.info(f"Found private channel '{channel_name}' with ID: {channel_id}")
                return channel_id

        logger.error(f"Channel '{channel_name}' not found")
        return None

    except SlackApiError as e:
        logger.error(f"Error resolving channel name: {e.response['error']}")
        return None


def get_user_info(client: WebClient, user_id: str) -> str:
    """Get user display name from user ID."""
    try:
        result = client.users_info(user=user_id)
        user = result.get('user', {})
        profile = user.get('profile', {})
        display_name = (
            profile.get('display_name') or
            profile.get('real_name') or
            user.get('name') or
            user_id
        )
        return display_name
    except SlackApiError as e:
        logger.warning(f"Could not fetch user info for {user_id}: {e.response['error']}")
        return user_id


def fetch_channel_messages(
    client: WebClient,
    channel_id: str,
    oldest_ts: Optional[str] = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Fetch messages from a Slack channel.

    Args:
        client: Slack WebClient instance
        channel_id: Slack channel ID
        oldest_ts: Optional timestamp to fetch messages after
        limit: Maximum number of messages to fetch

    Returns:
        List of message dicts
    """
    logger.debug(f"Fetching messages from channel {channel_id} (oldest_ts={oldest_ts})")

    try:
        kwargs = {'channel': channel_id, 'limit': limit}
        if oldest_ts:
            kwargs['oldest'] = oldest_ts

        result = client.conversations_history(**kwargs)
        messages = result.get('messages', [])

        # Filter to only user messages (exclude bot messages, system messages)
        messages = [
            m for m in messages
            if m.get('type') == 'message' and not m.get('subtype')
        ]

        logger.debug(f"Fetched {len(messages)} messages")
        return messages

    except SlackApiError as e:
        error_msg = e.response.get('error', 'unknown_error')
        logger.error(f"Error fetching messages: {error_msg}")

        if error_msg == 'ratelimited':
            retry_after = int(e.response.headers.get('Retry-After', 60))
            logger.warning(f"Rate limited, should retry after {retry_after} seconds")

        return []


def sanitize_tag(tag: str) -> str:
    """
    Sanitize a tag to be compatible with kanban validation.

    Kanban tags only allow: letters, numbers, underscores (_), and hyphens (-).
    This function replaces colons and other special characters with underscores.

    Args:
        tag: The raw tag string

    Returns:
        Sanitized tag compatible with kanban system
    """
    import re
    # Replace spaces and colons with underscores
    tag = tag.replace(' ', '_').replace(':', '_')
    # Remove any remaining invalid characters (keep only alphanumeric, _, -)
    tag = re.sub(r'[^a-zA-Z0-9_-]', '_', tag)
    # Collapse multiple underscores
    tag = re.sub(r'_+', '_', tag)
    # Remove leading/trailing underscores
    tag = tag.strip('_')
    return tag


# =============================================================================
# File Attachment Handling
# =============================================================================

def extract_files_from_message(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract file attachment information from a Slack message.

    Args:
        message: Message dict from conversations.history API

    Returns:
        List of file info dicts with keys: id, name, url_private_download, size, mimetype
    """
    files = message.get('files', [])
    if not files:
        return []

    extracted = []
    for file_info in files:
        # Skip external file links (not uploaded to Slack)
        if file_info.get('mode') == 'external':
            logger.debug(f"Skipping external file: {file_info.get('name')}")
            continue

        # Skip files that are tombstoned (deleted)
        if file_info.get('mode') == 'tombstone':
            logger.debug(f"Skipping deleted file: {file_info.get('id')}")
            continue

        extracted.append({
            'id': file_info.get('id'),
            'name': file_info.get('name', f"file_{file_info.get('id')}"),
            'url_private_download': file_info.get('url_private_download'),
            'url_private': file_info.get('url_private'),
            'size': file_info.get('size', 0),
            'mimetype': file_info.get('mimetype', 'application/octet-stream'),
            'filetype': file_info.get('filetype', 'unknown'),
            'title': file_info.get('title', '')
        })

    return extracted


def download_message_files(
    files: List[Dict[str, Any]],
    bot_token: str,
    channel_id: str,
    message_ts: str,
    downloader: 'AttachmentDownloader'
) -> List[str]:
    """
    Download all files from a Slack message.

    Args:
        files: List of file info dicts from extract_files_from_message()
        bot_token: Slack bot token for authorization
        channel_id: Channel ID for directory organization
        message_ts: Message timestamp for filename prefix
        downloader: AttachmentDownloader instance

    Returns:
        List of local file paths (empty for failures)
    """
    if not files:
        return []

    downloaded_paths = []
    headers = {'Authorization': f'Bearer {bot_token}'}

    # Sanitize message_ts for use in filename (remove dots)
    ts_prefix = message_ts.replace('.', '_')

    target_dir = downloader.base_dir / 'slack' / channel_id

    for file_info in files:
        # Prefer url_private_download, fallback to url_private
        url = file_info.get('url_private_download') or file_info.get('url_private')
        if not url:
            logger.warning(f"No download URL for file {file_info.get('id')}")
            continue

        original_filename = file_info.get('name', 'unnamed')

        metadata = {
            'source': 'slack',
            'source_id': file_info.get('id'),
            'message_ts': message_ts,
            'channel_id': channel_id,
            'mime_type': file_info.get('mimetype'),
            'filetype': file_info.get('filetype'),
            'title': file_info.get('title', ''),
            'original_size': file_info.get('size', 0)
        }

        path, error = downloader.download_file(
            url=url,
            target_dir=target_dir,
            filename_prefix=ts_prefix,
            original_filename=original_filename,
            headers=headers,
            metadata=metadata
        )

        if path:
            downloaded_paths.append(path)
            logger.info(f"Downloaded Slack file: {original_filename}")
        else:
            logger.warning(f"Failed to download {original_filename}: {error}")

    return downloaded_paths


def create_kanban_task(
    message_text: str,
    author_name: str,
    tags: List[str],
    kanban_script: str,
    dry_run: bool = False
) -> Optional[str]:
    """
    Create a kanban task from a Slack message.

    Args:
        message_text: The message content
        author_name: The author's display name
        tags: List of tags to apply
        kanban_script: Path to kanban.sh script
        dry_run: If True, don't actually create the task

    Returns:
        Task ID if created, None if failed or dry run
    """
    # Build the kanban command
    # Escape single quotes in message text
    escaped_text = message_text.replace("'", "'\\''")

    # Build tags argument
    tags_arg = ','.join(tags) if tags else 'slack-input'

    if dry_run:
        logger.info(f"[DRY RUN] Would create task: {message_text[:100]}...")
        logger.debug(f"[DRY RUN] Tags: {tags_arg}")
        return "dry-run-task-id"

    try:
        # Run kanban create command
        cmd = [kanban_script, 'create', message_text, '--tags', tags_arg]
        logger.debug(f"Running: {' '.join(cmd[:3])}...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            # Parse output to get task ID
            try:
                output = json.loads(result.stdout)
                if isinstance(output, list) and len(output) > 0:
                    task_id = output[0].get('id')
                    logger.info(f"Created kanban task: {task_id}")
                    return task_id
            except json.JSONDecodeError:
                logger.warning(f"Could not parse kanban output: {result.stdout[:200]}")
                # Still return success if command succeeded
                return "unknown-task-id"
        else:
            logger.error(f"Failed to create kanban task: {result.stderr}")
            return None

    except subprocess.TimeoutExpired:
        logger.error("Kanban command timed out")
        return None
    except Exception as e:
        logger.error(f"Error creating kanban task: {e}")
        return None


def process_messages(
    messages: List[Dict[str, Any]],
    channel_name: str,
    channel_id: str,
    client: WebClient,
    state_mgr: SlackStateManager,
    kanban_script: str,
    dry_run: bool = False,
    bot_token: Optional[str] = None,
    downloader: Optional['AttachmentDownloader'] = None,
    download_attachments: bool = True
) -> int:
    """
    Process new messages: create kanban tasks and update state.

    Args:
        messages: List of message dicts
        channel_name: Channel name for logging
        channel_id: Channel ID
        client: Slack WebClient for user lookups
        state_mgr: SlackStateManager instance
        kanban_script: Path to kanban.sh script
        dry_run: If True, don't create tasks
        bot_token: Slack bot token for downloading attachments
        downloader: AttachmentDownloader instance for file downloads
        download_attachments: Whether to download file attachments

    Returns:
        Number of messages processed
    """
    if not messages:
        return 0

    logger.info(f"Processing {len(messages)} new messages from #{channel_name}")
    processed = 0

    # Sort messages oldest first
    messages.sort(key=lambda m: m.get('ts', '0'))

    for msg in messages:
        ts = msg.get('ts')
        user_id = msg.get('user', 'unknown')
        text = msg.get('text', '')

        # Check if message has files (allows processing messages with only attachments)
        has_files = bool(msg.get('files'))

        # Skip empty messages (unless they have files)
        if not text.strip() and not has_files:
            logger.debug(f"Skipping empty message ts={ts}")
            continue

        # Skip already processed
        if state_mgr.is_processed(ts):
            logger.debug(f"Skipping already processed message ts={ts}")
            continue

        # Get user info
        author_name = get_user_info(client, user_id)

        # Convert timestamp to ISO date
        try:
            date_str = datetime.fromtimestamp(float(ts)).isoformat()
        except (ValueError, TypeError):
            date_str = ts

        logger.info(f"New message from {author_name}: {text[:50]}{'...' if len(text) > 50 else ''}")

        # Handle file attachments
        attachment_paths = []
        if download_attachments and ATTACHMENTS_AVAILABLE and downloader and bot_token:
            files = extract_files_from_message(msg)
            if files:
                logger.info(f"Found {len(files)} file(s) attached to message")
                if not dry_run:
                    attachment_paths = download_message_files(
                        files=files,
                        bot_token=bot_token,
                        channel_id=channel_id,
                        message_ts=ts,
                        downloader=downloader
                    )
                    if attachment_paths:
                        logger.info(f"Downloaded {len(attachment_paths)} file(s)")
                else:
                    logger.info(f"[DRY RUN] Would download {len(files)} file(s)")

        # Build task text with attachment paths
        task_text = text
        if attachment_paths:
            task_text += format_attachments_section(attachment_paths)

        # Create kanban task
        # Create author tag with sanitization (colons not allowed in kanban tags)
        author_tag = sanitize_tag(f'author_{author_name}')
        tags = ['slack-input', author_tag]

        # Add has-attachments tag if files were downloaded
        if attachment_paths:
            tags.append('has-attachments')

        task_id = create_kanban_task(task_text, author_name, tags, kanban_script, dry_run)

        if task_id:
            # Record in state
            message_data = {
                'text': text,
                'author': user_id,
                'author_name': author_name,
                'date': date_str,
                'channel': channel_name,
                'channel_id': channel_id,
                'thread_ts': msg.get('thread_ts', ts),
                'attachment_count': len(attachment_paths),
                'attachment_paths': attachment_paths
            }

            if not dry_run:
                state_mgr.mark_processed(ts, task_id, message_data)

            processed += 1
        else:
            logger.warning(f"Failed to create task for message ts={ts}")

    return processed


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


def validate_slack_environment() -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Validate Slack environment variables are properly configured.

    Checks for SLACK_BOT_TOKEN in environment or .env file.
    The token should start with 'xoxb-' for bot tokens.

    Returns:
        Tuple of (bot_token, channel, errors)
        - bot_token: The Slack bot token if found, None otherwise
        - channel: The Slack channel if found, None otherwise
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
            "    - chat:write (optional, for slack_respond.py)"
        )
    elif not bot_token.startswith('xoxb-'):
        errors.append(
            f"SLACK_BOT_TOKEN appears invalid (should start with 'xoxb-').\n"
            f"  Current value starts with: {bot_token[:10]}...\n"
            f"  Bot tokens from Slack start with 'xoxb-'.\n"
            f"  To generate a valid bot token, visit:\n"
            f"    {SLACK_TOKEN_DOCS_URL}"
        )

    # Check for channel (optional at validation, but warn)
    channel = os.getenv('SLACK_CHANNEL')

    return bot_token, channel, errors


def print_env_help() -> None:
    """Print help message about configuring Slack environment variables."""
    print("\n" + "=" * 70)
    print("Slack Integration - Environment Configuration")
    print("=" * 70)
    print("""
Required Environment Variables:
  SLACK_BOT_TOKEN       Your Slack bot token (starts with xoxb-)

Optional Environment Variables:
  SLACK_CHANNEL         Default channel to monitor (can also use --channel flag)
  CHECK_INTERVAL_SECONDS  Polling interval in seconds (default: 60)
  LOG_LEVEL             DEBUG, INFO, WARNING, ERROR (default: INFO)

Configuration Methods:
  1. Environment variables:
     export SLACK_BOT_TOKEN=xoxb-your-token-here
     export SLACK_CHANNEL=bug-reports

  2. .env file (in project root):
     SLACK_BOT_TOKEN=xoxb-your-token-here
     SLACK_CHANNEL=bug-reports

Generating a Slack Bot Token:
  1. Go to https://api.slack.com/apps and create a new app
  2. Under "OAuth & Permissions", add the required scopes:
     - channels:history, channels:read (public channels)
     - groups:history, groups:read (private channels)
     - users:read (user info)
     - files:read (download file attachments)
     - chat:write (for slack_respond.py)
  3. Install the app to your workspace
  4. Copy the "Bot User OAuth Token" (starts with xoxb-)

  Full tutorial: """ + SLACK_TOKEN_DOCS_URL + """

Example .env file:
  SLACK_BOT_TOKEN=xoxb-YOUR-BOT-TOKEN-HERE
  SLACK_CHANNEL=bug-reports
  CHECK_INTERVAL_SECONDS=120
  LOG_LEVEL=INFO
""")
    print("=" * 70 + "\n")


def main_loop(args: argparse.Namespace) -> int:
    """Main monitoring loop."""
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
    logger.info("Slack Fetch - Creating kanban tasks from Slack messages")
    logger.info("=" * 70)

    # Validate environment
    bot_token, env_channel, errors = validate_slack_environment()

    if errors:
        for error in errors:
            logger.error(error)
        print_env_help()
        return 1

    # Get channel from args or env
    channel = args.channel or env_channel
    if not channel:
        logger.error("No channel specified. Use --channel or set SLACK_CHANNEL environment variable")
        print("\nHint: Set SLACK_CHANNEL in your .env file or pass --channel flag")
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

    # Resolve channel
    channel_id = get_channel_id(client, channel)
    if not channel_id:
        logger.error(f"Could not find channel '{channel}'. Make sure the bot is invited.")
        return 1

    # Initialize state manager
    state_dir = project_dir / '.juno_task' / 'slack'
    state_file = state_dir / 'slack.ndjson'
    logger.info(f"Initializing state manager: {state_file}")
    state_mgr = SlackStateManager(str(state_file))

    # Get check interval
    check_interval = int(os.getenv('CHECK_INTERVAL_SECONDS', 60))

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.dry_run:
        logger.info("Running in DRY RUN mode - no tasks will be created")

    # Initialize attachment downloader if enabled
    downloader = None
    download_attachments = getattr(args, 'download_attachments', True) and is_attachments_enabled()
    if download_attachments and ATTACHMENTS_AVAILABLE:
        attachments_dir = project_dir / '.juno_task' / 'attachments'
        downloader = AttachmentDownloader(base_dir=str(attachments_dir))
        logger.info(f"Attachment downloads enabled: {attachments_dir}")
    elif download_attachments and not ATTACHMENTS_AVAILABLE:
        logger.warning("Attachment downloads requested but attachment_downloader module not available")
        download_attachments = False

    logger.info(f"Monitoring channel #{channel} (ID: {channel_id})")
    logger.info(f"Check interval: {check_interval} seconds")
    logger.info(f"Mode: {'once' if args.once else 'continuous'}")
    logger.info(f"Download attachments: {download_attachments}")
    logger.info("-" * 70)

    # Main loop
    iteration = 0
    total_processed = 0

    while not shutdown_requested:
        iteration += 1
        logger.debug(f"Starting iteration {iteration}")

        try:
            # Get last timestamp
            last_ts = state_mgr.get_last_timestamp()

            # Fetch new messages
            messages = fetch_channel_messages(client, channel_id, oldest_ts=last_ts)

            # Filter out already processed
            new_messages = [m for m in messages if not state_mgr.is_processed(m.get('ts', ''))]

            if new_messages:
                processed = process_messages(
                    new_messages,
                    channel,
                    channel_id,
                    client,
                    state_mgr,
                    kanban_script,
                    dry_run=args.dry_run,
                    bot_token=bot_token,
                    downloader=downloader,
                    download_attachments=download_attachments
                )
                total_processed += processed
                logger.info(f"Processed {processed} messages (total: {total_processed})")
            else:
                logger.debug("No new messages")

            # Exit if --once mode
            if args.once:
                logger.info("--once mode: exiting after single check")
                break

            # Sleep
            if not shutdown_requested:
                logger.debug(f"Sleeping for {check_interval} seconds...")
                time.sleep(check_interval)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            if args.once:
                return 1
            time.sleep(check_interval)

    # Shutdown
    logger.info("-" * 70)
    logger.info(f"Shutting down. Created {total_processed} kanban tasks.")
    logger.info(f"Total processed messages: {state_mgr.get_message_count()}")

    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Fetch Slack messages and create kanban tasks',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --channel bug-reports                    # Run once (default)
  %(prog)s --channel feature-requests --continuous  # Continuous monitoring
  %(prog)s --channel general --dry-run --verbose    # Test mode
  %(prog)s --channel uploads --download-attachments # Explicit attachment download

Environment Variables:
  SLACK_BOT_TOKEN              Slack bot token (required)
  SLACK_CHANNEL                Default channel to monitor
  CHECK_INTERVAL_SECONDS       Polling interval (default: 60)
  LOG_LEVEL                    DEBUG, INFO, WARNING, ERROR (default: INFO)
  JUNO_DOWNLOAD_ATTACHMENTS    Enable/disable file downloads (default: true)
  JUNO_MAX_ATTACHMENT_SIZE     Max file size in bytes (default: 50MB)

Notes:
  - Messages are tagged with 'slack-input' and 'author_<name>'
  - Messages with attachments also get 'has-attachments' tag
  - State is persisted to .juno_task/slack/slack.ndjson
  - Attachments saved to .juno_task/attachments/slack/<channel_id>/
  - Use Ctrl+C for graceful shutdown
  - Required OAuth scope for file downloads: files:read
        """
    )

    parser.add_argument(
        '--channel',
        help='Slack channel name to monitor (with or without #)'
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--once',
        dest='once',
        action='store_true',
        default=True,
        help='Run once and exit (DEFAULT)'
    )
    mode_group.add_argument(
        '--continuous',
        dest='once',
        action='store_false',
        help='Run continuously with polling'
    )

    # Attachment handling options
    attachment_group = parser.add_mutually_exclusive_group()
    attachment_group.add_argument(
        '--download-attachments',
        dest='download_attachments',
        action='store_true',
        default=True,
        help='Download file attachments from messages (DEFAULT)'
    )
    attachment_group.add_argument(
        '--no-attachments',
        dest='download_attachments',
        action='store_false',
        help='Skip downloading file attachments'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without creating tasks'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable DEBUG level logging'
    )

    args = parser.parse_args()

    try:
        return main_loop(args)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
