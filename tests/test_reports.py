from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date

from mfp_mcp import server


@dataclass
class FakeEntry:
    totals: dict[str, float]


@dataclass
class FakeExercise:
    entries: list[dict]

    def get_as_list(self) -> list[dict]:
        return self.entries


@dataclass
class FakeDay:
    entries: list[FakeEntry]
    exercises: list[FakeExercise]


class FakeClient:
    def __init__(self, days: dict[date, FakeDay]):
        self.days = days

    def get_report(self, **_kwargs):
        raise json.JSONDecodeError("Expecting value", "", 0)

    def get_date(self, target_date: date) -> FakeDay:
        return self.days[target_date]


def test_mfp_get_report_falls_back_to_diary_for_supported_reports(monkeypatch):
    days = {
        date(2026, 3, 21): FakeDay(
            entries=[
                FakeEntry({"calories": 1000.0, "protein": 90.0, "carbohydrates": 50.0, "fat": 40.0})
            ],
            exercises=[
                FakeExercise(
                    [{"nutrition_information": {"calories burned": 200.0}}]
                )
            ],
        ),
        date(2026, 3, 22): FakeDay(
            entries=[
                FakeEntry({"calories": 1500.0, "protein": 120.0, "carbohydrates": 100.0, "fat": 60.0})
            ],
            exercises=[],
        ),
        date(2026, 3, 23): FakeDay(
            entries=[
                FakeEntry({"calories": 900.0, "protein": 130.0, "carbohydrates": 30.0, "fat": 20.0})
            ],
            exercises=[
                FakeExercise(
                    [{"nutrition_information": {"calories burned": 100.0}}]
                )
            ],
        ),
    }

    monkeypatch.setattr(server, "get_mfp_client", lambda: FakeClient(days))

    result = asyncio.run(
        server.mfp_get_report(
            server.GetReportInput(
                report_name="Net Calories",
                start_date="2026-03-21",
                end_date="2026-03-23",
                response_format="json",
            )
        )
    )

    payload = json.loads(result)

    assert payload["source"] == "diary_fallback"
    assert payload["values"] == {
        "2026-03-21": 800.0,
        "2026-03-22": 1500.0,
        "2026-03-23": 800.0,
    }
    assert payload["summary"] == {
        "total": 3100.0,
        "average": 1033.33,
        "min": 800.0,
        "max": 1500.0,
    }


def test_build_report_from_diary_supports_macro_aliases():
    days = {
        date(2026, 3, 23): FakeDay(
            entries=[
                FakeEntry({"calories": 900.0, "protein": 130.0, "carbohydrates": 30.0, "fat": 20.0})
            ],
            exercises=[],
        )
    }
    client = FakeClient(days)

    carbs_report = server.build_report_from_diary(
        client, "Carbs", date(2026, 3, 23), date(2026, 3, 23)
    )
    total_calories_report = server.build_report_from_diary(
        client, "Total Calories", date(2026, 3, 23), date(2026, 3, 23)
    )

    assert carbs_report == OrderedDict([(date(2026, 3, 23), 30.0)])
    assert total_calories_report == OrderedDict([(date(2026, 3, 23), 900.0)])


def test_build_report_all_returns_all_macros():
    days = {
        date(2026, 3, 21): FakeDay(
            entries=[
                FakeEntry({"calories": 1000.0, "protein": 90.0, "carbohydrates": 50.0, "fat": 40.0})
            ],
            exercises=[
                FakeExercise(
                    [{"nutrition_information": {"calories burned": 200.0}}]
                )
            ],
        ),
        date(2026, 3, 22): FakeDay(
            entries=[
                FakeEntry({"calories": 1500.0, "protein": 120.0, "carbohydrates": 100.0, "fat": 60.0})
            ],
            exercises=[],
        ),
    }
    client = FakeClient(days)

    report = server.build_report_from_diary(
        client, "All", date(2026, 3, 21), date(2026, 3, 22)
    )

    assert report[date(2026, 3, 21)] == {
        "calories": 1000.0,
        "protein": 90.0,
        "carbs": 50.0,
        "fat": 40.0,
        "net_calories": 800.0,
    }
    assert report[date(2026, 3, 22)] == {
        "calories": 1500.0,
        "protein": 120.0,
        "carbs": 100.0,
        "fat": 60.0,
        "net_calories": 1500.0,
    }


def test_mfp_get_report_all_with_summary(monkeypatch):
    days = {
        date(2026, 3, 21): FakeDay(
            entries=[
                FakeEntry({"calories": 1000.0, "protein": 90.0, "carbohydrates": 50.0, "fat": 40.0})
            ],
            exercises=[],
        ),
        date(2026, 3, 22): FakeDay(
            entries=[
                FakeEntry({"calories": 1500.0, "protein": 120.0, "carbohydrates": 100.0, "fat": 60.0})
            ],
            exercises=[],
        ),
    }

    monkeypatch.setattr(server, "get_mfp_client", lambda: FakeClient(days))

    result = asyncio.run(
        server.mfp_get_report(
            server.GetReportInput(
                report_name="All",
                start_date="2026-03-21",
                end_date="2026-03-22",
                response_format="json",
            )
        )
    )

    payload = json.loads(result)
    assert payload["source"] == "diary_fallback"
    assert payload["values"]["2026-03-21"]["protein"] == 90.0
    assert payload["values"]["2026-03-22"]["protein"] == 120.0
    assert payload["summary"]["protein"]["average"] == 105.0
    assert payload["summary"]["calories"]["total"] == 2500.0
