"""
The Claude integration, without touching the network.

Two classes of bug live here, and neither shows up until production:

* **A request shaped for the wrong model.** `effort` and adaptive thinking are
  rejected outright by Haiku 4.5 and Sonnet 4.5. Since the model is an
  environment variable chosen for cost, a hard-coded request shape turns a
  cheaper model into a 400 on every call.
* **A failure that is not survived.** AI is an enhancement to a request, never
  the point of it. A document upload whose analysis fails must still store the
  document, so every path here returns a usable shape rather than raising.
"""
import json
from unittest.mock import patch

import anthropic
import pytest

from app.services import ai_service as ai


class Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class Response:
    def __init__(self, blocks, stop_reason="end_turn", stop_details=None):
        self.content, self.stop_reason, self.stop_details = blocks, stop_reason, stop_details


@pytest.fixture
def call(monkeypatch):
    """Capture the request that would go to Claude, and script the reply."""
    monkeypatch.setattr(ai.settings, "ANTHROPIC_API_KEY", "sk-ant-test", raising=False)
    captured = {}
    scripted = {"response": Response([Block("text", "{}")]), "raises": None}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        if scripted["raises"] is not None:
            raise scripted["raises"]
        return scripted["response"]

    class FakeClient:
        class beta:
            class messages:
                create = staticmethod(fake_create)

    monkeypatch.setattr(ai, "client", lambda: FakeClient())
    return captured, scripted


# ------------------------------------------------------- request shaping
class TestModelAwareRequests:
    @pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-5"])
    @pytest.mark.asyncio
    async def test_older_models_get_no_effort_or_thinking(self, call, monkeypatch, model):
        """Regression: `effort` is rejected by these models, so sending it made
        every call fail the moment someone picked a cheaper model."""
        captured, _ = call
        monkeypatch.setattr(ai.settings, "ANTHROPIC_MODEL", model, raising=False)

        await ai.case_guidance(case_context={})

        assert "output_config" not in captured
        assert "thinking" not in captured
        assert "betas" not in captured

    @pytest.mark.asyncio
    async def test_current_models_get_adaptive_thinking_and_effort(self, call, monkeypatch):
        captured, _ = call
        monkeypatch.setattr(ai.settings, "ANTHROPIC_MODEL", "claude-opus-5", raising=False)

        await ai.case_guidance(case_context={})

        assert captured["thinking"] == {"type": "adaptive"}
        assert captured["output_config"]["effort"] == ai.settings.ANTHROPIC_EFFORT
        # Passports and identity papers can trip a safety classifier; a fallback
        # answers instead of returning nothing.
        assert "server-side-fallback-2026-07-01" in captured["betas"]

    @pytest.mark.asyncio
    async def test_sonnet_5_thinks_but_has_no_fallbacks(self, call, monkeypatch):
        captured, _ = call
        monkeypatch.setattr(ai.settings, "ANTHROPIC_MODEL", "claude-sonnet-5", raising=False)

        await ai.case_guidance(case_context={})

        assert "thinking" in captured
        assert "betas" not in captured

    @pytest.mark.asyncio
    async def test_a_per_call_effort_override_is_used(self, call, monkeypatch):
        """The document checklist runs on every new request; it does not need
        the same thinking depth as case strategy."""
        captured, _ = call
        monkeypatch.setattr(ai.settings, "ANTHROPIC_MODEL", "claude-opus-5", raising=False)

        await ai.suggest_required_documents(
            visa_type="Skilled Worker", destination_country="UK", summary="x")

        assert captured["output_config"]["effort"] == "low"


# ---------------------------------------------------------- attachments
class TestDocumentBlocks:
    def test_an_image_is_sent_as_an_image(self):
        block = ai._document_block(b"png-bytes", "image/png")
        assert block["type"] == "image"
        assert block["source"]["media_type"] == "image/png"

    def test_a_pdf_is_sent_as_a_document(self):
        """The upgrade over the previous provider, which could only send images
        and so answered about a bank statement from its filename."""
        block = ai._document_block(b"%PDF-1.4", "application/pdf")
        assert block["type"] == "document"
        assert block["source"]["media_type"] == "application/pdf"

    def test_heic_cannot_be_sent(self):
        """Uploads accept HEIC because iPhones produce it; the API does not take
        it. Sending it anyway would be a guaranteed 400."""
        assert ai._document_block(b"heic", "image/heic") is None

    def test_a_file_too_large_to_encode_is_not_sent(self):
        """A request caps at 32 MB and base64 inflates by 4/3, so the largest
        permitted upload (25 MB) does not fit."""
        assert ai._document_block(b"x" * (ai.MAX_INLINE_BYTES + 1), "image/png") is None

    @pytest.mark.asyncio
    async def test_an_unsendable_file_is_still_analysed_from_its_name(self, call):
        _, scripted = call
        scripted["response"] = Response([Block("text", '{"recommendation": "manual_review"}')])

        result = await ai.analyze_document(
            file_bytes=b"heic", mime_type="image/heic", document_name="photo.heic")

        assert result["recommendation"] == "manual_review"

    @pytest.mark.asyncio
    async def test_the_file_is_sent_before_the_question(self, call):
        """Claude reads a document better when it arrives ahead of the prompt
        about it."""
        captured, _ = call
        await ai.analyze_document(
            file_bytes=b"%PDF", mime_type="application/pdf", document_name="p.pdf")

        content = captured["messages"][0]["content"]
        assert content[0]["type"] == "document"
        assert content[1]["type"] == "text"


