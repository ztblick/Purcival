"""
Tool interface — base classes for all agent tools.

Every tool the agent can use implements this interface. The agent loop
interacts with tools only through get_context(), get_methods(), and
execute(). It doesn't know or care whether a tool talks to Google,
a local file, or a web API.

To add a new tool:
    1. Create a class that inherits from Tool
    2. Implement get_context(), get_methods(), and execute()
    3. Register it in tools/__init__.py
    4. Enable it for a persona
"""

from dataclasses import dataclass, field


@dataclass
class ToolMethod:
    """
    Describes one action a tool can perform.

    The agent loop formats these into the LLM prompt so the model
    knows what actions are available. The permission system checks
    tiers before execution.
    """
    name: str               # e.g. "send_message"
    description: str        # For the LLM: "Send a Telegram message to the user"
    tier: str               # "observe", "message", "draft", "execute"
    parameters: dict = field(default_factory=dict)
    # parameters is a dict describing expected kwargs, e.g.:
    # {"text": {"type": "str", "description": "Message text", "required": True}}


class Tool:
    """
    Base class for all agent tools.

    Subclasses must implement get_context(), get_methods(), and execute().
    The agent loop calls these in a fixed order during each cycle.
    """

    name: str = ""
    description: str = ""
    enabled: bool = True

    def get_context(self) -> str | None:
        """
        Perception: return current state as a plain text string.

        Called when the agent cycle includes this tool. Should be fast
        and deterministic. Returns None if there's nothing relevant
        to report (lets the cycle skip this tool in the LLM prompt).

        The tool is responsible for diffing against its own last-known
        state internally. It only returns NEW or CHANGED information,
        not a full dump every time.

        This method should NEVER call an LLM. It reads external APIs,
        diffs against stored state, and formats the result as text.
        """
        return None

    def get_methods(self) -> list[ToolMethod]:
        """
        Return the actions this tool can perform, with tier tags.

        The agent loop includes these in the LLM prompt so the model
        knows what actions are available.
        """
        return []

    def execute(self, method_name: str, **kwargs) -> str:
        """
        Perform an action. Returns a human-readable result string.

        Only called after the agent loop has verified permissions
        through the code-level validation gate.

        Raises:
            ValueError: If the method_name doesn't exist on this tool.
        """
        raise ValueError(f"Unknown method '{method_name}' on tool '{self.name}'")
