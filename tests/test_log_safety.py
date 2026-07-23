"""Tests for log redaction and auth error message safety (Issue #3)."""

from __future__ import annotations

import logging
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from conftest import FakePage, FakeWikiClient
from filelock import Timeout
from mwclient.errors import LoginError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.errors import AUTH_ERROR, AuthError, to_mcp_error
from wg21_wiki_mcp.fetch import PageFetcher
from wg21_wiki_mcp.log import get_logger
from wg21_wiki_mcp.log_safety import (
    AUTH_FAILURE_MESSAGE,
    LogSafetyFilter,
    auth_error_mcp_message,
    auth_path_failure_label,
    clear_redactions,
    is_safe_auth_message,
    register_config_secrets,
    register_redactions,
    safe_exception_summary,
    sanitize_text,
    summarize_auth_failures,
)
from wg21_wiki_mcp.models import AuthError as AuthErrorModel


@pytest.fixture(autouse=True)
def _clean_redactions():
    clear_redactions()
    yield
    clear_redactions()


class TestSanitizeText:
    def test_redacts_registered_secret(self):
        register_redactions("supersecretpassword")
        assert "supersecretpassword" not in sanitize_text("login failed: supersecretpassword")
        assert "[REDACTED]" in sanitize_text("login failed: supersecretpassword")

    def test_redacts_registered_wiki_content(self):
        content = "CONFIDENTIAL committee wikitext body that must not leak"
        register_redactions(content)
        assert content not in sanitize_text(f"accidental log: {content}")
        assert "[REDACTED]" in sanitize_text(f"accidental log: {content}")

    def test_redacts_credential_patterns(self):
        out = sanitize_text("Auth failed password=MySecret123 token=abc")
        assert "MySecret123" not in out
        assert "abc" not in out
        assert out.count("[REDACTED]") >= 2
        bearer = sanitize_text("Authorization: Bearer mytoken123")
        assert "mytoken123" not in bearer
        assert "[REDACTED]" in bearer
        bare_bearer = sanitize_text("Use Bearer mytoken123 here")
        assert "mytoken123" not in bare_bearer
        cookie = sanitize_text("Cookie: session=abc123; path=/")
        assert "abc123" not in cookie
        assert "[REDACTED]" in cookie

    def test_redacts_multiline_authorization_header(self):
        text = "Authorization: Bearer secret-token\nX-Other: value"
        out = sanitize_text(text)
        assert "secret-token" not in out
        assert "X-Other: value" in out

    def test_ignores_short_secrets(self):
        register_redactions("abc")
        assert sanitize_text("abc") == "abc"

    def test_session_reauth_message_requires_full_format(self):
        assert is_safe_auth_message("Session could not be re-established after 6 attempts.")
        assert not is_safe_auth_message("Session could not be re-established after 6 attempts. leaked-secret")

    def test_empty_text_is_unchanged(self):
        assert sanitize_text("") == ""

    def test_known_safe_auth_messages(self):
        assert is_safe_auth_message(AUTH_FAILURE_MESSAGE)
        assert is_safe_auth_message("SAML login failed (no SAMLResponse; check credentials/MFA).")
        assert is_safe_auth_message("Authentication failed (bot: LoginError); verify wiki credentials.")

    def test_summarize_auth_failures_empty_paths(self):
        assert summarize_auth_failures([]) == AUTH_FAILURE_MESSAGE

    def test_safe_exception_summary_empty_message(self):
        assert safe_exception_summary(RuntimeError()) == "RuntimeError"

    def test_auth_error_mcp_message_passes_safe_message(self):
        safe = summarize_auth_failures(["bot: LoginError"])
        assert auth_error_mcp_message(AuthErrorModel(safe)) == safe

    def test_saml_diagnostic_mcp_message_strips_dynamic_suffix(self):
        full = "SAML SSO entry point returned HTTP error.; url=https://w.example; status=403"
        assert auth_error_mcp_message(AuthErrorModel(full)) == "SAML SSO entry point returned HTTP error."

    def test_saml_diagnostic_with_unsafe_url_falls_back(self):
        unsafe = "SAML SSO entry point returned HTTP error.; url=https://w.example/?password=leaked"
        assert auth_error_mcp_message(AuthErrorModel(unsafe)) == AUTH_FAILURE_MESSAGE
        assert not is_safe_auth_message(unsafe)

    def test_saml_diagnostic_with_valid_suffix_is_safe(self):
        msg = "SAML SSO entry point returned HTTP error.; url=https://w.example; status=403"
        assert is_safe_auth_message(msg)

    def test_clientlogin_wrapper_preserves_saml_mcp_message(self):
        wrapped = (
            "clientlogin unavailable; SAML SSO entry point returned HTTP error.; url=https://w.example; status=403"
        )
        assert is_safe_auth_message(wrapped)
        assert auth_error_mcp_message(AuthErrorModel(wrapped)) == "SAML SSO entry point returned HTTP error."

    def test_saml_request_failed_diagnostic_is_safe(self):
        msg = "SAML SSO request failed: Timeout.; url=https://w.example; status=504"
        assert is_safe_auth_message(msg)
        assert auth_error_mcp_message(AuthErrorModel(msg)) == "SAML SSO request failed: Timeout."

    def test_saml_diagnostic_rejects_invalid_status_suffix(self):
        msg = "SAML SSO entry point returned HTTP error.; status=not-a-number"
        assert not is_safe_auth_message(msg)

    def test_saml_diagnostic_rejects_invalid_fields_suffix(self):
        msg = "Could not locate username/password fields on the IdP form.; fields=['bad name']"
        assert not is_safe_auth_message(msg)

    def test_saml_diagnostic_rejects_unknown_suffix_part(self):
        msg = "SAML ACS endpoint rejected the response.; evil=payload"
        assert not is_safe_auth_message(msg)

    def test_saml_static_head_without_suffix_is_safe(self):
        assert is_safe_auth_message("SAML IdP POST returned HTTP error.")

    def test_saml_diagnostic_rejects_status_out_of_range(self):
        msg = "SAML SSO entry point returned HTTP error.; status=99"
        assert not is_safe_auth_message(msg)

    def test_register_config_secrets(self, tmp_path):
        config = Config(
            base_url="https://w.example",
            bot=Credentials("bot", "b", "bot-password-value"),
            user=Credentials("user", "u", "user-password-value"),
            cache_dir=tmp_path / "c",
        )
        register_config_secrets(config)
        assert "bot-password-value" not in sanitize_text("leak bot-password-value")
        assert "user-password-value" not in sanitize_text("leak user-password-value")

    def test_server_context_create_registers_secrets(self, tmp_path):
        config = Config(
            base_url="https://w.example",
            bot=Credentials("bot", "b", "ctx-secret-password"),
            user=None,
            cache_dir=tmp_path / "c",
        )
        ctx = ServerContext.create(config)
        try:
            assert "ctx-secret-password" not in sanitize_text("ctx-secret-password")
        finally:
            ctx.close()


