from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mfp_mcp import server


@dataclass
class FakeResponse:
    text: str = ""
    status_code: int = 200
    url: str = "https://www.myfitnesspal.com/food/diary"
    headers: dict[str, str] = field(default_factory=dict)
    history: list["FakeResponse"] = field(default_factory=list)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, get_responses: list[FakeResponse], post_response: FakeResponse):
        self._get_responses = list(get_responses)
        self._post_response = post_response
        self.post_calls: list[dict] = []

    def get(self, url: str, params=None):
        if not self._get_responses:
            raise AssertionError(f"Unexpected GET request to {url}")
        return self._get_responses.pop(0)

    def post(self, url: str, data=None, headers=None, allow_redirects=True):
        self.post_calls.append(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "allow_redirects": allow_redirects,
            }
        )
        return self._post_response


SEARCH_HTML = """
<html>
  <body>
    <form id="food-nutritional-details-form">
      <input name="authenticity_token" value="search-token" />
    </form>
  </body>
</html>
"""


DIARY_HTML_WITH_META = """
<html>
  <head>
    <meta name="csrf-token" content="meta-token" />
  </head>
  <body>
    <form action="/food/diary" method="post">
      <input name="authenticity_token" value="wrong-form-token" />
    </form>
  </body>
</html>
"""


def test_raw_add_food_rejects_redirect_without_new_entry(monkeypatch):
    session = FakeSession(
        get_responses=[FakeResponse(text=SEARCH_HTML)],
        post_response=FakeResponse(
            text="ok",
            status_code=200,
            history=[FakeResponse(status_code=302, headers={"Location": "/food/diary"})],
        ),
    )

    diary_calls = [
        [{"entry_id": "1", "meal": "Snacks", "name": "Existing item"}],
        [{"entry_id": "1", "meal": "Snacks", "name": "Existing item"}],
    ]

    monkeypatch.setattr(
        server,
        "raw_get_diary_entries",
        lambda *_args, **_kwargs: diary_calls.pop(0),
    )

    with pytest.raises(RuntimeError, match="no new diary entry appeared"):
        server.raw_add_food(
            session=session,
            food_id="food-id",
            serving_id="serving-id",
            date_str="2026-03-23",
            meal_index="3",
            quantity=1.0,
        )


def test_raw_add_food_accepts_verified_new_entry(monkeypatch):
    session = FakeSession(
        get_responses=[FakeResponse(text=SEARCH_HTML)],
        post_response=FakeResponse(
            text="ok",
            status_code=200,
            history=[FakeResponse(status_code=302, headers={"Location": "/food/diary"})],
        ),
    )

    diary_calls = [
        [{"entry_id": "1", "meal": "Snacks", "name": "Existing item"}],
        [
            {"entry_id": "1", "meal": "Snacks", "name": "Existing item"},
            {"entry_id": "2", "meal": "Snacks", "name": "New item"},
        ],
    ]

    monkeypatch.setattr(
        server,
        "raw_get_diary_entries",
        lambda *_args, **_kwargs: diary_calls.pop(0),
    )

    server.raw_add_food(
        session=session,
        food_id="food-id",
        serving_id="serving-id",
        date_str="2026-03-23",
        meal_index="3",
        quantity=1.0,
    )

    post_call = session.post_calls[0]
    assert post_call["data"]["authenticity_token"] == "search-token"
    assert post_call["allow_redirects"] is True


def test_raw_delete_food_entry_uses_meta_csrf_and_page_referer(monkeypatch):
    session = FakeSession(
        get_responses=[
            FakeResponse(
                text=DIARY_HTML_WITH_META,
                url="https://www.myfitnesspal.com/food/diary/testpost?date=2026-03-23",
            )
        ],
        post_response=FakeResponse(
            status_code=302,
            url="https://www.myfitnesspal.com/food/remove/123",
            headers={"Location": "https://www.myfitnesspal.com/food/diary/testpost"},
        ),
    )

    monkeypatch.setattr(
        server,
        "raw_get_diary_entries",
        lambda *_args, **_kwargs: [],
    )

    server.raw_delete_food_entry(
        session=session,
        entry_id="123",
        date_str="2026-03-23",
        username="",
    )

    post_call = session.post_calls[0]
    assert post_call["data"]["authenticity_token"] == "meta-token"
    assert (
        post_call["headers"]["Referer"]
        == "https://www.myfitnesspal.com/food/diary/testpost?date=2026-03-23"
    )
    assert post_call["allow_redirects"] is False


def test_raw_delete_food_entry_rejects_login_redirect(monkeypatch):
    session = FakeSession(
        get_responses=[FakeResponse(text=DIARY_HTML_WITH_META)],
        post_response=FakeResponse(
            status_code=302,
            url="https://www.myfitnesspal.com/food/remove/123",
            headers={"Location": "https://www.myfitnesspal.com/account/login"},
        ),
    )

    monkeypatch.setattr(server, "raw_get_diary_entries", lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="redirected to login"):
        server.raw_delete_food_entry(
            session=session,
            entry_id="123",
            date_str="2026-03-23",
            username="",
        )
