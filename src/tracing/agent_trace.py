"""
Unified trace system for all pipeline agents.

Every agent wraps its LLM invocation inside a PipelineTrace span.
TraceCallback is a LangChain callback that captures tool calls, tool results,
LLM reasoning, and token counts — replacing the per-agent _AgentTraceLogger.

Typical usage inside an agent node:
    trace = PipelineTrace(session_id=..., test_name=..., project=...)
    with trace.start_span("detection", model="MiniMax-M2.7") as span:
        cb = TraceCallback(span)
        result = agent.invoke(..., config={"callbacks": [cb], "recursion_limit": 40})
    # span is now closed; span.token_usage, span.to_dict() are available
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

_MAX_LLM_TEXT  = 500   # chars of post-<think> action text stored per LLM call
_MAX_TOOL_IO   = 600   # chars of tool input / output stored in trace


def _clean_llm_text(text: str) -> str:
    """Strip <think> blocks and keep only the model's action / final answer."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if len(cleaned) > _MAX_LLM_TEXT:
        return cleaned[:_MAX_LLM_TEXT] + f" …[+{len(cleaned)-_MAX_LLM_TEXT} chars]"
    return cleaned


def _truncate(text: str | None, max_len: int = _MAX_TOOL_IO) -> str | None:
    """Truncate a string for trace storage."""
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len] + f" …[+{len(text)-max_len} chars]"


@dataclass
class ToolEvent:
    tool: str
    input: str
    output: str | None = None
    error: str | None = None


@dataclass
class LLMEvent:
    call_index: int
    text: str
    input_tokens: int
    output_tokens: int


@dataclass
class AgentSpan:
    """One agent's complete invocation record within a pipeline run."""

    agent: str
    model: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    tool_events: list[ToolEvent] = field(default_factory=list)
    llm_events: list[LLMEvent] = field(default_factory=list)
    success: bool = False
    error: str | None = None
    _llm_call_index: int = field(default=0, repr=False)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)

    @property
    def token_usage(self) -> dict[str, Any]:
        total_in  = sum(e.input_tokens  for e in self.llm_events)
        total_out = sum(e.output_tokens for e in self.llm_events)
        return {
            "model":         self.model,
            "input_tokens":  total_in,
            "output_tokens": total_out,
            "total_tokens":  total_in + total_out,
            "llm_calls":     len(self.llm_events),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent":            self.agent,
            "model":            self.model,
            "started_at":       self.started_at,
            "finished_at":      self.finished_at,
            "duration_seconds": self.duration_seconds,
            "success":          self.success,
            "error":            self.error,
            "token_usage":      self.token_usage,
            "tool_call_count":  len(self.tool_events),
            "tool_events": [
                {
                    "tool":   e.tool,
                    "input":  _truncate(e.input),
                    "output": _truncate(e.output),
                    "error":  e.error,
                }
                for e in self.tool_events
            ],
            "llm_events": [
                {
                    "call":          e.call_index,
                    "action":        _clean_llm_text(e.text),
                    "input_tokens":  e.input_tokens,
                    "output_tokens": e.output_tokens,
                }
                for e in self.llm_events
            ],
        }

    def as_legacy_agent_trace(self) -> list[dict[str, Any]]:
        """
        Flatten span events into the old agent_trace list format so callers that
        read state['agent_trace'] keep working without changes.
        """
        events: list[dict[str, Any]] = []
        for evt in self.tool_events:
            events.append({"type": "tool_call", "tool": evt.tool, "input": evt.input})
            if evt.output is not None:
                events.append({"type": "tool_result", "output": evt.output})
            if evt.error is not None:
                events.append({"type": "tool_error", "error": evt.error})
        for evt in self.llm_events:
            events.append({
                "type":          "llm_output",
                "llm_call":      evt.call_index,
                "text":          evt.text,
                "input_tokens":  evt.input_tokens,
                "output_tokens": evt.output_tokens,
            })
        return events


class TraceCallback(BaseCallbackHandler):
    """
    LangChain callback that writes all events into an AgentSpan.
    Shared by every agent — replaces per-agent _AgentTraceLogger implementations.
    Log prefix uses the agent name so multi-agent logs are distinguishable.
    """

    def __init__(self, span: AgentSpan) -> None:
        super().__init__()
        self._span = span

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs: Any) -> None:
        tool_name = serialized.get("name", "unknown_tool")
        agent_name = self._span.agent.upper()
        inp_clean = input_str.strip()
        preview = inp_clean[:150] + (" …" if len(inp_clean) > 150 else "")
        logger.debug("[%s Tool] %s(%s)", agent_name, tool_name, preview)
        self._span.tool_events.append(ToolEvent(tool=tool_name, input=inp_clean))

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        output_str = output.content if hasattr(output, "content") else str(output)
        agent_name = self._span.agent.upper()
        preview = output_str if len(output_str) <= 300 else output_str[:300] + " …[truncated]"
        logger.debug("[%s Result] %s", agent_name, preview.replace("\n", " "))
        if self._span.tool_events:
            self._span.tool_events[-1].output = output_str

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        err = str(error)
        agent_name = self._span.agent.upper()
        logger.error("[%s ToolError] %s", agent_name, err)
        if self._span.tool_events:
            self._span.tool_events[-1].error = err

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self._span._llm_call_index += 1
        agent_name = self._span.agent.upper()
        text = ""
        try:
            text = response.generations[0][0].text.strip()
            # Clean <think> tags so the terminal output isn't flooded with raw reasoning
            action_preview = _clean_llm_text(text)
            logger.debug("[%s LLM #%d] %s", agent_name, self._span._llm_call_index, action_preview.replace("\n", " "))
        except Exception:
            pass

        usage = (response.llm_output or {}).get("token_usage", {})
        in_tok  = int(usage.get("prompt_tokens",     0))
        out_tok = int(usage.get("completion_tokens", 0))
        
        running_in = sum(e.input_tokens for e in self._span.llm_events) + in_tok
        running_out = sum(e.output_tokens for e in self._span.llm_events) + out_tok
        logger.debug(
            "[%s Tokens #%d] in=%d out=%d  (total=%d)",
            agent_name, self._span._llm_call_index, in_tok, out_tok, running_in + running_out
        )
        self._span.llm_events.append(LLMEvent(
            call_index=self._span._llm_call_index,
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
        ))


@dataclass
class PipelineTrace:
    """
    Collects all agent spans for one test invocation.
    One PipelineTrace per test; each agent appends its span via start_span().
    Span dicts are stored in RepairState and written to raw traces directory.
    """

    session_id: str
    test_name: str
    project: str
    spans: list[AgentSpan] = field(default_factory=list)

    @contextmanager
    def start_span(self, agent: str, model: str) -> Generator[AgentSpan, None, None]:
        """
        Context manager that opens a span, yields it for the caller to use,
        and closes it (success=True or records error) on exit.
        """
        span = AgentSpan(agent=agent, model=model)
        self.spans.append(span)
        try:
            yield span
            span.success = True
        except Exception as exc:
            span.error = str(exc)
            raise
        finally:
            span.finished_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "test_name":  self.test_name,
            "project":    self.project,
            "spans":      [s.to_dict() for s in self.spans],
        }