class TestLogSafetyFilter:
    def test_filter_scrubs_message_and_args(self, caplog):
        secret = "bot-password-value-xyz"
        content = "verbatim wiki page content block"
        register_redactions(secret, content)

        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.test_log_safety")
        logger = get_logger("test_log_safety")
        logger.warning("failure with %s and %s", secret, content)

        combined = caplog.text
        assert secret not in combined
        assert content not in combined
        assert "[REDACTED]" in combined

    def test_filter_applies_to_root_logger_records(self, caplog):
        secret = "registered-runtime-secret"
        register_redactions(secret)

        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.fetch")
        logger = get_logger("fetch")
        logger.warning("lock issue: %s", secret)

        assert secret not in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_get_logger_filter_is_idempotent(self):
        logger = get_logger("test.idempotent")
        filter_count = sum(1 for filt in logger.filters if isinstance(filt, LogSafetyFilter))
        again = get_logger("test.idempotent")
        assert again is logger
        assert filter_count == 1
        assert sum(1 for filt in again.filters if isinstance(filt, LogSafetyFilter)) == 1

    def test_raw_stdlib_logger_under_package_is_redacted(self, caplog):
        secret = "raw-stdlib-secret-value"
        register_redactions(secret)
        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.raw_test")
        logging.getLogger("wg21_wiki_mcp.raw_test").warning("leak %s", secret)
        assert secret not in caplog.text
        assert "[REDACTED]" in caplog.text


