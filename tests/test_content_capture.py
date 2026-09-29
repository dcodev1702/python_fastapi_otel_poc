"""
Content-capture tests for Foundry Learn Agent.

Author  : dcodev1702 & M365 Copilot / Cowork
Created : 2026-09-29

HTTP instrumentation records metadata only, so by default a trace shows THAT the app talked to OpenAI and Microsoft
Learn, not WHAT was said. With OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY the app records the
content on its spans in the GenAI semantic-convention format: system instructions, input and output messages, tool
definitions, and each tool call's arguments and result. These tests pin that format down, and check that nothing is
recorded while the switch is off.

Run:  pytest -q          (from the repository root; pytest.ini puts the root on the import path)
"""

# pylint: disable=missing-function-docstring  # each test's name states the behaviour it checks

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import SEARCH_RESULT_TEXT, final_brief, function_call, llm_response, wait_for_spans

import app as app_module

ASK_BODY = {"topic": "Microsoft Foundry", "paragraphs": 2, "links": 3}
CONTENT_KEYS = {
    "gen_ai.system_instructions", "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.tool.definitions"
}
USER_MESSAGE = {"role": "user", "parts": [{"type": "text", "content": app_module.build_user_prompt(**ASK_BODY)}]}
SEARCH_CALL = {"type": "tool_call", "id": "call_1", "name": "microsoft_docs_search", "arguments": {"query": "Foundry"}}


def finished(spans, name):
    """Spans with this name, oldest first, once the request's SERVER span has ended."""
    done = wait_for_spans(spans, lambda s: any(span.kind.name == "SERVER" for span in s))
    return sorted((span for span in done if span.name == name), key=lambda span: span.start_time)


def content(span, key):
    """A content attribute decoded from the JSON string the conventions prescribe on spans."""
    return json.loads(span.attributes[key])


def script_local_run(openai) -> None:
    """Two turns: the model asks for one search, then answers."""
    openai.script.extend(
        [
            llm_response([function_call("microsoft_docs_search", {"query": "Foundry"})]),
            llm_response([SimpleNamespace(type="message")], text=final_brief()),
        ]
    )


@pytest.mark.parametrize(
    "value, expected",
    [("", False), ("NO_CONTENT", False), ("EVENT_ONLY", False), ("SPAN_ONLY", True), ("span_and_event", True),
     ("true", True)],
)
def test_the_capture_switch_follows_the_standard_values(monkeypatch, value, expected):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", value)
    assert app_module.capture_content() is expected


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_by_default_spans_carry_genai_metadata_but_no_content(client, spans, learn, openai):
    script_local_run(openai)

    assert client.post("/ask", json=ASK_BODY).status_code == 200

    turns = finished(spans, "llm.turn")
    for turn in turns:
        assert turn.attributes["gen_ai.operation.name"] == "chat"
        assert turn.attributes["gen_ai.provider.name"] == "openai"
        assert turn.attributes["gen_ai.request.model"] == app_module.OPENAI_MODEL
        assert not CONTENT_KEYS & set(turn.attributes), "content is opt-in"
    assert [list(turn.attributes["gen_ai.response.finish_reasons"]) for turn in turns] == [["tool_call"], ["stop"]]
    (agent_run,) = finished(spans, "agent.run")
    assert agent_run.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert "gen_ai.input.messages" not in agent_run.attributes
    (tool_span,) = finished(spans, "mcp tools/call microsoft_docs_search")
    assert tool_span.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool_span.attributes["gen_ai.tool.name"] == "microsoft_docs_search"
    assert "gen_ai.tool.call.result" not in tool_span.attributes


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_captured_local_run_follows_the_genai_message_format(client, spans, learn, openai, monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY")
    script_local_run(openai)

    response = client.post("/ask", json=ASK_BODY)

    assert response.status_code == 200
    first, second = finished(spans, "llm.turn")
    assert content(first, "gen_ai.system_instructions") == [{"type": "text", "content": app_module.SYSTEM_PROMPT}]
    assert content(first, "gen_ai.input.messages") == [USER_MESSAGE], "turn 1 sees only the prompt"
    assert content(first, "gen_ai.output.messages") == [
        {"role": "assistant", "parts": [SEARCH_CALL], "finish_reason": "tool_call"}
    ]
    assert [tool["name"] for tool in content(first, "gen_ai.tool.definitions")] == [
        "microsoft_docs_search", "microsoft_code_sample_search", "microsoft_docs_fetch"
    ]
    tool_result = {"type": "tool_call_response", "id": "call_1", "response": SEARCH_RESULT_TEXT}
    assert content(second, "gen_ai.input.messages") == [
        USER_MESSAGE,
        {"role": "assistant", "parts": [SEARCH_CALL]},
        {"role": "tool", "parts": [tool_result]},
    ], "turn 2 sees the whole conversation so far, including what Learn returned"
    assert content(second, "gen_ai.output.messages") == [
        {"role": "assistant", "parts": [{"type": "text", "content": final_brief()}], "finish_reason": "stop"}
    ]

    (tool_span,) = finished(spans, "mcp tools/call microsoft_docs_search")
    assert content(tool_span, "gen_ai.tool.call.arguments") == {"query": "Foundry"}
    assert content(tool_span, "gen_ai.tool.call.result") == {"content": [{"type": "text", "text": SEARCH_RESULT_TEXT}]}

    (agent_run,) = finished(spans, "agent.run")
    assert content(agent_run, "gen_ai.input.messages") == [USER_MESSAGE]
    (answer,) = content(agent_run, "gen_ai.output.messages")
    assert json.loads(answer["parts"][0]["content"]) == response.json()["brief"], "the answer the caller received"


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_captured_hosted_run_shows_what_openai_did_with_learn(client, spans, learn, openai, monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY")
    openai.script.append(
        llm_response(
            [
                SimpleNamespace(type="mcp_list_tools", tools=[SimpleNamespace(name="microsoft_docs_search")]),
                SimpleNamespace(
                    type="mcp_call", id="mcp_1", name="microsoft_docs_search", arguments='{"query": "Foundry"}',
                    output="Learn says hello", error=None,
                ),
                SimpleNamespace(type="message"),
            ],
            text=final_brief(),
        )
    )

    assert client.post("/ask-hosted", json=ASK_BODY).status_code == 200

    (turn,) = finished(spans, "llm.turn")
    assert content(turn, "gen_ai.input.messages") == [USER_MESSAGE]
    assert content(turn, "gen_ai.tool.definitions") == [
        {"type": "mcp", "name": "microsoft_learn", "server_url": app_module.LEARN_MCP_URL}
    ]
    (message,) = content(turn, "gen_ai.output.messages")
    assert message["parts"] == [
        {"type": "server_tool_call", "id": "mcp_1", "name": "microsoft_docs_search",
         "server_tool_call": {"type": "mcp", "arguments": {"query": "Foundry"}}},
        {"type": "server_tool_call_response", "id": "mcp_1",
         "server_tool_call_response": {"type": "mcp", "output": "Learn says hello", "error": None}},
        {"type": "text", "content": final_brief()},
    ], "the Learn calls OpenAI made are not spans, but their data is in the response - and now in the trace"
    assert message["finish_reason"] == "stop"
