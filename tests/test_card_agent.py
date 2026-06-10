from __future__ import annotations

import pytest

from bookwiki.agents.card_agent import CardAgent
from tests.fakes import RecordingRuntime


@pytest.mark.asyncio
async def test_card_agent_returns_chapter_level_card_result() -> None:
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "items": [
                    {
                        "front": "What is point estimation?",
                        "back": "It uses data to choose one value for an unknown parameter.",
                        "citations": [{"ref_id": "src-p001", "quote": "one value"}],
                    }
                ],
                "owner_task_id": "chapter-1:card",
            }
        ]
    )

    result = await CardAgent().run(
        {
            "chapter_id": "chapter-1",
            "title": "Point Estimation",
            "source_md": "<!-- source_ref: src-p001 -->\nPoint estimation chooses one value.",
            "chapter_body_md": "# Point Estimation\n\nPoint estimation chooses one value.",
            "cards_per_chapter": 1,
        },
        model="deepseek-v4-flash",
        runtime=runtime,
    )

    assert result.items[0].front == "What is point estimation?"
    assert result.owner_task_id.endswith(":card")
