from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest

from mfp_mcp.server import (
    DeleteFoodFromDiaryInput,
    get_raw_session,
    mfp_delete_food_from_diary,
    raw_add_food,
    raw_get_diary_entries,
)


RUN_LIVE_TESTS = os.getenv("MFP_RUN_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_LIVE_TESTS,
    reason="Live MyFitnessPal smoke tests are disabled. Set MFP_RUN_LIVE_TESTS=1 to enable.",
)


def test_live_add_then_delete_smoke():
    target_date = os.getenv("MFP_LIVE_TEST_DATE", str(date.today()))
    session = get_raw_session()

    before_entries = raw_get_diary_entries(session, target_date, "")
    before_ids = {entry["entry_id"] for entry in before_entries}

    # Keep the live smoke tiny and generic to minimize diary noise.
    raw_add_food(
        session=session,
        food_id="3565579294",
        serving_id="4055398418",
        date_str=target_date,
        meal_index="3",
        quantity=0.08,
    )

    canary_id = None
    try:
        after_add_entries = raw_get_diary_entries(session, target_date, "")
        new_entries = [
            entry for entry in after_add_entries if entry["entry_id"] not in before_ids
        ]
        assert new_entries, "Live smoke add did not create a new diary entry"

        canary = new_entries[-1]
        canary_id = canary["entry_id"]
        assert canary["meal"] == "Snacks"

        result = asyncio.run(
            mfp_delete_food_from_diary(
                DeleteFoodFromDiaryInput(entry_id=canary_id, date=target_date)
            )
        )
        assert '"success": true' in result

        after_delete_entries = raw_get_diary_entries(session, target_date, "")
        assert not any(entry["entry_id"] == canary_id for entry in after_delete_entries)
    finally:
        if canary_id:
            remaining_entries = raw_get_diary_entries(session, target_date, "")
            if any(entry["entry_id"] == canary_id for entry in remaining_entries):
                asyncio.run(
                    mfp_delete_food_from_diary(
                        DeleteFoodFromDiaryInput(entry_id=canary_id, date=target_date)
                    )
                )
