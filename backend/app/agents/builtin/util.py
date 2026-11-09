"""内置适配器共用的小工具函数。"""
from __future__ import annotations

import json
from typing import Any


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def flatten_content(content: Any, limit: int = 4000) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"\n… (+{len(text) - limit} chars)"


def dig(payload: Any, *path: str, default: Any = None) -> Any:
    cursor = payload
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return default
        cursor = cursor[key]
    return cursor