class TestAuthFailureMessages:
    def test_summarize_auth_failures_omits_upstream_text(self):
        msg = summarize_auth_failures(["bot: LoginError", "user: ConnectionError"])
        assert "LoginError" in msg
        assert "verify wiki credentials" in msg
        assert "bad creds" not in msg

    def test_auth_path_failure_label_is_type_only(self):
        exc = ValueError("password=leaked-from-idp")
        label = auth_path_failure_label("bot", exc)
        assert label == "bot: ValueError"
        assert "leaked" not in label
        assert "password" not in label

    def test_simulated_login_failure_raises_safe_auth_error(self, tmp_path, monkeypatch):
        secret = "my-bot-password-should-not-appear"

        class LeakySite:
            connection = MagicMock()

            def login(self, _u, _p):
                raise LoginError(self, "Failed", f"invalid password {secret}")

        client = wc.WikiClient(
            Config(
                base_url="https://w.example",
                bot=Credentials("bot", "Acct@bot", secret),
                user=None,
                cache_dir=tmp_path / "c",
            )
        )
        monkeypatch.setattr(client, "_new_site", lambda: LeakySite())

        with pytest.raises(AuthErrorModel) as exc_info:
            client.login()

        message = str(exc_info.value)
        assert secret not in message
        assert "invalid password" not in message
        assert "bot: LoginError" in message

    def test_auth_error_mcp_message_contains_no_secret(self):
        secret = "my-wiki-bot-password-value"
        err = to_mcp_error(AuthError(f"legacy unsafe text with {secret}"))
        assert err.error.code == AUTH_ERROR
        assert secret not in err.error.message

    def test_calendar_failure_log_contains_no_secret(self, tmp_path, caplog):
        secret = "calendar-log-secret-value"
        register_redactions(secret)
        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.meetings")

        from wg21_wiki_mcp.meetings import MeetingCalendar

        class _BrokenSession:
            headers: dict[str, str] = {}

            def get(self, *_a, **_k):
                raise RuntimeError(f"network down password={secret}")

        cal = MeetingCalendar(
            Config(
                base_url="https://w.example",
                bot=Credentials("bot", "b", secret),
                user=None,
                cache_dir=tmp_path / "c",
            ),
            session=_BrokenSession(),
        )
        cal.is_meeting_active()

        assert secret not in caplog.text
        assert "network down" in caplog.text.lower() or "RuntimeError" in caplog.text

    def test_fetch_lock_timeout_log_contains_no_page_title(self, tmp_path, caplog):
        page_title = "SecretCommitteePageTitle"
        page_content = "confidential wikitext that must never be logged"
        register_redactions(page_content)

        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.fetch")

        client = FakeWikiClient()
        client.pages[page_title] = FakePage(page_content, 1)
        cache = Cache(tmp_path / "c")
        fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]

        with patch("wg21_wiki_mcp.fetch.FileLock") as mock_fl:
            mock_lock = MagicMock()
            mock_lock.acquire.side_effect = Timeout(f"blocked on {page_title}")
            mock_fl.return_value = mock_lock
            fetcher.get_page(page_title, ttl_seconds=1000)

        assert page_title not in caplog.text
        assert page_content not in caplog.text
        assert "title_hash=" in caplog.text
        cache.close()


