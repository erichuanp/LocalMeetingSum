"""LLM-based summarization: topics × speakers × viewpoints + TODOs."""
from __future__ import annotations

import json
import os
from typing import Optional

from openai import OpenAI


SYSTEM_PROMPT = """你是一名会议纪要助手。输入是一段已标注发言人的对话转写。
要求输出**严格 JSON**(不要任何额外文字、不要 markdown 代码块),结构如下:

{
  "topics": [
    {
      "title": "事项的简短中文标题(<=20 字)",
      "summary": "事项一句话总体描述",
      "viewpoints": [
        {"speaker": "<原文中的发言人名>", "view": "该发言人对此事项的核心观点(尽量复述其原意,不要凭空创作)"}
      ]
    }
  ],
  "todos": [
    {"owner": "<原文中的发言人名 或 '未指明'>", "task": "明确的待办事项"}
  ]
}

规则:
- 提取**所有**实际谈及的事项;每个事项下列出**所有**对此事项发表过意见的发言人。
- 不要捏造内容,若某发言人对某事项没有明确观点,就不要把他列入该事项。
- 若某事项只有一人提到,viewpoints 列表里只放一个人。
- todos 仅当对话中**明确**或**强烈暗示**有人要做某事时才提取;否则返回 `[]`。
- 字段名固定为英文,字段值用中文。
- 只输出 JSON。"""


def _client() -> OpenAI:
    base = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
    key = os.getenv("LLM_API_KEY", "1")
    return OpenAI(base_url=base, api_key=key)


def _format_transcript(utterances: list[dict]) -> str:
    """Render utterances as plain text grouped by time order."""
    lines: list[str] = []
    for u in sorted(utterances, key=lambda x: x.get("start_ms", 0)):
        spk = u.get("speaker") or "?"
        txt = (u.get("text") or "").strip()
        if not txt:
            continue
        lines.append(f"[{spk}] {txt}")
    return "\n".join(lines)


def summarize(utterances: list[dict], extra_instruction: Optional[str] = None) -> dict:
    """Synchronously call the LLM and return parsed JSON dict.
       Raises on parse failure (caller decides UX)."""
    transcript = _format_transcript(utterances)
    if not transcript:
        return {"topics": [], "todos": [], "note": "empty transcript"}
    user_msg = transcript
    if extra_instruction:
        user_msg = f"额外要求: {extra_instruction}\n\n" + transcript
    client = _client()
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
    )
    raw = resp.choices[0].message.content or ""
    # The model sometimes wraps JSON in ```json blocks despite instructions; strip those.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        # remove leading "json\n"
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    # Try to locate the outermost JSON object.
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise
