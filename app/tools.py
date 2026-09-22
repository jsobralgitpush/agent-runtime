from collections.abc import Awaitable, Callable
from typing import Any

Tool = Callable[[Any], Awaitable[Any]]


async def echo(value: Any) -> Any:
    return value


async def uppercase(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("uppercase expects a string")
    return value.upper()


async def extract_field(value: Any) -> Any:
    if not isinstance(value, dict) or "object" not in value or "field" not in value:
        raise ValueError("extract_field expects object and field")
    obj = value["object"]
    if not isinstance(obj, dict):
        raise ValueError("object must be a mapping")
    return obj[value["field"]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {
            "echo": echo,
            "uppercase": uppercase,
            "extract_field": extract_field,
        }

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {name}") from exc

    def register(self, name: str, tool: Tool) -> None:
        if not name or name in self._tools:
            raise ValueError(f"Tool already registered or invalid: {name}")
        self._tools[name] = tool
