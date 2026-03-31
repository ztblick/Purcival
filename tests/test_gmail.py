"""
Tests for Gmail integration.

Offline tests use mocked API responses — no Google credentials needed.
Live tests require credentials and skip gracefully if not available.

Run with: python tests/test_gmail.py

Covers:
    - Layer 2 filters: _is_automated, _is_directly_addressed
    - get_context() with mocked API (full pipeline)
    - Seen message ID diffing (new vs already-seen)
    - EMAIL_GUIDELINES prepended to context when emails exist
    - Error tracking (consecutive failures, thresholds)
    - Email formatting
    - Edge cases: first run, all filtered, empty inbox
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory import PersonaMemory
from tools.gmail import (
    GmailTool,
    EMAIL_GUIDELINES,
    FAILURE_NOTIFY_THRESHOLD,
    FAILURE_DISABLE_THRESHOLD,
    _is_automated,
    _is_directly_addressed,
)


# --- Helpers ---

def _make_memory():
    """Create a PersonaMemory with a temp directory."""
    tmpdir = tempfile.mkdtemp()
    with patch("memory.DATA_DIR", Path(tmpdir)):
        return PersonaMemory("test_persona")


def _make_tool(memory=None, messages=None, profile_email="zach@example.com"):
    """
    Create a GmailTool with a mocked Gmail API service.

    Args:
        memory: PersonaMemory instance (created if None)
        messages: list of raw message dicts the API will return
            from messages().list(). Each should have 'id' and 'threadId'.
        profile_email: email address returned by the profile endpoint.
    """
    if memory is None:
        memory = _make_memory()
    if messages is None:
        messages = []

    mock_service = MagicMock()

    # Mock users().messages().list()
    mock_service.users().messages().list().execute.return_value = {
        "messages": messages
    }

    # Mock users().getProfile()
    mock_service.users().getProfile().execute.return_value = {
        "emailAddress": profile_email
    }

    # Build a lookup for message details
    _message_details = {}

    def register_message(msg_id, headers, snippet="", thread_id=""):
        """Register a message's details for the mock get() call."""
        payload_headers = [{"name": k, "value": v} for k, v in headers.items()]
        _message_details[msg_id] = {
            "id": msg_id,
            "threadId": thread_id,
            "snippet": snippet,
            "payload": {"headers": payload_headers},
        }

    def mock_get(userId, id, format="metadata", metadataHeaders=None):
        result = MagicMock()
        result.execute.return_value = _message_details.get(id, {
            "id": id, "snippet": "", "payload": {"headers": []}
        })
        return result

    mock_service.users().messages().get = mock_get

    mock_creds = MagicMock()
    tool = GmailTool(memory, mock_creds)
    tool._service = mock_service

    return tool, memory, register_message


def _make_human_email(msg_id, from_addr, subject, snippet="Hello...",
                      to="zach@example.com", cc="", thread_id=""):
    """Create a standard human email for testing."""
    return {
        "msg_id": msg_id,
        "thread_id": thread_id or f"thread_{msg_id}",
        "headers": {
            "From": from_addr,
            "To": to,
            "Subject": subject,
            "Date": "Mon, 30 Mar 2026 14:15:00 -0700",
        },
        "snippet": snippet,
    }


# =======================================================================
# Layer 2 Filter Tests
# =======================================================================

class TestIsAutomated:
    """Test the _is_automated header filter."""

    def test_normal_email_not_automated(self):
        headers = {
            "From": "John Smith <john@example.com>",
            "To": "zach@example.com",
            "Subject": "Resume review",
        }
        assert not _is_automated(headers)

    def test_list_unsubscribe_is_automated(self):
        headers = {
            "From": "Newsletter <news@company.com>",
            "List-Unsubscribe": "<mailto:unsubscribe@company.com>",
        }
        assert _is_automated(headers)

    def test_list_id_is_automated(self):
        headers = {
            "From": "GitHub <notifications@github.com>",
            "List-Id": "<repo.github.com>",
        }
        assert _is_automated(headers)

    def test_precedence_bulk_is_automated(self):
        headers = {
            "From": "marketing@store.com",
            "Precedence": "bulk",
        }
        assert _is_automated(headers)

    def test_precedence_list_is_automated(self):
        headers = {"Precedence": "list"}
        assert _is_automated(headers)

    def test_auto_submitted_is_automated(self):
        headers = {"Auto-Submitted": "auto-generated"}
        assert _is_automated(headers)

    def test_auto_submitted_no_is_not_automated(self):
        """Auto-Submitted: no means a human sent it."""
        headers = {
            "From": "john@example.com",
            "Auto-Submitted": "no",
        }
        assert not _is_automated(headers)

    def test_noreply_sender_is_automated(self):
        headers = {"From": "noreply@bank.com"}
        assert _is_automated(headers)

    def test_no_reply_with_dash_is_automated(self):
        headers = {"From": "no-reply@service.com"}
        assert _is_automated(headers)

    def test_notifications_sender_is_automated(self):
        headers = {"From": "notifications@app.com"}
        assert _is_automated(headers)

    def test_alerts_sender_is_automated(self):
        headers = {"From": "Security Alerts <alerts@google.com>"}
        assert _is_automated(headers)

    def test_mailer_daemon_is_automated(self):
        headers = {"From": "mailer-daemon@mail.example.com"}
        assert _is_automated(headers)

    def test_empty_headers_not_automated(self):
        """No headers means we can't determine — let it through."""
        assert not _is_automated({})


class TestIsDirectlyAddressed:
    """Test the _is_directly_addressed filter."""

    def test_in_to_field(self):
        headers = {"To": "zach@example.com"}
        assert _is_directly_addressed(headers, "zach@example.com")

    def test_in_cc_field(self):
        headers = {
            "To": "alice@example.com",
            "Cc": "zach@example.com, bob@example.com",
        }
        assert _is_directly_addressed(headers, "zach@example.com")

    def test_not_in_any_field(self):
        """BCC'd or not directly addressed — filter out."""
        headers = {
            "To": "alice@example.com",
            "Cc": "bob@example.com",
        }
        assert not _is_directly_addressed(headers, "zach@example.com")

    def test_case_insensitive(self):
        headers = {"To": "Zach@Example.COM"}
        assert _is_directly_addressed(headers, "zach@example.com")

    def test_no_user_email_lets_through(self):
        """If we don't know the user's email, let everything through."""
        headers = {"To": "someone@example.com"}
        assert _is_directly_addressed(headers, "")

    def test_multiple_recipients_in_to(self):
        headers = {"To": "alice@example.com, zach@example.com, bob@example.com"}
        assert _is_directly_addressed(headers, "zach@example.com")


# =======================================================================
# get_context() Integration Tests
# =======================================================================

