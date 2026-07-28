"""The streaming path must be guardrailed, like chat() already was.

Streaming is the *primary* path in app/views/chat.py — chat() is only the
fallback when streaming fails — so while chat_stream() was unguardrailed the
majority of live responses reached the UI unchecked. These tests pin the
wiring so it cannot silently regress to that state.

They exercise `apply_guardrail()` directly (the seam both request paths now
share) rather than driving Streamlit, and stub `agent.guardrail.check` so no
Foundation Model judge call is made.
"""

import sys
import types

import pytest

import app.agent_client as agent_client


class FakeResult:
    def __init__(self, allowed, rule_triggered=None, judge_verdict=None):
        self.allowed = allowed
        self.rule_triggered = rule_triggered
        self.judge_verdict = judge_verdict
        self.safe_fallback_text = "I can't answer that safely — please ask your pharmacist."


@pytest.fixture
def stub_guardrail(monkeypatch):
    """Replace the guardrail's two entry points and capture block logging."""
    logged = []

    def install(allowed):
        import agent.guardrail as guardrail

        monkeypatch.setattr(guardrail, "check", lambda text, trace=None: FakeResult(
            allowed, rule_triggered="llm_judge", judge_verdict="YES"
        ))
        monkeypatch.setattr(guardrail, "tool_trace_from_responses_output", lambda items: [])

        fake_db = types.ModuleType("app.db")
        fake_db.log_guardrail_block = lambda **kw: logged.append(kw)
        monkeypatch.setitem(sys.modules, "app.db", fake_db)
        return logged

    return install


def test_blocked_response_is_replaced_wholesale(stub_guardrail):
    """Citations and pending_confirmation must not survive a block.

    A surviving confirmation card would let the UI render a schedule-change
    button under text the safety layer just refused to stand behind.
    """
    logged = stub_guardrail(allowed=False)
    parsed = {
        "text": "Take 1000mg instead.",
        "citations": [{"chunk_id": "abc"}],
        "pending_confirmation": {"action": "create_from_extraction"},
    }

    result = agent_client.apply_guardrail(parsed, [], "patient-1")

    assert result["text"] != parsed["text"]
    assert result["citations"] == []
    assert result["pending_confirmation"] is None
    assert len(logged) == 1, "a block must be recorded, not silently swallowed"
    assert logged[0]["patient_id"] == "patient-1"


def test_allowed_response_passes_through_untouched(stub_guardrail):
    logged = stub_guardrail(allowed=True)
    parsed = {
        "text": "Metformin is taken twice daily.",
        "citations": [{"chunk_id": "abc"}],
        "pending_confirmation": None,
    }

    result = agent_client.apply_guardrail(parsed, [], "patient-1")

    assert result == parsed
    assert logged == [], "nothing to log when nothing was blocked"


def test_block_excerpt_is_truncated(stub_guardrail):
    """log_guardrail_block stores an excerpt, not an unbounded response."""
    logged = stub_guardrail(allowed=False)

    agent_client.apply_guardrail(
        {"text": "x" * 5000, "citations": [], "pending_confirmation": None}, [], "patient-1"
    )

    assert len(logged[0]["model_output_excerpt"]) == 500


def test_streaming_call_site_applies_the_guardrail():
    """The Chat view's streaming branch must call apply_guardrail().

    Asserted against the source because driving the real branch needs a live
    Streamlit render loop and an agent endpoint; this at least fails loudly if
    the call is deleted, which is the regression that matters.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "views" / "chat.py").read_text()
    streaming_fn = source.split("def _render_streaming_response")[1].split("\ndef ")[0]
    assert "apply_guardrail" in streaming_fn, (
        "streaming is the primary chat path — it must not render unchecked model text"
    )


def test_chat_and_stream_share_one_guardrail_implementation():
    """chat() must delegate rather than keep a second copy of the logic."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "agent_client.py").read_text()
    chat_fn = source.split("\ndef chat(")[1].split("\ndef ")[0]
    assert "apply_guardrail" in chat_fn
    assert "log_guardrail_block" not in chat_fn, "block logging belongs in the shared seam"
