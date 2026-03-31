"""
GmailTool — gives the agent awareness of Zach's inbox.

Three-layer filtering ensures only genuine human correspondence
reaches the LLM:

    Layer 1: Gmail API query — category:primary is:unread newer_than:1d
             (eliminates ~90% of email volume on Google's servers)
    Layer 2: Python header filters — mailing list headers, no-reply
             senders, auto-submitted messages, not-directly-addressed
             (eliminates automated messages that snuck through)
    Layer 3: LLM reasoning — Jo decides what's worth Zach's attention
             (guided by EMAIL_GUIDELINES prepended to context output)

The default is silence. Jo only messages Zach about emails that
require action, connect to known context, or contain time-sensitive
information. Zach checks his own inbox — Jo catches what he'd miss.

Error tracking mirrors GoogleCalendarTool: consecutive failures are
counted, user is notified at 3, tool disables at 10.

All state is persisted in the tool_state table via PersonaMemory.
"""

import json
import logging
from datetime import datetime, timedelta

from googleapiclient.discovery import build

from tools.base import Tool, ToolMethod
from memory import PersonaMemory

logger = logging.getLogger(__name__)

# --- Configuration ---

# Gmail API query — runs on Google's servers, pre-filters before download
GMAIL_QUERY = "category:primary is:unread newer_than:1d"

# Maximum snippet length for get_context() output (chars)
SNIPPET_MAX_LENGTH = 200

# Maximum body length when fetching full message content (chars)
BODY_MAX_LENGTH = 2000

# Error tracking thresholds (same as GoogleCalendarTool)
FAILURE_NOTIFY_THRESHOLD = 3
FAILURE_DISABLE_THRESHOLD = 10

# Headers to request from the API for filtering
FILTER_HEADERS = [
    "From", "To", "Cc", "Subject", "Date",
    "List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted",
]

# No-reply sender patterns — emails from these are always automated
NOREPLY_PATTERNS = [
    "noreply@", "no-reply@", "donotreply@", "do-not-reply@",
    "notifications@", "notification@", "alerts@", "alert@",
    "mailer-daemon@", "postmaster@", "bounce@",
]


# --- Email Guidelines ---
# Prepended to get_context() output when there are emails to show.
# This is the "skill file" pattern — behavioral instructions load
# when the capability is invoked, not on every interaction.

EMAIL_GUIDELINES = """\
## HOW TO HANDLE EMAILS

The emails below have been pre-filtered to real messages from real \
people addressed directly to Zach. Automated messages, newsletters, \
marketing, and notifications have already been removed. Everything \
you see here is genuine human correspondence.

But not every real email needs Zach's attention right now. Your \
default is silence — Zach checks his own inbox. Your job is to \
catch the things he'd miss, forget about, or would want to engage \
with sooner.

WHEN TO MESSAGE ZACH:
  - The email requires action from him (reply, review, decision)
  - It contains time-sensitive information (deadline, meeting change)
  - It connects to something you already know about — a project \
he's excited about, a person he's been waiting to hear from, \
an opportunity he mentioned
  - It contains unexpectedly good or bad news
  - You think he'd genuinely want to know about it right now

WHEN TO STAY QUIET:
  - FYI emails that can wait until he checks his inbox
  - Thread replies where he's CC'd but not the primary actor
  - Routine confirmations, even from real people
  - Anything where your honest assessment is "this can wait"

HOW TO MESSAGE ZACH ABOUT AN EMAIL:
  Don't just forward the subject line. Be a thinking partner. \
Connect the email to what you know about Zach's life, goals, \
schedule, and recent conversations. Be curious about his reaction.

  Good: "John just emailed about reviewing your resume — this is \
the opportunity you were excited about last week. Want me to pull \
up what he said?"
  Good: "Your old department at Menlo just reached out. From the \
subject line, it looks like they might be asking you to come back. \
Have you seen this?"
  Good: "You got an email from someone at Google recruiting. I don't \
know if you're interested, but given our conversations about the \
career switch, I wanted to make sure you saw it."

  Bad: "You have a new email from John Smith with subject 'Resume \
review'." (Just forwarding — no thinking)
  Bad: "New email alert: 3 unread messages in your inbox." (You're \
not a notification system)

  Ask questions. Be curious. If you think an email might make Zach \
excited, nervous, or relieved — say so. If it connects to a \
conversation you've had before, make that connection. You have \
context about Zach's life that a normal email client doesn't. Use it.

IF YOU'RE UNSURE:
  Note the email in your narrative state so you remember it, but \
don't message Zach. You can bring it up later — "by the way, you \
got an email from X yesterday, did you see that?" is a perfectly \
good approach during a future check-in.\
"""