class TestGetContext:
    """Test the full get_context() pipeline with mocked API."""

    def test_no_messages_returns_none(self):
        tool, mem, register = _make_tool(messages=[])
        assert tool.get_context() is None

    def test_new_human_email_returns_context(self):
        msgs = [{"id": "msg1", "threadId": "t1"}]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg1", {
            "From": "John Smith <john@example.com>",
            "To": "zach@example.com",
            "Subject": "Resume review",
            "Date": "Mon, 30 Mar 2026 14:15:00 -0700",
        }, snippet="Hey Zach, I looked over your resume...")

        context = tool.get_context()
        assert context is not None
        assert "John Smith" in context
        assert "Resume review" in context
        assert "resume" in context.lower()

    def test_guidelines_prepended_when_emails_exist(self):
        msgs = [{"id": "msg1", "threadId": "t1"}]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg1", {
            "From": "john@example.com",
            "To": "zach@example.com",
            "Subject": "Test",
        }, snippet="Hello")

        context = tool.get_context()
        assert context is not None
        # Guidelines should appear before the email
        assert "HOW TO HANDLE EMAILS" in context
        guidelines_pos = context.index("HOW TO HANDLE EMAILS")
        emails_pos = context.index("NEW EMAILS")
        assert guidelines_pos < emails_pos

    def test_automated_email_filtered_returns_none(self):
        msgs = [{"id": "msg1", "threadId": "t1"}]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg1", {
            "From": "noreply@bank.com",
            "To": "zach@example.com",
            "Subject": "Your statement is ready",
            "List-Unsubscribe": "<mailto:unsub@bank.com>",
        }, snippet="View your statement online")

        context = tool.get_context()
        assert context is None

    def test_not_directly_addressed_filtered(self):
        msgs = [{"id": "msg1", "threadId": "t1"}]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg1", {
            "From": "alice@example.com",
            "To": "bob@example.com",
            "Subject": "FYI",
        }, snippet="Just so you know...")

        context = tool.get_context()
        assert context is None

    def test_already_seen_messages_not_repeated(self):
        msgs = [{"id": "msg1", "threadId": "t1"}]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg1", {
            "From": "john@example.com",
            "To": "zach@example.com",
            "Subject": "Test",
        }, snippet="Hello")

        # First call — should return context
        context1 = tool.get_context()
        assert context1 is not None

        # Second call — msg1 is now in seen_ids, should return None
        context2 = tool.get_context()
        assert context2 is None

    def test_mix_of_human_and_automated(self):
        """Only human emails should appear in context."""
        msgs = [
            {"id": "msg_human", "threadId": "t1"},
            {"id": "msg_auto", "threadId": "t2"},
        ]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg_human", {
            "From": "Sarah <sarah@example.com>",
            "To": "zach@example.com",
            "Subject": "Dinner plans",
        }, snippet="Are you free Saturday?")
        register("msg_auto", {
            "From": "noreply@service.com",
            "To": "zach@example.com",
            "Subject": "Your weekly digest",
            "List-Unsubscribe": "<mailto:unsub@service.com>",
        }, snippet="Here's what you missed")

        context = tool.get_context()
        assert context is not None
        assert "Sarah" in context
        assert "Dinner plans" in context
        assert "weekly digest" not in context
        assert "noreply" not in context

    def test_filtered_messages_still_marked_seen(self):
        """Automated messages should be added to seen_ids so they
        don't get re-processed on the next cycle."""
        msgs = [{"id": "msg_auto", "threadId": "t1"}]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg_auto", {
            "From": "noreply@bank.com",
            "To": "zach@example.com",
            "Subject": "Statement",
        })

        tool.get_context()  # Filters it, returns None

        seen = tool._load_seen_ids()
        assert "msg_auto" in seen

    def test_user_email_auto_detected(self):
        """User email should be fetched from profile and cached."""
        tool, mem, register = _make_tool(profile_email="zach@school.edu")

        email = tool._get_user_email()
        assert email == "zach@school.edu"

        # Should be cached in tool_state
        cached = mem.get_tool_state("gmail", "user_email")
        assert cached == "zach@school.edu"

    def test_message_ids_include_thread_id(self):
        """Formatted output should include message and thread IDs
        so Jo can call get_message or get_thread."""
        msgs = [{"id": "msg123", "threadId": "thread456"}]
        tool, mem, register = _make_tool(messages=msgs)
        register("msg123", {
            "From": "john@example.com",
            "To": "zach@example.com",
            "Subject": "Test",
        }, snippet="Hello", thread_id="thread456")

        context = tool.get_context()
        assert "msg123" in context
        assert "thread456" in context


# =======================================================================
# Error Tracking Tests
# =======================================================================

class TestErrorTracking:

    def test_failure_increments(self):
        tool, mem, _ = _make_tool()
        tool._record_failure("API error")
        assert tool._get_consecutive_failures() == 1
        tool._record_failure("Another error")
        assert tool._get_consecutive_failures() == 2

    def test_success_resets_failures(self):
        tool, mem, _ = _make_tool()
        tool._record_failure("error")
        tool._record_failure("error")
        tool._reset_failures()
        assert tool._get_consecutive_failures() == 0

    def test_notify_threshold(self):
        mem = _make_memory()
        tool, _, _ = _make_tool(memory=mem)
        mem.set_tool_state("gmail", "consecutive_failures",
                          str(FAILURE_NOTIFY_THRESHOLD))

        # Make the API call fail
        tool._service.users().messages().list().execute.side_effect = Exception("API down")

        context = tool.get_context()
        assert context is not None
        assert "EMAIL ERROR" in context
        assert "Let Zach know" in context

    def test_disable_threshold(self):
        mem = _make_memory()
        tool, _, _ = _make_tool(memory=mem)
        mem.set_tool_state("gmail", "consecutive_failures",
                          str(FAILURE_DISABLE_THRESHOLD))

        context = tool.get_context()
        assert context is not None
        assert "EMAIL ERROR" in context
        assert "auth" in context.lower() or "re-run" in context.lower()


# =======================================================================
# Seen ID Management
# =======================================================================

class TestSeenIds:

    def test_empty_on_fresh_start(self):
        tool, mem, _ = _make_tool()
        assert tool._load_seen_ids() == set()

    def test_save_and_load(self):
        tool, mem, _ = _make_tool()
        tool._save_seen_ids({"msg1", "msg2", "msg3"})
        loaded = tool._load_seen_ids()
        assert loaded == {"msg1", "msg2", "msg3"}

    def test_caps_at_500(self):
        tool, mem, _ = _make_tool()
        big_set = {f"msg_{i}" for i in range(700)}
        tool._save_seen_ids(big_set)
        loaded = tool._load_seen_ids()
        assert len(loaded) == 500


# =======================================================================
# Method Tests
# =======================================================================

class TestMethods:

    def test_get_methods_includes_all(self):
        tool, _, _ = _make_tool()
        methods = tool.get_methods()
        names = {m.name for m in methods}
        assert "get_unread" in names
        assert "get_message" in names
        assert "get_thread" in names
        assert "draft_reply" in names
        assert "send_email" in names

    def test_tiers_correct(self):
        tool, _, _ = _make_tool()
        methods = {m.name: m.tier for m in tool.get_methods()}
        assert methods["get_unread"] == "observe"
        assert methods["get_message"] == "observe"
        assert methods["get_thread"] == "observe"
        assert methods["draft_reply"] == "draft"
        assert methods["send_email"] == "execute"

    def test_draft_reply_not_implemented(self):
        tool, _, _ = _make_tool()
        try:
            tool.execute("draft_reply", message_id="msg1", body="test")
            assert False, "Should have raised NotImplementedError"
        except NotImplementedError:
            pass

    def test_send_email_not_implemented(self):
        tool, _, _ = _make_tool()
        try:
            tool.execute("send_email", to="a@b.com", subject="x", body="y")
            assert False, "Should have raised NotImplementedError"
        except NotImplementedError:
            pass


# =======================================================================
# Run all tests
# =======================================================================

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestIsAutomated,
        TestIsDirectlyAddressed,
        TestGetContext,
        TestErrorTracking,
        TestSeenIds,
        TestMethods,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            total += 1
            test_name = f"{cls.__name__}.{method_name}"
            try:
                if hasattr(instance, "setup_method"):
                    instance.setup_method()
                getattr(instance, method_name)()
                passed += 1
                print(f"  \u2713 {test_name}")
            except Exception as e:
                failed += 1
                errors.append((test_name, e))
                print(f"  \u2717 {test_name}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"\n  {name}:")
            traceback.print_exception(type(err), err, err.__traceback__)

    sys.exit(0 if failed == 0 else 1)
