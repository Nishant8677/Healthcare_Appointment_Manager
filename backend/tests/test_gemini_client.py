"""The Gemini client, driven by a mock transport.

The fixture below is a real response from the live API, captured before this client was
written — including the `thought` step the model emits because it reasons by default. That
detail is the whole reason these tests exist: the answer is *not* in `steps[0]`, and a client
that assumes it is works right up until reasoning is switched on.

Two Google documentation pages disagreed about the `response_format` shape. The one asserted
here is the one the live API actually accepted.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.llm import (
    GeminiLLMClient,
    LLMError,
    LLMRefusal,
    PreVisitSummary,
    build_llm_client,
)

# Verbatim from a live call, trimmed only of the opaque `signature` blob.
REAL_RESPONSE: dict[str, Any] = {
    "id": "v1_Chc3OWVIYXMzZEZwVGtnOFVQamJqb3VRaxIX",
    "object": "interaction",
    "model": "gemini-3.7-flash",
    "status": "completed",
    "service_tier": "standard",
    "usage": {"total_tokens": 105, "total_output_tokens": 1, "total_thought_tokens": 96},
    "steps": [
        {"type": "thought", "signature": "EtkDCtYDARFNMg..."},
        {
            "type": "model_output",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "urgency": "High",
                            "chief_complaint": "Chest tightness on exertion for 12 days",
                            "suggested_questions": [
                                "Does the tightness radiate to your jaw, neck or left arm?",
                                "Any breathlessness, nausea or sweating with it?",
                                "Does it settle with rest, and how long does an episode last?",
                            ],
                        }
                    ),
                }
            ],
        },
    ],
}


def client_returning(
    payload: dict[str, Any] | list[Any] | str, status_code: int = 200
) -> tuple[GeminiLLMClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(payload, str):
            return httpx.Response(status_code, content=payload)
        return httpx.Response(status_code, json=payload)

    client = GeminiLLMClient(
        api_key="test-key",
        model="gemini-3.7-flash",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )
    return client, seen


def response_with(status: str, errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
    body = dict(REAL_RESPONSE)
    body["status"] = status
    if errors is not None:
        body["errors"] = errors
    return body


async def generate(client: GeminiLLMClient) -> PreVisitSummary:
    return await client.generate(
        system="You are a clinical triage assistant.",
        user="Chest tightness on exertion for 12 days.",
        output_model=PreVisitSummary,
        max_tokens=2000,
    )


# --------------------------------------------------------------------------- the request


async def test_the_request_matches_what_the_live_api_accepts() -> None:
    client, seen = client_returning(REAL_RESPONSE)

    await generate(client)

    request = seen[0]
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert request.headers["x-goog-api-key"] == "test-key"

    body = json.loads(request.content)
    assert body["model"] == "gemini-3.7-flash"
    assert body["input"] == "Chest tightness on exertion for 12 days."
    assert body["generation_config"] == {"max_output_tokens": 2000}
    # The shape the API reference documented was rejected with a 400; this is the one that
    # works. Pinned so a "tidy-up" cannot quietly revert it.
    assert body["response_format"]["type"] == "text"
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["response_format"]["schema"]["required"] == [
        "urgency",
        "chief_complaint",
        "suggested_questions",
    ]


async def test_the_system_prompt_travels_as_a_system_instruction() -> None:
    """Not prepended to the input. The rule that patient text is data rather than commands is
    the prompt-injection defence, and it must not share a channel with the untrusted text."""
    client, seen = client_returning(REAL_RESPONSE)

    await generate(client)

    body = json.loads(seen[0].content)
    assert body["system_instruction"] == "You are a clinical triage assistant."
    assert "clinical triage assistant" not in body["input"]


# --------------------------------------------------------------------------- the response


async def test_it_reads_past_the_thought_step() -> None:
    """The bug this whole file exists for: the answer is in a later step, not `steps[0]`."""
    client, _ = client_returning(REAL_RESPONSE)

    summary = await generate(client)

    assert summary.chief_complaint == "Chest tightness on exertion for 12 days"
    assert len(summary.suggested_questions) == 3


async def test_capitalised_urgency_is_normalised() -> None:
    """The live model answered "High"; the database stores lower case."""
    client, _ = client_returning(REAL_RESPONSE)

    assert (await generate(client)).urgency == "high"


async def test_multiple_text_parts_are_joined() -> None:
    """`content` is a list, and nothing promises the JSON arrives in one piece."""
    body = json.dumps(
        {"urgency": "low", "chief_complaint": "Sore throat", "suggested_questions": []}
    )
    split = {
        **REAL_RESPONSE,
        "steps": [
            {"type": "thought", "signature": "..."},
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": body[:20]},
                    {"type": "text", "text": body[20:]},
                ],
            },
        ],
    }
    client, _ = client_returning(split)

    assert (await generate(client)).chief_complaint == "Sore throat"


async def test_non_text_parts_are_ignored() -> None:
    body = json.dumps({"urgency": "low", "chief_complaint": "Rash", "suggested_questions": []})
    mixed = {
        **REAL_RESPONSE,
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "image", "data": "..."}, {"type": "text", "text": body}],
            }
        ],
    }
    client, _ = client_returning(mixed)

    assert (await generate(client)).chief_complaint == "Rash"


# --------------------------------------------------------------------------- bad output


async def test_text_that_is_not_json_is_an_error_not_a_crash() -> None:
    """Reachable here and impossible on the Anthropic path: this provider returns JSON as a
    string, so decoding it is a step that can fail."""
    broken = {
        **REAL_RESPONSE,
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": "Sorry, I can't."}]}
        ],
    }
    client, _ = client_returning(broken)

    with pytest.raises(LLMError, match="not valid JSON"):
        await generate(client)


async def test_json_of_the_wrong_shape_is_rejected() -> None:
    wrong = {
        **REAL_RESPONSE,
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": '{"urgency": "high"}'}]}
        ],
    }
    client, _ = client_returning(wrong)

    with pytest.raises(LLMError, match="did not match the required shape"):
        await generate(client)


async def test_an_urgency_outside_the_three_levels_is_rejected() -> None:
    """A model that invents "critical" must not have it written into a clinical record."""
    invented = {
        **REAL_RESPONSE,
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "urgency": "critical",
                                "chief_complaint": "x",
                                "suggested_questions": [],
                            }
                        ),
                    }
                ],
            }
        ],
    }
    client, _ = client_returning(invented)

    with pytest.raises(LLMError, match="did not match the required shape"):
        await generate(client)


async def test_a_response_with_no_model_output_step_is_an_error() -> None:
    client, _ = client_returning({**REAL_RESPONSE, "steps": [{"type": "thought"}]})

    with pytest.raises(LLMError, match="no output text"):
        await generate(client)


# --------------------------------------------------------------------------- statuses


async def test_a_safety_refusal_is_terminal() -> None:
    """Retrying a refusal reaches the same answer and spends quota to do it."""
    client, _ = client_returning(
        response_with("failed", [{"code": "x", "message": "Blocked by safety policy"}])
    )

    with pytest.raises(LLMRefusal):
        await generate(client)


async def test_a_failure_that_is_not_a_refusal_stays_retryable() -> None:
    """Assuming an unfamiliar failure is permanent would discard summaries that would have
    succeeded on the next attempt."""
    client, _ = client_returning(
        response_with("failed", [{"code": "x", "message": "internal error"}])
    )

    with pytest.raises(LLMError):
        await generate(client)


@pytest.mark.parametrize("status", ["in_progress", "queued", "incomplete", "requires_action"])
async def test_any_unfinished_state_is_an_error(status: str) -> None:
    client, _ = client_returning(response_with(status))

    with pytest.raises(LLMError, match="did not complete"):
        await generate(client)


async def test_running_out_of_output_tokens_says_so() -> None:
    client, _ = client_returning(response_with("budget_exceeded"))

    with pytest.raises(LLMError, match="ran out of output tokens"):
        await generate(client)


# --------------------------------------------------------------------------- transport


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_provider_side_failures_are_retryable(status_code: int) -> None:
    client, _ = client_returning({"error": {"message": "try later"}}, status_code=status_code)

    with pytest.raises(LLMError, match=str(status_code)):
        await generate(client)


async def test_an_error_wrapped_in_an_array_still_reports_its_message() -> None:
    """The live endpoint returns `[{"error": {...}}]`, not the bare object its own reference
    documents. Captured from a real 400: without unwrapping the array every failure reads
    "unknown error", losing the one message that says what to fix."""
    client, _ = client_returning(
        [
            {
                "error": {
                    "code": 400,
                    "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                }
            }
        ],
        status_code=400,
    )

    with pytest.raises(LLMError, match="API key not valid"):
        await generate(client)


async def test_an_unrecognised_error_shape_does_not_crash() -> None:
    """Falling back to a vague message is acceptable; raising something other than LLMError
    from the error path is not, because it escapes the worker's retry handling."""
    client, _ = client_returning([["unexpected"]], status_code=400)

    with pytest.raises(LLMError, match="unknown error"):
        await generate(client)


