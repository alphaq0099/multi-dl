"""Shared transfer stages for terminal and desktop progress adapters."""
import re

PHASE_LABELS = {
    'waiting': 'Waiting', 'resolving': 'Fetching metadata',
    'downloading': 'Downloading', 'processing': 'Processing',
}


def infer_phase(message=None, downloaded=None, speed=None, previous='waiting'):
    message = (message or '').lower()
    if any(word in message for word in ('ffmpeg', 'processing', 'merging', 'finalizing', 'postprocess', 'saving history')):
        return 'processing'
    if any(word in message for word in ('waiting', 'starting gallery worker', 'retry', 'warning:', 'backoff')):
        return 'waiting'
    if any(word in message for word in ('metadata', 'extractor:', 'po-token', 'player:', 'streams:', 'auth:', 'connecting', 'resolving', 'fetching next')):
        return 'resolving'
    if (downloaded or 0) > 0 or (speed or 0) > 0:
        return 'downloading'
    return previous


def plain_text(value):
    """Remote titles/statuses must not inject terminal control sequences."""
    value = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', str(value or ''))
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(char if ord(char) >= 32 and ord(char) != 127 else ' ' for char in value)