class TestRedactionsConcurrency:
    def test_concurrent_register_clear_and_sanitize(self):
        witness = "anchor-redaction-secret-xyz"
        register_redactions(witness)
        probe = f"leak {witness} trailer"
        errors: list[BaseException] = []
        witness_live = threading.Event()
        witness_live.set()
        start = threading.Barrier(4)

        def register_volatile() -> None:
            try:
                start.wait(timeout=5)
                for i in range(200):
                    register_redactions(f"volatile-secret-{i:04d}-padding")
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        def clear_and_restore() -> None:
            try:
                start.wait(timeout=5)
                for _ in range(40):
                    witness_live.clear()
                    clear_redactions()
                    register_redactions(witness)
                    witness_live.set()
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        def scrub_loop() -> None:
            try:
                start.wait(timeout=5)
                for i in range(400):
                    sanitize_text(f"leak volatile-secret-{i % 200:04d}-padding noise")
                    sanitize_text("token=abc password=def")
                    if witness_live.is_set() and witness in sanitize_text(probe):
                        raise AssertionError("witness secret leaked during concurrent clear/register")
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=register_volatile),
            threading.Thread(target=register_volatile),
            threading.Thread(target=clear_and_restore),
            threading.Thread(target=scrub_loop),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert not any(thread.is_alive() for thread in threads)
        assert witness not in sanitize_text(f"leak {witness}")

        post_check = "post-concurrency-check-secret"
        register_redactions(post_check)
        assert post_check not in sanitize_text(f"leak {post_check}")

    def test_concurrent_register_does_not_drop_active_redactions(self):
        witness = "persistent-witness-secret-abc"
        register_redactions(witness)
        errors: list[BaseException] = []
        start = threading.Barrier(3)
        probe = f"leak {witness} trailer"

        def register_more() -> None:
            try:
                start.wait(timeout=5)
                for i in range(300):
                    register_redactions(f"extra-secret-{i:04d}-suffix")
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        def scrub_witness() -> None:
            try:
                start.wait(timeout=5)
                for _ in range(500):
                    if witness in sanitize_text(probe):
                        raise AssertionError("witness secret leaked during concurrent register")
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=register_more),
            threading.Thread(target=register_more),
            threading.Thread(target=scrub_witness),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert not any(thread.is_alive() for thread in threads)
        assert witness not in sanitize_text(f"leak {witness}")


class TestLogSafetyFilterUnit:
    def test_filter_returns_true(self):
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        assert LogSafetyFilter().filter(record) is True

    def test_filter_handles_mapping_style_args(self):
        register_redactions("supersecretpassword")
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="value=%(key)s",
            args=({"key": "supersecretpassword"},),
            exc_info=None,
        )
        assert LogSafetyFilter().filter(record) is True
        assert record.args == {"key": "[REDACTED]"}

    def test_filter_handles_tuple_wrapped_mapping_args(self):
        register_redactions("tuple-secret-value")
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="value=%(key)s",
            args=(),
            exc_info=None,
        )
        record.args = ({"key": "tuple-secret-value"},)
        assert LogSafetyFilter().filter(record) is True
        assert record.args == ({"key": "[REDACTED]"},)

    def test_filter_skips_non_string_message(self):
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=12345,
            args=(),
            exc_info=None,
        )
        assert LogSafetyFilter().filter(record) is True
        assert record.msg == 12345

    def test_filter_scrubs_nested_args(self):
        register_redactions("nested-secret-value")
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="nested %(outer)s",
            args=(),
            exc_info=None,
        )
        record.args = ({"outer": {"inner": "nested-secret-value"}},)
        assert LogSafetyFilter().filter(record) is True
        assert record.args == ({"outer": {"inner": "[REDACTED]"}},)

    def test_filter_scrubs_exc_info_and_exc_text(self):
        secret = "exc-secret-password"
        register_redactions(secret)
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="boom",
            args=(),
            exc_info=None,
        )
        try:
            raise ValueError(f"password={secret}")
        except ValueError:
            record.exc_info = sys.exc_info()
        record.exc_text = f"Traceback...\npassword={secret}"
        assert LogSafetyFilter().filter(record) is True
        formatter = logging.Formatter()
        formatted = formatter.formatException(record.exc_info)
        assert secret not in formatted
        assert secret not in record.exc_text
        assert "[REDACTED]" in record.exc_text
