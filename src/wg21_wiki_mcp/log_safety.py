"""Centralized redaction for logs and auth error messages.

Guarantees that credential values and wiki page content never reach a log
record.  Register secrets at startup (see :func:`register_config_secrets`);
the :class:`LogSafetyFilter` attached in :mod:`wg21_wiki_mcp.log` scrubs every
record before it reaches a handler.

Error helpers (:func:`summarize_auth_failures`, :data:`AUTH_FAILURE_MESSAGE`)
ensure :class:`~wg21_wiki_mcp.errors.AuthError` messages never reproduce
upstream exception text that might carry credentials or page HTML.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

_REDACTED = "[REDACTED]"
_MIN_SECRET_LEN = 4

# Runtime registry of literal values that must never appear in logs.
_redactions: set[str] = set()

# Patterns that often precede credential material in exception or debug text.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(passwd\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(secret\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(authorization\s*:\s*).+"),
    re.compile(r"(?i)(apikey\s*[=:]\s*)\S+"),
)

AUTH_FAILURE_MESSAGE = (
    "Authentication failed for every configured credential path; verify wiki credentials in the server configuration."
)

# Fixed AuthError messages raised directly from wiki_client (no upstream text).
_KNOWN_SAFE_AUTH_MESSAGES = frozenset(
    {
        AUTH_FAILURE_MESSAGE,
        "SAML login completed but the API still sees an anonymous session.",
        "SAML IdP login form not found (page changed or extra step required).",
        "Could not locate username/password fields on the IdP form.",
        "SAML login failed (no SAMLResponse; check credentials/MFA).",
    }
)

# Static SAML heads that may be followed by validated "; url=...; status=...; fields=..." suffixes.
_SAML_STATIC_HEADS = (
    "SAML SSO entry point returned HTTP error.",
    "SAML IdP login form not found (page changed or extra step required).",
    "Could not locate username/password fields on the IdP form.",
    "SAML login failed (no SAMLResponse; check credentials/MFA).",
    "SAML ACS endpoint rejected the response.",
    "SAML IdP POST returned HTTP error.",
)

_SAML_REQUEST_FAILED_HEAD_RE = re.compile(r"^SAML SSO request failed: \w+\.$")
_SAFE_FIELDS_SUFFIX_RE = re.compile(r"^fields=\[(?:'[\w.-]+'(?:, '[\w.-]+')*)?\]$")

_SESSION_REAUTH_MESSAGE_RE = re.compile(r"^Session could not be re-established after \d+ attempts\.$")


def _split_saml_diagnostic(message: str) -> tuple[str, str] | None:
    """Split a SAML diagnostic into static head and optional validated suffix."""
    if message in _KNOWN_SAFE_AUTH_MESSAGES or message in _SAML_STATIC_HEADS:
        return message, ""
    for head in _SAML_STATIC_HEADS:
        prefix = head + "; "
        if message.startswith(prefix):
            return head, message[len(prefix) :]
    head_end = message.find("; url=")
    if head_end == -1:
        head_end = message.find("; status=")
    if head_end == -1:
        head_end = message.find("; fields=")
    head = message if head_end == -1 else message[:head_end]
    if _SAML_REQUEST_FAILED_HEAD_RE.match(head):
        suffix = message[len(head) + 2 :] if head_end != -1 else ""
        return head, suffix
    return None


def _is_safe_saml_diagnostic_suffix(suffix: str) -> bool:
    if not suffix:
        return True
    for part in suffix.split("; "):
        if part.startswith("url="):
            url = part[4:]
            if not url.startswith(("http://", "https://")) or " " in url or sanitize_text(url) != url:
                return False
        elif part.startswith("status="):
            try:
                code = int(part[7:])
            except ValueError:
                return False
            if not 100 <= code <= 599:
                return False
        elif part.startswith("fields="):
            if not _SAFE_FIELDS_SUFFIX_RE.match(part):
                return False
        else:
            return False
    return True


def _is_safe_saml_diagnostic(message: str) -> bool:
    split = _split_saml_diagnostic(message)
    if split is None:
        return False
    _head, suffix = split
    return _is_safe_saml_diagnostic_suffix(suffix)


def is_safe_auth_message(message: str) -> bool:
    """Return True if ``message`` was constructed without upstream exception text."""
    if message in _KNOWN_SAFE_AUTH_MESSAGES:
        return True
    if _is_safe_saml_diagnostic(message):
        return True
    if message.startswith("Authentication failed (") and message.endswith("); verify wiki credentials."):
        return True
    return _SESSION_REAUTH_MESSAGE_RE.match(message) is not None


def auth_error_mcp_message(exc: BaseException) -> str:
    """Return the MCP-facing message for an authentication failure."""
    msg = str(exc) or AUTH_FAILURE_MESSAGE
    split = _split_saml_diagnostic(msg)
    if split is not None:
        head, suffix = split
        if _is_safe_saml_diagnostic_suffix(suffix):
            return head
        return AUTH_FAILURE_MESSAGE
    if is_safe_auth_message(msg):
        return msg
    return AUTH_FAILURE_MESSAGE


def register_redactions(*values: str | None) -> None:
    """Register literal strings to scrub from every log record."""
    for value in values:
        if value and len(value) >= _MIN_SECRET_LEN:
            _redactions.add(value)


def register_config_secrets(config: Config) -> None:
    """Register credential passwords from ``config`` for log redaction."""
    for cred in config.ordered_credentials:
        register_redactions(cred.password)


def clear_redactions() -> None:
    """Clear the redaction registry (for tests)."""
    _redactions.clear()


def sanitize_text(text: str) -> str:
    """Return ``text`` with registered secrets and credential-like spans redacted."""
    if not text:
        return text
    out = text
    for secret in sorted(_redactions, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, _REDACTED)
    for pattern in _CREDENTIAL_PATTERNS:
        out = pattern.sub(rf"\1{_REDACTED}", out)
    return out


def safe_exception_summary(exc: BaseException) -> str:
    """Return a log-safe summary of ``exc`` (type name + sanitized message)."""
    if type(exc).__name__ == "Timeout":
        return "Timeout"
    raw = str(exc)
    if not raw:
        return type(exc).__name__
    sanitized = sanitize_text(raw)
    return f"{type(exc).__name__}: {sanitized}"


def summarize_auth_failures(path_summaries: list[str]) -> str:
    """Build a safe :class:`AuthError` message from per-path failure labels."""
    if not path_summaries:
        return AUTH_FAILURE_MESSAGE
    joined = "; ".join(path_summaries)
    return f"Authentication failed ({joined}); verify wiki credentials."


def auth_path_failure_label(cred_label: str, exc: BaseException) -> str:
    """Return a safe per-credential failure label (label + exception type only)."""
    return f"{cred_label}: {type(exc).__name__}"


class LogSafetyFilter(logging.Filter):
    """Scrub credential values and registered content from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Sanitize ``record`` in place; always emit the record."""
        if isinstance(record.msg, str):
            record.msg = sanitize_text(record.msg)
        if record.args:
            args = record.args
            if isinstance(args, dict):
                record.args = {
                    key: sanitize_text(value) if isinstance(value, str) else value for key, value in args.items()
                }
            elif isinstance(args, tuple):
                if len(args) == 1 and isinstance(args[0], dict):
                    mapping = args[0]
                    record.args = (
                        {
                            key: sanitize_text(value) if isinstance(value, str) else value
                            for key, value in mapping.items()
                        },
                    )
                else:
                    record.args = tuple(sanitize_text(arg) if isinstance(arg, str) else arg for arg in args)
        return True
