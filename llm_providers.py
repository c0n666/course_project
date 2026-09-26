"""Free LLM providers for the AI coach: Google Gemini and Groq, over plain HTTP (urllib).

Each provider opens a Conversation that keeps the provider's native message history, so the
agent loop in coach_agent.py only deals with neutral Reply / ToolCall objects.

Configuration (environment):
  COACH_PROVIDER   gemini | groq (default: whichever has a key, Gemini first)
  GEMINI_API_KEY   key from https://aistudio.google.com/apikey (free tier, no card)
  GEMINI_MODEL     default gemini-3.5-flash-lite (the most generous free daily quota)
  GROQ_API_KEY     key from https://console.groq.com/keys (free tier, no card)
  GROQ_MODEL       default openai/gpt-oss-120b
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
USER_AGENT = "NutritionWorkout/1.0"
TIMEOUT = 60
MAX_OUTPUT_TOKENS = 8192


class ProviderError(Exception):
    """The provider failed: HTTP/network error, quota, blocked content or an unusable reply."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    error: str | None = None  # set when the model sent arguments that are not valid JSON


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        if exc.code == 429:
            raise ProviderError("The free AI quota is used up for now (HTTP 429).") from exc
        raise ProviderError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ProviderError(f"Request failed: {exc}") from exc


class Provider:
    name = ""
    key_env = ""
    model_env = ""
    default_model = ""

    @property
    def api_key(self) -> str:
        return _env(self.key_env)

    @property
    def model(self) -> str:
        return _env(self.model_env) or self.default_model

    @property
    def engine(self) -> str:
        return f"{self.name}:{self.model}"

    def start(self, system: str, user_text: str, tools: list[dict[str, Any]],
              history: list[dict[str, str]] | None = None) -> "Conversation":
        """Open a conversation. history: earlier turns as {"role": "user"|"assistant", "content": text}."""
        raise NotImplementedError


class Conversation:
    def send(self) -> Reply:
        raise NotImplementedError

    def add_tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        """results: (call, content, is_error) for every call of the last reply."""
        raise NotImplementedError

    def add_user(self, text: str) -> None:
        raise NotImplementedError


# --- Gemini (generateContent) -------------------------------------------------------------

class GeminiProvider(Provider):
    name = "gemini"
    key_env = "GEMINI_API_KEY"
    model_env = "GEMINI_MODEL"
    default_model = "gemini-3.5-flash-lite"

    def start(self, system, user_text, tools, history=None):
        return GeminiConversation(self, system, user_text, tools, history or [])


class GeminiConversation(Conversation):
    def __init__(self, provider: GeminiProvider, system: str, user_text: str, tools: list[dict[str, Any]],
                 history: list[dict[str, str]]):
        self.provider = provider
        self.system = system
        self.tools = [
            {"name": t["name"], "description": t["description"], "parametersJsonSchema": t["input_schema"]}
            for t in tools
        ]
        self.contents: list[dict[str, Any]] = [
            {"role": "model" if turn["role"] == "assistant" else "user", "parts": [{"text": turn["content"]}]}
            for turn in history
        ]
        self.contents.append({"role": "user", "parts": [{"text": user_text}]})

    def send(self) -> Reply:
        data = _post_json(
            GEMINI_URL.format(model=self.provider.model),
            {
                "systemInstruction": {"parts": [{"text": self.system}]},
                "contents": self.contents,
                "tools": [{"functionDeclarations": self.tools}],
                "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": MAX_OUTPUT_TOKENS},
            },
            {"x-goog-api-key": self.provider.api_key},
        )
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no answer")
            raise ProviderError(f"Gemini returned nothing ({reason}).")
        candidate = candidates[0]
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        if candidate.get("finishReason") not in (None, "STOP") and not parts:
            raise ProviderError(f"Gemini stopped: {candidate.get('finishReason')}.")
        # Keep the model turn exactly as returned: thinking models attach thought signatures.
        self.contents.append({"role": "model", "parts": parts})

        reply = Reply()
        for part in parts:
            if "functionCall" in part:
                call = part["functionCall"]
                reply.tool_calls.append(ToolCall(id=call.get("id", ""), name=call.get("name", ""),
                                                 args=call.get("args") or {}))
            elif part.get("text") and not part.get("thought"):
                reply.text += part["text"]
        if candidate.get("finishReason") == "MAX_TOKENS" and not reply.tool_calls:
            raise ProviderError("Gemini ran out of output tokens.")
        return reply

    def add_tool_results(self, results):
        parts = []
        for call, content, is_error in results:
            try:
                payload: Any = json.loads(content)
            except ValueError:
                payload = content
            response = {"error": payload} if is_error else {"result": payload}
            function_response = {"name": call.name, "response": response}
            if call.id:  # newer models number their calls; echo the id so results match up
                function_response["id"] = call.id
            parts.append({"functionResponse": function_response})
        self.contents.append({"role": "user", "parts": parts})

    def add_user(self, text):
        self.contents.append({"role": "user", "parts": [{"text": text}]})


# --- Groq (OpenAI-compatible chat completions) ----------------------------------------------

class GroqProvider(Provider):
    name = "groq"
    key_env = "GROQ_API_KEY"
    model_env = "GROQ_MODEL"
    default_model = "openai/gpt-oss-120b"

    def start(self, system, user_text, tools, history=None):
        return GroqConversation(self, system, user_text, tools, history or [])


class GroqConversation(Conversation):
    def __init__(self, provider: GroqProvider, system: str, user_text: str, tools: list[dict[str, Any]],
                 history: list[dict[str, str]]):
        self.provider = provider
        self.tools = [
            {"type": "function",
             "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools
        ]
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *({"role": turn["role"], "content": turn["content"]} for turn in history),
            {"role": "user", "content": user_text},
        ]

    def send(self) -> Reply:
        data = _post_json(
            GROQ_URL,
            {
                "model": self.provider.model,
                "messages": self.messages,
                "tools": self.tools,
                "tool_choice": "auto",
                "temperature": 0.4,
                "max_completion_tokens": MAX_OUTPUT_TOKENS,
            },
            {"Authorization": f"Bearer {self.provider.api_key}"},
        )
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("Groq returned nothing.")
        message = choices[0].get("message") or {}
        raw_calls = message.get("tool_calls") or []
        if choices[0].get("finish_reason") == "length" and not raw_calls:
            raise ProviderError("Groq ran out of output tokens.")

        # Echo back only standard fields (reasoning models add extra ones the API won't accept back).
        echoed: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if raw_calls:
            echoed["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
                for c in raw_calls
            ]
        self.messages.append(echoed)

        reply = Reply(text=message.get("content") or "")
        for c in raw_calls:
            try:
                args, error = json.loads(c["function"].get("arguments") or "{}"), None
            except ValueError:
                args, error = {}, "Arguments were not valid JSON."
            reply.tool_calls.append(ToolCall(id=c["id"], name=c["function"]["name"],
                                             args=args if isinstance(args, dict) else {}, error=error))
        return reply

    def add_tool_results(self, results):
        for call, content, _is_error in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})


PROVIDERS: dict[str, Provider] = {"gemini": GeminiProvider(), "groq": GroqProvider()}


def get_provider() -> Provider | None:
    """The configured provider with an API key, or None (the coach then runs in basic mode)."""
    choice = _env("COACH_PROVIDER").lower()
    if choice:
        provider = PROVIDERS.get(choice)
        return provider if provider and provider.api_key else None
    return next((p for p in PROVIDERS.values() if p.api_key), None)
