"""Template hygiene checks.

A squash-merged change once landed a Jinja template whose lines were prefixed
with their own line numbers (``2<aside class="chat-sidebar">``). The template
still compiled, so nothing failed; the digits simply rendered into the page.
These checks walk every template in the project and fail when a line looks like
numbered source rather than markup.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "app" / "templates"

#: A line that starts with a 1-4 digit number immediately followed by markup.
_NUMBERED_MARKUP_RE = re.compile(r"^\d{1,4}\s*<")


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


@pytest.mark.parametrize("template", _templates(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_templates_have_no_line_number_prefixes(template: Path):
    offenders = [
        number
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), start=1)
        if _NUMBERED_MARKUP_RE.match(line)
    ]
    assert (
        not offenders
    ), f"{template.relative_to(REPO_ROOT)} contains line-numbered markup on lines {offenders}"


def test_chat_template_extends_the_base_layout():
    """The chat page must extend the shared layout, not open with markup."""
    text = (TEMPLATES / "chat" / "index.html").read_text(encoding="utf-8")
    assert text.startswith('{% extends "base.html" %}')
    assert "conversation-status" in text
