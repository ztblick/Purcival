"""
TelegramTool — wraps the existing Telegram send function.

This tool gives the agent loop a uniform interface for sending messages
to the user. It doesn't do perception — incoming Telegram messages are
handled by the existing handle_message path in telegram_bot.py.
"""

from tools.base import Tool, ToolMethod


class TelegramTool(Tool):

    name = "telegram"
    description = "Send messages to Zach via Telegram."

    def __init__(self, send_fn):
        """
        Args:
            send_fn: An async function that sends a message.
                Signature: async send_fn(text: str) -> None
                This is the same send_fn used by the old proactive system.
        """
        self._send_fn = send_fn
        self._last_result = None

    def get_context(self) -> str | None:
        """Telegram has no perception — messages come through handle_message."""
        return None

    def get_methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                name="send_message",
                description="Send a Telegram message to Zach.",
                tier="message",
                parameters={
                    "text": {
                        "type": "str",
                        "description": "The message text to send",
                        "required": True,
                    },
                },
            ),
        ]

    def execute(self, method_name: str, **kwargs) -> str:
        if method_name == "send_message":
            text = kwargs.get("text", "")
            if not text:
                return "Error: empty message text"
            # Store the message for async sending by the agent loop.
            # The agent loop is responsible for actually calling send_fn
            # because execute() is synchronous but send_fn is async.
            self._last_result = text
            return f"Message queued: {text[:80]}..."
        else:
            raise ValueError(f"Unknown method '{method_name}' on TelegramTool")

    def get_pending_message(self) -> str | None:
        """
        Retrieve and clear the last queued message.

        The agent loop calls this after execute() to get the message
        text and send it via the async send_fn.
        """
        msg = self._last_result
        self._last_result = None
        return msg
