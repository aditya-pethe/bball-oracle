from __future__ import annotations

import re
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def render(name: str, **values: object) -> str:
    prompt = (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), prompt)


def load(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
