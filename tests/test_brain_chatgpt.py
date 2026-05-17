"""
Tests for the ChatGPT provider and the task-based dispatch in brain.py.

Offline tests verify (no API calls made):
    - ChatGPT handler sends system prompt as first message
    - Per-task model selection for all three providers
    - Fallback to Ollama on missing API key
    - Fallback to Ollama on unknown provider
    - Lazy client initialization

Smoke tests (require OPENAI_API_KEY env var) verify:
    - Real API call returns a non-empty string for chat task
    - Real API call returns a non-empty string for reasoning task
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure project root is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import brain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_openai_response(text: str) -> MagicMock:
    """Build a mock that looks like an openai ChatCompletion response."""
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_mock_anthropic_response(text: str) -> MagicMock:
    """Build a mock that looks like an anthropic Messages response."""
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


# ---------------------------------------------------------------------------
# ChatGPT handler tests
# ---------------------------------------------------------------------------

def test_chatgpt_sends_system_as_first_message():
    """System prompt must be prepended as role:system before user messages."""
    print("  test_chatgpt_sends_system_as_first_message...", end=" ")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_openai_response("hi")

    with patch.object(brain, "_chatgpt_client", mock_client):
        brain._ask_chatgpt(
            messages=[{"role": "user", "content": "hello"}],
            system="You are helpful.",
            max_tokens=100,
            model="gpt-test",
        )

    create_call = mock_client.chat.completions.create.call_args
    messages_sent = create_call.kwargs["messages"]
    assert messages_sent[0] == {"role": "system", "content": "You are helpful."}
    assert messages_sent[1] == {"role": "user", "content": "hello"}

    print("PASS")


def test_chatgpt_returns_response_text():
    """_ask_chatgpt should return the text from choices[0].message.content."""
    print("  test_chatgpt_returns_response_text...", end=" ")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_openai_response(
        "response text"
    )

    with patch.object(brain, "_chatgpt_client", mock_client):
        result = brain._ask_chatgpt(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            max_tokens=100,
            model="gpt-test",
        )

    assert result == "response text"
    print("PASS")


def test_chatgpt_passes_model_and_max_tokens():
    """Model and max_completion_tokens must be forwarded to the API call."""
    print("  test_chatgpt_passes_model_and_max_tokens...", end=" ")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_openai_response("ok")

    with patch.object(brain, "_chatgpt_client", mock_client):
        brain._ask_chatgpt(
            messages=[],
            system="s",
            max_tokens=512,
            model="gpt-5.4-mini",
        )

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5.4-mini"
    assert kwargs["max_completion_tokens"] == 512

    print("PASS")


# ---------------------------------------------------------------------------
# Per-task model selection
# ---------------------------------------------------------------------------

def test_chatgpt_uses_chat_model_for_chat_task():
    """brain.ask with task='chat' must use CHATGPT_CHAT_MODEL."""
    print("  test_chatgpt_uses_chat_model_for_chat_task...", end=" ")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_openai_response("ok")

    with patch.object(brain, "_chatgpt_client", mock_client), \
         patch.object(config, "CHATGPT_CHAT_MODEL", "gpt-chat-sentinel"), \
         patch.object(config, "DEFAULT_PROVIDER", "chatgpt"):
        brain.ask([{"role": "user", "content": "hi"}], system="s", task="chat")

    model_used = mock_client.chat.completions.create.call_args.kwargs["model"]
    assert model_used == "gpt-chat-sentinel"
    print("PASS")


def test_chatgpt_uses_summary_model_for_summary_task():
    """brain.ask with task='summary' must use CHATGPT_SUMMARY_MODEL."""
    print("  test_chatgpt_uses_summary_model_for_summary_task...", end=" ")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_openai_response("ok")

    with patch.object(brain, "_chatgpt_client", mock_client), \
         patch.object(config, "CHATGPT_SUMMARY_MODEL", "gpt-summary-sentinel"), \
         patch.object(config, "DEFAULT_PROVIDER", "chatgpt"):
        brain.ask([{"role": "user", "content": "hi"}], system="s", task="summary")

    model_used = mock_client.chat.completions.create.call_args.kwargs["model"]
    assert model_used == "gpt-summary-sentinel"
    print("PASS")


def test_chatgpt_uses_reasoning_model_for_reasoning_task():
    """brain.ask with task='reasoning' must use CHATGPT_REASONING_MODEL."""
    print("  test_chatgpt_uses_reasoning_model_for_reasoning_task...", end=" ")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_openai_response("ok")

    with patch.object(brain, "_chatgpt_client", mock_client), \
         patch.object(config, "CHATGPT_REASONING_MODEL", "gpt-reasoning-sentinel"), \
         patch.object(config, "DEFAULT_PROVIDER", "chatgpt"):
        brain.ask([{"role": "user", "content": "hi"}], system="s", task="reasoning")

    model_used = mock_client.chat.completions.create.call_args.kwargs["model"]
    assert model_used == "gpt-reasoning-sentinel"
    print("PASS")


def test_claude_uses_per_task_models():
    """Claude chat/summary/reasoning tasks must each use their configured model."""
    print("  test_claude_uses_per_task_models...", end=" ")

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_mock_anthropic_response("ok")

    task_model_pairs = [
        ("chat",      "claude-chat-sentinel"),
        ("summary",   "claude-summary-sentinel"),
        ("reasoning", "claude-reasoning-sentinel"),
    ]

    for task, sentinel in task_model_pairs:
        with patch.object(brain, "_claude_client", mock_client), \
             patch.object(config, "CLAUDE_CHAT_MODEL",      "claude-chat-sentinel"), \
             patch.object(config, "CLAUDE_SUMMARY_MODEL",   "claude-summary-sentinel"), \
             patch.object(config, "CLAUDE_REASONING_MODEL", "claude-reasoning-sentinel"), \
             patch.object(config, "DEFAULT_PROVIDER", "claude"):
            brain.ask([{"role": "user", "content": "hi"}], system="s", task=task)

        model_used = mock_client.messages.create.call_args.kwargs["model"]
        assert model_used == sentinel, f"task '{task}': expected {sentinel}, got {model_used}"

    print("PASS")


def test_ollama_uses_per_task_models():
    """Ollama chat/summary/reasoning tasks must each use their configured model."""
    print("  test_ollama_uses_per_task_models...", end=" ")

    task_model_pairs = [
        ("chat",      "ollama-chat-sentinel"),
        ("summary",   "ollama-summary-sentinel"),
        ("reasoning", "ollama-reasoning-sentinel"),
    ]

    for task, sentinel in task_model_pairs:
        with patch("brain.requests") as mock_requests, \
             patch.object(config, "OLLAMA_CHAT_MODEL",      "ollama-chat-sentinel"), \
             patch.object(config, "OLLAMA_SUMMARY_MODEL",   "ollama-summary-sentinel"), \
             patch.object(config, "OLLAMA_REASONING_MODEL", "ollama-reasoning-sentinel"), \
             patch.object(config, "DEFAULT_PROVIDER", "ollama"):

            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "ok"}}]
            }
            mock_requests.post.return_value = mock_response

            brain.ask([{"role": "user", "content": "hi"}], system="s", task=task)

        payload = mock_requests.post.call_args.kwargs["json"]
        assert payload["model"] == sentinel, (
            f"task '{task}': expected {sentinel}, got {payload['model']}"
        )

    print("PASS")


# ---------------------------------------------------------------------------
# Fallback behaviour
# ---------------------------------------------------------------------------

def test_falls_back_to_ollama_on_missing_chatgpt_key():
    """Missing OPENAI_API_KEY should fall back to Ollama, not raise."""
    print("  test_falls_back_to_ollama_on_missing_chatgpt_key...", end=" ")

    with patch("brain.requests") as mock_requests, \
         patch.object(brain, "_chatgpt_client", None), \
         patch.object(config, "OPENAI_API_KEY", ""), \
         patch.object(config, "DEFAULT_PROVIDER", "chatgpt"):

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "fallback response"}}]
        }
        mock_requests.post.return_value = mock_response

        result = brain.ask(
            [{"role": "user", "content": "hi"}],
            system="s",
            task="chat",
        )

    assert result == "fallback response"
    assert mock_requests.post.called
    print("PASS")


def test_falls_back_to_ollama_on_missing_claude_key():
    """Missing ANTHROPIC_API_KEY should fall back to Ollama, not raise."""
    print("  test_falls_back_to_ollama_on_missing_claude_key...", end=" ")

    with patch("brain.requests") as mock_requests, \
         patch.object(brain, "_claude_client", None), \
         patch.object(config, "ANTHROPIC_API_KEY", ""), \
         patch.object(config, "DEFAULT_PROVIDER", "claude"):

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "fallback response"}}]
        }
        mock_requests.post.return_value = mock_response

        result = brain.ask(
            [{"role": "user", "content": "hi"}],
            system="s",
            task="chat",
        )

    assert result == "fallback response"
    assert mock_requests.post.called
    print("PASS")


def test_falls_back_to_ollama_on_unknown_provider():
    """An unrecognised provider name should silently fall back to Ollama."""
    print("  test_falls_back_to_ollama_on_unknown_provider...", end=" ")

    with patch("brain.requests") as mock_requests, \
         patch.object(config, "DEFAULT_PROVIDER", "unknown_provider"):

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "fallback"}}]
        }
        mock_requests.post.return_value = mock_response

        result = brain.ask(
            [{"role": "user", "content": "hi"}],
            system="s",
        )

    assert result == "fallback"
    print("PASS")


# ---------------------------------------------------------------------------
# Lazy client init
# ---------------------------------------------------------------------------

def test_chatgpt_client_not_initialized_at_import():
    """The ChatGPT client should not be created until the first call."""
    print("  test_chatgpt_client_not_initialized_at_import...", end=" ")

    original = brain._chatgpt_client
    try:
        brain._chatgpt_client = None
        # Just importing / referencing brain should not have created the client
        assert brain._chatgpt_client is None
    finally:
        brain._chatgpt_client = original

    print("PASS")


def test_ensure_chatgpt_raises_without_key():
    """_ensure_chatgpt() must raise RuntimeError when OPENAI_API_KEY is empty."""
    print("  test_ensure_chatgpt_raises_without_key...", end=" ")

    original_client = brain._chatgpt_client
    try:
        brain._chatgpt_client = None
        with patch.object(config, "OPENAI_API_KEY", ""):
            try:
                brain._ensure_chatgpt()
                assert False, "Should have raised RuntimeError"
            except RuntimeError as e:
                assert "OPENAI_API_KEY" in str(e)
    finally:
        brain._chatgpt_client = original_client

    print("PASS")


def test_ask_raises_without_system_prompt():
    """brain.ask() must raise ValueError when system is None."""
    print("  test_ask_raises_without_system_prompt...", end=" ")

    try:
        brain.ask([{"role": "user", "content": "hi"}])
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "system prompt" in str(e).lower()

    print("PASS")


# ---------------------------------------------------------------------------
# Smoke tests (require OPENAI_API_KEY)
# ---------------------------------------------------------------------------

def _chatgpt_available() -> bool:
    return (
        os.getenv("PURCIVAL_RUN_LIVE_TESTS") == "1"
        and bool(os.getenv("OPENAI_API_KEY"))
    )


@pytest.mark.skipif(
    not _chatgpt_available(),
    reason="live OpenAI smoke test; set PURCIVAL_RUN_LIVE_TESTS=1 and OPENAI_API_KEY to run",
)
def test_smoke_chatgpt_chat():
    """Real API call for task='chat' should return a non-empty string."""
    print("  test_smoke_chatgpt_chat...", end=" ")

    # Reset client so it initialises fresh with the real key
    brain._chatgpt_client = None

    result = brain.ask(
        [{"role": "user", "content": "Reply with the single word: hello"}],
        system="You are a test assistant. Follow instructions exactly.",
        provider="chatgpt",
        task="chat",
        max_tokens=20,
    )

    assert isinstance(result, str) and len(result.strip()) > 0
    print(f"PASS (response: '{result.strip()[:40]}')")


@pytest.mark.skipif(
    not _chatgpt_available(),
    reason="live OpenAI smoke test; set PURCIVAL_RUN_LIVE_TESTS=1 and OPENAI_API_KEY to run",
)
def test_smoke_chatgpt_reasoning():
    """Real API call for task='reasoning' should return a non-empty string."""
    print("  test_smoke_chatgpt_reasoning...", end=" ")

    brain._chatgpt_client = None

    result = brain.ask(
        [{"role": "user", "content": "What is 2 + 2?"}],
        system="You are a test assistant.",
        provider="chatgpt",
        task="reasoning",
        max_tokens=20,
    )

    assert isinstance(result, str) and len(result.strip()) > 0
    print(f"PASS (response: '{result.strip()[:40]}')")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\nRunning brain ChatGPT tests...\n")

    offline_tests = [
        test_chatgpt_sends_system_as_first_message,
        test_chatgpt_returns_response_text,
        test_chatgpt_passes_model_and_max_tokens,
        test_chatgpt_uses_chat_model_for_chat_task,
        test_chatgpt_uses_summary_model_for_summary_task,
        test_chatgpt_uses_reasoning_model_for_reasoning_task,
        test_claude_uses_per_task_models,
        test_ollama_uses_per_task_models,
        test_falls_back_to_ollama_on_missing_chatgpt_key,
        test_falls_back_to_ollama_on_missing_claude_key,
        test_falls_back_to_ollama_on_unknown_provider,
        test_chatgpt_client_not_initialized_at_import,
        test_ensure_chatgpt_raises_without_key,
        test_ask_raises_without_system_prompt,
    ]

    smoke_tests = [
        test_smoke_chatgpt_chat,
        test_smoke_chatgpt_reasoning,
    ]

    passed = failed = skipped = 0

    print("  Offline tests:\n")
    for test in offline_tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL — {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()

    if _chatgpt_available():
        print("  Smoke tests (live tests enabled):\n")
        for test in smoke_tests:
            try:
                test()
                passed += 1
            except Exception as e:
                print(f"FAIL — {e}")
                import traceback
                traceback.print_exc()
                failed += 1
    else:
        skipped = len(smoke_tests)
        print(
            f"  Skipping {skipped} smoke test(s) — OPENAI_API_KEY not set.\n"
            f"  To run: set OPENAI_API_KEY in your .env and re-run.\n"
        )

    print(f"\n{'='*40}")
    parts = [f"{passed} passed", f"{failed} failed"]
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"  {', '.join(parts)}")
    print(f"{'='*40}\n")

    sys.exit(0 if failed == 0 else 1)
