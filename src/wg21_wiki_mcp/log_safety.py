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

import copy
import logging
import re
import threading
from collections.abc import Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .config import Config

_REDACTED = "[REDACTED]"
_MIN_SECRET_LEN = 4

# Runtime registry of literal values that must never appear in logs.
_redactions: set[str] = set()
_redactions_snapshot: tuple[str, ...] = ()
_redactions_lock = threading.Lock()

# Patterns that often precede credential material in exception or debug text.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(passwd\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(secret\s*[=:]\s*)\S+"),
    re.compile(r"(?is)(authorization\s*:\s*).+?(?:\r?\n(?!\s)|\Z)"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(apikey\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(session\s*[=:]\s*)\S+"),
    re.compile(r"(?is)(set-cookie\s*:\s*).+?(?:\r?\n(?!\s)|\Z)"),
    re.compile(r"(?is)(cookie\s*:\s*).+?(?:\r?\n(?!\s)|\Z)"),
    re.compile(r"(?i)(\bBearer\s+)\S+"),
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
_CLIENTLOGIN_WRAPPER_PREFIX = "clientlogin unavailable; "


def _split_saml_diagnostic(message: str) -> tuple[str, str] | None:
    """Split a SAML diagnostic into static head and optional validated suffix."""
    if message.startswith(_CLIENTLOGIN_WRAPPER_PREFIX):
        message = message[len(_CLIENTLOGIN_WRAPPER_PREFIX) :]
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


def _rebuild_redactions_snapshot_locked() -> None:
    """Refresh longest-first snapshot; caller must hold ``_redactions_lock``."""
    global _redactions_snapshot
    _redactions_snapshot = tuple(sorted(_redactions, key=len, reverse=True))


def _sorted_redactions_snapshot() -> tuple[str, ...]:
    """Return the cached redactions snapshot (longest-first)."""
    with _redactions_lock:
        return _redactions_snapshot


def register_redactions(*values: str | None) -> None:
    """Register literal strings to scrub from every log record."""
    with _redactions_lock:
        changed = False
        for value in values:
            if value and len(value) >= _MIN_SECRET_LEN:
                before = len(_redactions)
                _redactions.add(value)
                if len(_redactions) != before:
                    changed = True
        if changed:
            _rebuild_redactions_snapshot_locked()


def register_config_secrets(config: Config) -> None:
    """Register credential passwords from ``config`` for log redaction."""
    for cred in config.ordered_credentials:
        register_redactions(cred.password)


def clear_redactions() -> None:
    """Clear the redaction registry (for tests)."""
    with _redactions_lock:
        _redactions.clear()
        _rebuild_redactions_snapshot_locked()


def sanitize_text(text: str) -> str:
    """Return ``text`` with registered secrets and credential-like spans redacted."""
    if not text:
        return text
    out = text
    for secret in _sorted_redactions_snapshot():
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


def _sanitize_log_value(value: object) -> object:
    """Recursively redact strings inside log-format argument structures."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {key: _sanitize_log_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_sanitize_log_value(item) for item in value)
    if isinstance(value, list):
        return [_sanitize_log_value(item) for item in value]
    return value


def _sanitized_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    message: str,
) -> BaseException:
    """Build a scrubbed exception without mutating ``exc_value``."""
    if exc_type is UnicodeDecodeError:
        encoding = "utf-8"
        obj: bytes | bytearray | memoryview = b""
        start = 0
        end = 1
        if isinstance(exc_value, UnicodeDecodeError):
            encoding = exc_value.encoding
            obj = exc_value.object
            start = exc_value.start
            end = exc_value.end
        return UnicodeDecodeError(encoding, obj, start, end, message)
    try:
        return exc_type(message)
    except Exception:  # noqa: BLE001 - constructor-heavy types need a copied fallback
        try:
            sanitized_exc = copy.copy(exc_value)
        except Exception:  # noqa: BLE001 - last resort when copy is unsupported
            return RuntimeError(f"{exc_type.__name__}: {message}")
        sanitized_exc.args = (message,)
        return sanitized_exc


def _sanitize_exc_info(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> tuple[type[BaseException], BaseException, TracebackType | None]:
    """Return ``exc_info`` with the exception message scrubbed for credential leaks."""
    exc_type, exc_value, exc_tb = exc_info
    if exc_value is None:
        return exc_info
    summary = safe_exception_summary(exc_value)
    if ": " in summary:
        message = summary.split(": ", 1)[1]
    else:
        message = summary
    sanitized_exc = _sanitized_exception(exc_type, exc_value, message)
    return exc_type, sanitized_exc, exc_tb


class LogSafetyFilter(logging.Filter):
    """Scrub credential values and registered content from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Sanitize ``record`` in place; always emit the record."""
        if isinstance(record.msg, str):
            record.msg = sanitize_text(record.msg)
        if record.args:
            args = record.args
            if isinstance(args, Mapping):
                record.args = cast(Any, _sanitize_log_value(args))
            elif isinstance(args, tuple):
                if len(args) == 1 and isinstance(args[0], Mapping):
                    record.args = (_sanitize_log_value(args[0]),)
                else:
                    record.args = tuple(_sanitize_log_value(arg) for arg in args)
        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            if exc_type is not None and exc_value is not None:
                record.exc_info = _sanitize_exc_info((exc_type, exc_value, exc_tb))
        if isinstance(record.exc_text, str):
            record.exc_text = sanitize_text(record.exc_text)
        return True
