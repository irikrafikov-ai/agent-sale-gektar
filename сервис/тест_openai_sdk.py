"""Дымовой тест адаптера OpenAI Agents SDK без модели и внешних запросов."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    sdk = types.ModuleType("claude_agent_sdk")
    sdk.tool = lambda *_a, **_k: (lambda функция: функция)
    sdk.create_sdk_mcp_server = lambda **kwargs: kwargs
    sys.modules["claude_agent_sdk"] = sdk

import openai_sdk


async def main() -> None:
    assert len(openai_sdk.инструменты_модуля) == 16
    выбранные = openai_sdk.инструменты(
        ["Read", "mcp__gektar__avito_chat_messages"]
    )
    assert [и.name for и in выбранные] == ["Read", "avito_chat_messages"]

    ответ = await выбранные[0].on_invoke_tool(
        None,
        json.dumps({"file_path": "AGENT.md", "offset": 1, "limit": 3}),
    )
    данные = json.loads(ответ)
    assert данные["ok"] is True
    assert "1:" in данные["result"]

    запрет = await выбранные[0].on_invoke_tool(
        None,
        json.dumps({"file_path": "/etc/passwd", "offset": 1, "limit": 3}),
    )
    assert json.loads(запрет)["ok"] is False

    usage = SimpleNamespace(
        request_usage_entries=[SimpleNamespace(
            input_tokens=1_000_000,
            output_tokens=100_000,
            input_tokens_details=SimpleNamespace(
                cached_tokens=200_000, cache_write_tokens=100_000
            ),
        )]
    )
    # Long-context Astra: 700k×$20 + 200k×$2 + 100k×$25 + 100k×$75.
    assert openai_sdk._стоимость("gpt-6-astra", usage) == 24.4
    print("OpenAI Agents SDK: 16 инструментов и границы файлов — зелёные")


if __name__ == "__main__":
    asyncio.run(main())