# ------------------------------------------------------ reading the reply
class TestResponseHandling:
    @pytest.mark.asyncio
    async def test_thinking_blocks_are_not_mistaken_for_the_answer(self, call):
        """With thinking on, `content` holds reasoning as well as text - so the
        old `content[0]` habit reads the reasoning instead of the reply."""
        _, scripted = call
        scripted["response"] = Response([
            Block("thinking", "let me work through this"),
            Block("text", '{"summary": "the real answer"}'),
        ])

        result = await ai.case_guidance(case_context={})

        assert result["summary"] == "the real answer"

    @pytest.mark.asyncio
    async def test_json_wrapped_in_a_fence_is_still_parsed(self, call):
        _, scripted = call
        scripted["response"] = Response([Block("text", '```json\n{"risk_flags": ["a"]}\n```')])

        assert (await ai.case_guidance(case_context={}))["risk_flags"] == ["a"]

    @pytest.mark.asyncio
    async def test_a_truncated_answer_is_reported(self, call, caplog, monkeypatch):
        """ANTHROPIC_MAX_TOKENS is an environment variable someone can set too
        low. A cut-off answer looks complete to the caller - the assistant just
        stops mid-thought - so the log is the only place it can show up."""
        monkeypatch.setattr(ai.settings, "ANTHROPIC_MODEL", "claude-haiku-4-5", raising=False)
        _, scripted = call
        scripted["response"] = Response([Block("text", "half an ans")],
                                        stop_reason="max_tokens")

        with caplog.at_level("WARNING"):
            await ai.case_guidance(case_context={})

        assert any("max_tokens" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_refusal_is_not_read_as_content(self, call):
        """A declined request is a 200 with nothing useful in it."""
        _, scripted = call
        scripted["response"] = Response([], stop_reason="refusal")

        result = await ai.analyze_document(
            file_bytes=b"%PDF", mime_type="application/pdf", document_name="p.pdf")

        assert result["recommendation"] == "manual_review"
        assert "failed" in result["summary"]


# --------------------------------------------------------- never raising
class TestFailuresAreSurvived:
    @pytest.mark.parametrize("error", [
        anthropic.APIConnectionError(request=None),
        RuntimeError("something unforeseen"),
    ])
    @pytest.mark.asyncio
    async def test_document_analysis_degrades_instead_of_raising(self, call, error):
        """The upload must still be stored when the analysis cannot run."""
        _, scripted = call
        scripted["raises"] = error

        result = await ai.analyze_document(
            file_bytes=b"%PDF", mime_type="application/pdf", document_name="p.pdf")

        assert result["recommendation"] == "manual_review"
        assert result["extracted_fields"] == {}

    @pytest.mark.asyncio
    async def test_the_assistant_answers_even_when_claude_does_not(self, call):
        _, scripted = call
        scripted["raises"] = RuntimeError("down")

        reply = await ai.assistant_reply(history=[], message="hello")

        assert "unavailable" in reply.lower()

    @pytest.mark.asyncio
    async def test_the_checklist_falls_back_to_empty(self, call):
        _, scripted = call
        scripted["raises"] = RuntimeError("down")

        assert await ai.suggest_required_documents(
            visa_type="x", destination_country="y", summary="z") == []

    @pytest.mark.asyncio
    async def test_no_api_key_is_reported_not_crashed(self, monkeypatch):
        monkeypatch.setattr(ai.settings, "ANTHROPIC_API_KEY", "", raising=False)

        result = await ai.analyze_document(
            file_bytes=b"x", mime_type="image/png", document_name="d.png")

        assert "ANTHROPIC_API_KEY" in result["summary"]


# ----------------------------------------------------------- the prompt
class TestAssistantPrompt:
    @pytest.mark.asyncio
    async def test_workspace_data_is_a_separate_system_block(self, call):
        """Split on purpose: the model is told above how to treat this block, so
        the records arrive as data rather than as a new instruction."""
        captured, _ = call
        await ai.assistant_reply(
            history=[], message="hi", context={"role": "client", "cases": []})

        system = captured["system"]
        assert len(system) == 2
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert "WORKSPACE DATA" in system[1]["text"]

    @pytest.mark.asyncio
    async def test_non_conversational_history_turns_are_dropped(self, call):
        """Claude takes only user and assistant turns; a stored `system` row
        would be rejected for the whole conversation."""
        captured, _ = call
        await ai.assistant_reply(
            history=[{"role": "system", "content": "x"},
                     {"role": "user", "content": "earlier"}],
            message="now")

        assert [m["role"] for m in captured["messages"]] == ["user", "user"]

    def test_each_role_gets_its_own_instructions(self):
        """A client must never be handed the internal case-handling advice a
        consultant asks for."""
        assert ai.assistant_prompt("client") != ai.assistant_prompt("consultant_owner")
        assert "applicant themselves" in ai.assistant_prompt("client")
