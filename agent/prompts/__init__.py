"""Prompt loader utility.

Loads markdown prompts from agent/prompts/ and supports {placeholder} substitution.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def load(name: str, **kwargs) -> str:
    """Load a prompt by name and substitute {placeholders}.

    Uses str.format(); any literal { or } in the template must be doubled ({{, }}).
    """
    template = _read(name)
    if not kwargs:
        return template
    # Use safe format: replace only known keys, leave others intact
    for key, value in kwargs.items():
        template = template.replace("{" + key + "}", str(value))
    return template
