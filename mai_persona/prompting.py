"""提示词文件装载与统一角色视角前缀。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def persona_block(config: Any) -> str:
    """拼接完全由配置提供的角色资料，不内置具体角色设定。"""

    template = (PROMPT_DIR / "persona_prefix.txt").read_text(encoding="utf-8")
    viewpoint = "\n".join(f"- {line.strip()}" for line in config.viewpoint if line.strip())
    return (
        template.replace("{{persona_name}}", config.name.strip())
        .replace("{{viewpoint}}", viewpoint)
        .replace("{{goals}}", config.goals.strip())
    )


def render_prompt(template_name: str, persona: Any, payload: Any) -> str:
    """读取外置模板，并替换角色块与 JSON 输入。"""

    template_path = PROMPT_DIR / f"{template_name}.txt"
    template = template_path.read_text(encoding="utf-8")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return template.replace("{{persona}}", persona_block(persona)).replace("{{input}}", serialized)