class GmailTool(Tool):

    name = "gmail"
    description = (
        "Read Zach's email inbox. See new messages from real people "
        "with sender, subject, and a preview snippet. Newsletters, "
        "marketing, and automated messages are pre-filtered out."
    )

    def __init__(self, memory: PersonaMemory, credentials):
        """
        Args:
            memory: The persona's memory instance for state persistence.
            credentials: A valid google.oauth2.credentials.Credentials
                object with gmail.readonly scope.
        """
        self.memory = memory
        self._credentials = credentials
        self._service = build("gmail", "v1", credentials=credentials)

    # --- Tool Interface ---

    def get_context(self) -> str | None:
        """
        Perception: fetch unread primary emails, filter, and return
        what's new and genuinely from a human.

        Returns None if no new emails pass the filters.
        Returns EMAIL_GUIDELINES + formatted email snippets otherwise.

        Never calls an LLM. Pure API + filter logic.
        """
        # Check failure threshold
        failures = self._get_consecutive_failures()
        if failures >= FAILURE_DISABLE_THRESHOLD:
            return (
                "EMAIL ERROR: Gmail access has failed "
                f"{failures} consecutive times. The last error was: "
                f"{self._get_last_error()}. "
                "Ask Zach to re-run the Google auth flow from the terminal."
            )

        try:
            # 1. Ensure we have the user's email address for filtering
            user_email = self._get_user_email()

            # 2. Fetch unread primary emails from the API
            raw_messages = self._fetch_messages()

            # 3. Diff against seen message IDs
            seen_ids = self._load_seen_ids()
            new_messages = [m for m in raw_messages if m["id"] not in seen_ids]

            if not new_messages:
                self._reset_failures()
                return None

            # 4. Fetch headers and snippets for new messages
            detailed = self._fetch_message_details(new_messages)

            # 5. Apply Python header filters (Layer 2)
            filtered = []
            for msg in detailed:
                headers = msg["headers"]
                if _is_automated(headers):
                    logger.debug(
                        f"Gmail: filtered automated message "
                        f"from {headers.get('From', '?')}: "
                        f"{headers.get('Subject', '?')}"
                    )
                    continue
                if not _is_directly_addressed(headers, user_email):
                    logger.debug(
                        f"Gmail: filtered not-directly-addressed message "
                        f"from {headers.get('From', '?')}: "
                        f"{headers.get('Subject', '?')}"
                    )
                    continue
                filtered.append(msg)

            # 6. Update seen IDs (ALL fetched messages, not just filtered ones —
            #    we don't want to re-process filtered messages next cycle)
            all_ids = seen_ids | {m["id"] for m in raw_messages}
            self._save_seen_ids(all_ids)

            # 7. Reset failure counter
            self._reset_failures()

            # 8. Format and return with guidelines
            if not filtered:
                return None

            formatted = self._format_emails(filtered)
            return f"{EMAIL_GUIDELINES}\n\n{formatted}"

        except Exception as e:
            self._record_failure(str(e))
            failures = self._get_consecutive_failures()
            logger.error(
                f"GmailTool.get_context() failed "
                f"({failures} consecutive): {e}"
            )

            if failures >= FAILURE_NOTIFY_THRESHOLD:
                return (
                    f"EMAIL ERROR: Gmail access has failed "
                    f"{failures} consecutive times. Latest error: {e}. "
                    "Let Zach know so he can investigate."
                )

            return None

    def get_methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                name="get_unread",
                description=(
                    "Fetch the current list of unread emails with "
                    "sender, subject, and snippet preview."
                ),
                tier="observe",
                parameters={
                    "max_results": {
                        "type": "int",
                        "description": "Maximum emails to return (default 10)",
                        "required": False,
                    },
                },
            ),
            ToolMethod(
                name="get_message",
                description=(
                    "Fetch the full content of a specific email by ID. "
                    "Use this when you need more detail than the snippet."
                ),
                tier="observe",
                parameters={
                    "message_id": {
                        "type": "str",
                        "description": "The Gmail message ID",
                        "required": True,
                    },
                },
            ),
            ToolMethod(
                name="get_thread",
                description=(
                    "Fetch all messages in an email conversation thread. "
                    "Use this to see the full back-and-forth context."
                ),
                tier="observe",
                parameters={
                    "thread_id": {
                        "type": "str",
                        "description": "The Gmail thread ID",
                        "required": True,
                    },
                },
            ),
            ToolMethod(
                name="draft_reply",
                description=(
                    "Create a draft reply to an email. The draft is saved "
                    "in Gmail's Drafts folder for Zach to review and send."
                ),
                tier="draft",
                parameters={
                    "message_id": {
                        "type": "str",
                        "description": "The message ID to reply to",
                        "required": True,
                    },
                    "body": {
                        "type": "str",
                        "description": "The reply text",
                        "required": True,
                    },
                },
            ),
            ToolMethod(
                name="send_email",
                description=(
                    "Send an email from Zach's account. Requires explicit "
                    "approval before sending."
                ),
                tier="execute",
                parameters={
                    "to": {
                        "type": "str",
                        "description": "Recipient email address",
                        "required": True,
                    },
                    "subject": {
                        "type": "str",
                        "description": "Email subject line",
                        "required": True,
                    },
                    "body": {
                        "type": "str",
                        "description": "Email body text",
                        "required": True,
                    },
                },
            ),
        ]

    def execute(self, method_name: str, **kwargs) -> str:
        if method_name == "get_unread":
            return self._get_unread(kwargs.get("max_results", 10))
        elif method_name == "get_message":
            msg_id = kwargs.get("message_id")
            if not msg_id:
                raise ValueError("get_message requires a message_id")
            return self._get_message(msg_id)
        elif method_name == "get_thread":
            thread_id = kwargs.get("thread_id")
            if not thread_id:
                raise ValueError("get_thread requires a thread_id")
            return self._get_thread(thread_id)
        elif method_name == "draft_reply":
            raise NotImplementedError(
                "draft_reply is not yet implemented. "
                "This requires gmail.compose scope."
            )
        elif method_name == "send_email":
            raise NotImplementedError(
                "send_email is not yet implemented. "
                "This requires gmail.compose scope."
            )
        else:
            raise ValueError(f"Unknown method '{method_name}' on GmailTool")

    # --- Gmail API Operations ---

    def _fetch_messages(self, query: str = GMAIL_QUERY, max_results: int = 20) -> list[dict]:
        """
        Fetch message IDs and thread IDs matching the query.

        Returns a list of dicts with 'id' and 'threadId'.
        Does NOT fetch full message content — that's a separate call.
        """
        result = self._service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
        ).execute()

        return result.get("messages", [])

    def _fetch_message_details(self, messages: list[dict]) -> list[dict]:
        """
        Fetch headers and snippet for each message.

        Returns a list of dicts with:
            id, threadId, headers (dict), snippet, date
        """
        detailed = []

        for msg in messages:
            try:
                full = self._service.users().messages().get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=FILTER_HEADERS,
                ).execute()

                # Extract headers into a flat dict
                headers = {}
                for h in full.get("payload", {}).get("headers", []):
                    headers[h["name"]] = h["value"]

                detailed.append({
                    "id": msg["id"],
                    "threadId": msg.get("threadId", ""),
                    "headers": headers,
                    "snippet": full.get("snippet", "")[:SNIPPET_MAX_LENGTH],
                })
            except Exception as e:
                logger.warning(f"Failed to fetch message {msg['id']}: {e}")
                continue

        return detailed

    def _get_user_email(self) -> str:
        """
        Get the authenticated user's email address.

        Cached in tool_state after first successful fetch so we
        don't call the profile endpoint on every cycle.
        """
        cached = self.memory.get_tool_state(self.name, "user_email")
        if cached:
            return cached

        profile = self._service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "")

        if email:
            self.memory.set_tool_state(self.name, "user_email", email)
            logger.info(f"Gmail user email cached: {email}")

        return email

    # --- Explicit Method Implementations ---

    def _get_unread(self, max_results: int = 10) -> str:
        """Fetch unread messages with snippets (explicit method call)."""
        try:
            user_email = self._get_user_email()
            raw = self._fetch_messages(max_results=max_results)

            if not raw:
                return "No unread emails in Primary."

            detailed = self._fetch_message_details(raw)

            # Apply filters
            filtered = [
                m for m in detailed
                if not _is_automated(m["headers"])
                and _is_directly_addressed(m["headers"], user_email)
            ]

            if not filtered:
                return "No unread emails from real people addressed to you."

            return self._format_emails(filtered)

        except Exception as e:
            self._record_failure(str(e))
            return f"Error fetching email: {e}"

    def _get_message(self, message_id: str) -> str:
        """Fetch the full content of a specific message."""
        try:
            full = self._service.users().messages().get(
                userId="me",
                id=message_id,
                format="full",
            ).execute()

            # Extract headers
            headers = {}
            for h in full.get("payload", {}).get("headers", []):
                headers[h["name"]] = h["value"]

            # Extract body text
            body = self._extract_body(full.get("payload", {}))
            if len(body) > BODY_MAX_LENGTH:
                body = body[:BODY_MAX_LENGTH] + "\n\n[... message truncated ...]"

            from_addr = headers.get("From", "unknown")
            subject = headers.get("Subject", "(no subject)")
            date = headers.get("Date", "unknown date")

            return (
                f"From: {from_addr}\n"
                f"Subject: {subject}\n"
                f"Date: {date}\n"
                f"\n{body}"
            )

        except Exception as e:
            return f"Error fetching message {message_id}: {e}"

    def _get_thread(self, thread_id: str) -> str:
        """Fetch all messages in a thread."""
        try:
            thread = self._service.users().threads().get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            messages = thread.get("messages", [])
            if not messages:
                return f"Thread {thread_id} is empty."

            lines = [f"Thread with {len(messages)} message(s):\n"]

            for msg in messages:
                headers = {}
                for h in msg.get("payload", {}).get("headers", []):
                    headers[h["name"]] = h["value"]

                from_addr = headers.get("From", "unknown")
                date = headers.get("Date", "")
                snippet = msg.get("snippet", "")[:SNIPPET_MAX_LENGTH]

                lines.append(
                    f"  [{date}] {from_addr}\n"
                    f"    {snippet}\n"
                )

            return "\n".join(lines)

        except Exception as e:
            return f"Error fetching thread {thread_id}: {e}"

    # --- Body Extraction ---

    def _extract_body(self, payload: dict) -> str:
        """
        Extract plain text body from a Gmail message payload.

        Gmail messages can be simple (body in payload.body) or
        multipart (body in nested parts). We look for text/plain
        first, then fall back to text/html with tag stripping.
        """
        # Simple message — body directly in payload
        if payload.get("body", {}).get("data"):
            return self._decode_body(payload["body"]["data"])

        # Multipart message — search parts for text/plain
        parts = payload.get("parts", [])
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/plain" and part.get("body", {}).get("data"):
                return self._decode_body(part["body"]["data"])

        # Fallback: look for text/html and strip tags
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/html" and part.get("body", {}).get("data"):
                html = self._decode_body(part["body"]["data"])
                return self._strip_html(html)

        # Nested multipart — recurse into parts
        for part in parts:
            if part.get("parts"):
                result = self._extract_body(part)
                if result:
                    return result

        return "(could not extract message body)"

    def _decode_body(self, data: str) -> str:
        """Decode base64url-encoded body data from Gmail API."""
        import base64
        # Gmail uses URL-safe base64 encoding
        padded = data + "=" * (4 - len(data) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        return decoded.decode("utf-8", errors="replace")

    def _strip_html(self, html: str) -> str:
        """Rough HTML tag stripping for fallback body extraction."""
        import re
        # Remove script and style blocks
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Remove tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # --- Formatting ---

    def _format_emails(self, messages: list[dict]) -> str:
        """Format filtered messages into a text block for the LLM."""
        lines = [f"NEW EMAILS ({len(messages)}):\n"]

        for msg in messages:
            headers = msg["headers"]
            from_addr = headers.get("From", "unknown sender")
            subject = headers.get("Subject", "(no subject)")
            date = headers.get("Date", "unknown date")
            snippet = msg.get("snippet", "")

            lines.append(
                f"  FROM: {from_addr}\n"
                f"  SUBJECT: {subject}\n"
                f"  DATE: {date}\n"
                f"  ID: {msg['id']}  THREAD: {msg.get('threadId', '')}\n"
                f"  SNIPPET: {snippet}\n"
            )

        return "\n".join(lines)

    # --- State Persistence ---

    def _load_seen_ids(self) -> set[str]:
        """Load the set of already-seen message IDs from tool_state."""
        raw = self.memory.get_tool_state(self.name, "seen_message_ids")
        if raw:
            try:
                return set(json.loads(raw))
            except json.JSONDecodeError:
                pass
        return set()

    def _save_seen_ids(self, ids: set[str]):
        """
        Save seen message IDs to tool_state.

        Only keeps the most recent 500 IDs to prevent unbounded growth.
        Older IDs fall off naturally since we only query newer_than:1d.
        """
        # Keep only the most recent 500
        id_list = sorted(ids)[-500:]
        self.memory.set_tool_state(
            self.name, "seen_message_ids", json.dumps(id_list)
        )

    # --- Error Tracking (mirrors GoogleCalendarTool) ---

    def _get_consecutive_failures(self) -> int:
        raw = self.memory.get_tool_state(self.name, "consecutive_failures")
        try:
            return int(raw) if raw else 0
        except (ValueError, TypeError):
            return 0

    def _get_last_error(self) -> str:
        return self.memory.get_tool_state(self.name, "last_error") or "unknown"

    def _record_failure(self, error: str):
        failures = self._get_consecutive_failures() + 1
        self.memory.set_tool_state(self.name, "consecutive_failures", str(failures))
        self.memory.set_tool_state(self.name, "last_error", error)

    def _reset_failures(self):
        if self._get_consecutive_failures() > 0:
            self.memory.set_tool_state(self.name, "consecutive_failures", "0")


# --- Layer 2: Python Header Filters ---
# These are module-level functions so they can be tested independently.

def _is_automated(headers: dict) -> bool:
    """
    Returns True if the email is definitively automated/mass mail.
    These are filtered silently — the LLM never sees them.
    """
    # 1. Mailing list headers — definitive signal
    if headers.get("List-Unsubscribe"):
        return True
    if headers.get("List-Id"):
        return True

    # 2. Bulk/list precedence — definitive signal
    precedence = (headers.get("Precedence") or "").lower()
    if precedence in ("bulk", "list", "junk"):
        return True

    # 3. Auto-submitted header — definitive signal
    auto_submitted = (headers.get("Auto-Submitted") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return True

    # 4. No-reply sender patterns — very strong signal
    sender = (headers.get("From") or "").lower()
    for pattern in NOREPLY_PATTERNS:
        if pattern in sender:
            return True

    return False


def _is_directly_addressed(headers: dict, user_email: str) -> bool:
    """
    Returns True if the user's email is in the To or CC fields.

    Filters out BCC'd mass sends and emails where the user isn't
    an explicit recipient. CC'd emails pass because a colleague
    looping the user in is often relevant.
    """
    if not user_email:
        return True  # Can't check without email — let it through

    to_field = (headers.get("To") or "").lower()
    cc_field = (headers.get("Cc") or "").lower()
    user = user_email.lower()
    return user in to_field or user in cc_field
