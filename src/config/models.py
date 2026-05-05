"""
Model configuration registry.

Models are defined in models.json at the project root.
Built-in presets are used as a fallback if the file is absent.

Usage:
    cfg = get_model("minimax")
    llm = cfg.make_llm()

    # per-agent override in an agent node:
    model_key = (config or {}).get("configurable", {}).get("detection_model", "minimax")
    llm = get_model(model_key).make_llm()
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path(__file__).parent.parent.parent / "models.json"

_BUILT_IN: dict[str, dict[str, Any]] = {
    "minimax": {
        "name": "MiniMax-M2.7",
        "provider": "openai-compat",
        "base_url": "https://api.minimax.io/v1",
        "api_key_env": "MINIMAX_API_KEY",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "gpt-4o": {
        "name": "gpt-4o",
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "gpt-4o-mini": {
        "name": "gpt-4o-mini",
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
        "temperature": 0.2,
        "max_tokens": 2048,
    },
}


@dataclass
class ModelConfig:
    """Configuration for one LLM. Provider-agnostic; make_llm() handles dispatch."""

    key: str                      # short identifier used in CLI / config
    name: str                     # model ID sent to the API
    provider: str                 # "openai-compat" | "openai" | "anthropic"
    base_url: str | None = None   # required for openai-compat endpoints
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 4096
    extra: dict[str, Any] = field(default_factory=dict)

    def make_llm(self) -> Any:
        """Construct a LangChain chat model from this configuration."""
        api_key = os.environ.get(self.api_key_env) or os.environ.get("OPENAI_API_KEY") or ""

        if self.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic  # optional dep
            return ChatAnthropic(
                model=self.name,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                api_key=api_key or None,
                **self.extra,
            )

        # Both "openai" and "openai-compat" use ChatOpenAI
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = dict(
            model=self.name,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            api_key=api_key or None,
            **self.extra,
        )
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return ChatOpenAI(**kwargs)

    def __str__(self) -> str:
        return f"{self.key} ({self.name}, {self.provider})"


def _load_all() -> dict[str, dict[str, Any]]:
    """Merge built-in presets with models.json (file entries win on conflict)."""
    merged = dict(_BUILT_IN)
    if _CONFIG_FILE.exists():
        try:
            with _CONFIG_FILE.open(encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("models", {}).items():
                merged[k] = v
        except Exception as exc:
            logger.warning("Could not load %s: %s", _CONFIG_FILE.name, exc)
    return merged


def get_model(key: str) -> ModelConfig:
    """Return a ModelConfig by its short key (e.g. 'minimax', 'gpt-4o')."""
    all_models = _load_all()
    if key not in all_models:
        raise KeyError(
            f"Unknown model key '{key}'. Available: {sorted(all_models)}\n"
            f"Add new models to {_CONFIG_FILE.name} without changing code."
        )
    entry = all_models[key]
    return ModelConfig(
        key=key,
        name=entry["name"],
        provider=entry.get("provider", "openai-compat"),
        base_url=entry.get("base_url"),
        api_key_env=entry.get("api_key_env", "OPENAI_API_KEY"),
        temperature=float(entry.get("temperature", 0.2)),
        max_tokens=int(entry.get("max_tokens", 4096)),
        extra=entry.get("extra", {}),
    )


def list_models() -> list[str]:
    """Return all known model keys (built-in + models.json)."""
    return sorted(_load_all())