async def test_a_network_failure_is_retryable() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = GeminiLLMClient(
        api_key="k",
        model="gemini-3.7-flash",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(explode),
    )

    with pytest.raises(LLMError, match="could not reach"):
        await generate(client)


async def test_the_error_never_carries_the_prompt_or_the_key() -> None:
    """`last_error` is stored on the summary row and logged. For a pre-visit summary the
    prompt is a patient's symptoms."""
    client, _ = client_returning(
        {"error": {"message": "bad request"}, "echo": "Chest tightness on exertion"},
        status_code=400,
    )

    with pytest.raises(LLMError) as caught:
        await generate(client)

    assert "Chest tightness" not in str(caught.value)
    assert "test-key" not in str(caught.value)


async def test_a_non_json_body_does_not_crash_the_worker() -> None:
    client, _ = client_returning("<html>gateway timeout</html>", status_code=200)

    with pytest.raises(LLMError, match="non-JSON"):
        await generate(client)


# --------------------------------------------------------------------------- wiring


def settings_for(provider: str, **extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:y@localhost/z",
        jwt_secret=SecretStr("a" * 40),
        llm_provider=provider,
        **extra,
    )


def test_gemini_without_a_key_fails_loudly() -> None:
    """Falling back to the stub would serve canned text as a clinical summary."""
    with pytest.raises(ValueError, match="LLM_API_KEY is not set"):
        build_llm_client(settings_for("gemini"))


def test_gemini_with_a_key_builds_the_gemini_client() -> None:
    client = build_llm_client(settings_for("gemini", llm_api_key=SecretStr("k")))

    assert isinstance(client, GeminiLLMClient)


def test_each_provider_gets_its_own_default_model() -> None:
    """Sharing one default across providers means switching provider and forgetting the model
    sends an Anthropic model id to Google — a 400 on the first summary."""
    assert settings_for("gemini", llm_api_key=SecretStr("k")).llm_model == "gemini-3.7-flash"
    assert settings_for("anthropic", llm_api_key=SecretStr("k")).llm_model == "claude-opus-5"


def test_an_explicit_model_is_always_honoured() -> None:
    settings = settings_for("gemini", llm_api_key=SecretStr("k"), llm_model="gemini-3.6-flash")

    assert settings.llm_model == "gemini-3.6-flash"
