"""Last line of defence for what the bot sends to Slack.

A model that has been shown a credential (in a file it read, a log, a tool result) can
repeat it in a message. The prompt says not to; this makes sure. Every string argument
of every MCP tool call passes through redact() before the tool runs, so a token-shaped
string is replaced before it can reach chat.postMessage, a file upload, a channel topic
or anything else. The tool result then carries `redacted` (the kinds found, never the
values), so the caller knows its text was changed.

It matches shapes, not meaning: tokens with a known prefix, private key blocks, bearer
headers. A password in plain words passes. REDACT_EXTRA_REGEX adds one pattern of your
own; REDACT_OUTGOING=0 turns the filter off.

Try it:  echo 'token xoxb-1234567890-abcdefghij' | python3 redact.py
"""
import base64
import binascii
import logging
import os
import re
import sys

log = logging.getLogger("redact")

ENABLED = os.environ.get("REDACT_OUTGOING", "1") != "0"

SHAPES = [
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)),
    ("Slack token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}")),
    ("Slack app token", re.compile(r"\bxapp-[0-9]-[A-Za-z0-9-]{10,}")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/(?:services|triggers)/[A-Za-z0-9/_-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS secret key", re.compile(r"(?i)\baws_secret_access_key\b[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{30,}")),
    ("API key", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{32,}|xai-[A-Za-z0-9]{40,}|AIza[0-9A-Za-z_-]{35})")),
    ("Tailscale key", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9]{6,}(?:-[A-Za-z0-9]+)?")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}=*")),
]
_extra = os.environ.get("REDACT_EXTRA_REGEX", "")
if _extra:
    try:
        SHAPES.append(("configured pattern", re.compile(_extra)))
    except re.error as e:
        log.warning("REDACT_EXTRA_REGEX is not a valid pattern and is ignored: %s", e)

NOTE = ("Part of what you sent looked like a credential and was replaced with [redacted: <kind>] before it reached Slack. "
        "Do not try to send it another way.")


def redact(text):
    """(text with every secret-shaped part replaced, the kinds that were found)."""
    found = []
    for kind, pattern in SHAPES:
        text, n = pattern.subn(f"[redacted: {kind}]", text)
        if n:
            found.append(kind)
    return text, found


def _walk(value, found):
    if isinstance(value, str):
        value, kinds = redact(value)
        found.extend(kinds)
        return value
    if isinstance(value, (list, tuple)):
        return type(value)(_walk(v, found) for v in value)
    if isinstance(value, dict):
        return {k: _walk(v, found) for k, v in value.items()}
    return value


def _walk_base64(value, found):
    """A base64 argument is file content. Text files are filtered like any other text; binary passes as it is."""
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return value
    clean, kinds = redact(text)
    if not kinds:
        return value
    found.extend(kinds)
    return base64.b64encode(clean.encode()).decode()


def scrub_call(args, kwargs):
    """The arguments of one tool call, filtered. Returns (args, kwargs, sorted kinds found)."""
    found = []
    args = tuple(_walk(a, found) for a in args)
    kwargs = {k: (_walk_base64(v, found) if k.endswith("_base64") and isinstance(v, str) else _walk(v, found)) for k, v in kwargs.items()}
    return args, kwargs, sorted(set(found))


if __name__ == "__main__":
    out, kinds = redact(sys.stdin.read())
    sys.stdout.write(out)
    if kinds:
        sys.stderr.write("redacted: " + ", ".join(kinds) + "\n")
