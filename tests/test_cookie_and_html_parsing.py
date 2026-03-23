from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mfp_mcp import server


@dataclass
class FakeResponse:
    text: str = ""
    status_code: int = 200
    url: str = "https://www.myfitnesspal.com/food/diary"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def get(self, url: str, params=None):
        self.calls.append({"url": url, "params": params})
        return self.response


def test_normalize_cookie_records_handles_legacy_dict():
    normalized = server.normalize_cookie_records({"session": "abc", "csrf": "def"})

    assert normalized == [
        {
            "name": "session",
            "value": "abc",
            "domain": ".myfitnesspal.com",
            "path": "/",
            "secure": True,
            "expires": None,
            "discard": False,
            "source": "legacy-json",
        },
        {
            "name": "csrf",
            "value": "def",
            "domain": ".myfitnesspal.com",
            "path": "/",
            "secure": True,
            "expires": None,
            "discard": False,
            "source": "legacy-json",
        },
    ]


def test_normalize_cookie_records_handles_structured_list():
    normalized = server.normalize_cookie_records(
        [
            {
                "name": "remember_me",
                "value": 123,
                "domain": "",
                "path": "",
                "secure": False,
                "expires": "1700000000",
            },
            {
                "name": "bad",
                "value": None,
            },
            "not-a-dict",
        ]
    )

    assert normalized == [
        {
            "name": "remember_me",
            "value": "123",
            "domain": ".myfitnesspal.com",
            "path": "/",
            "secure": False,
            "expires": 1700000000,
            "discard": False,
            "source": "saved-cookie-record",
        }
    ]


def test_summarize_cookie_records_treats_short_lived_cf_bm_as_non_blocking():
    far_future = int((datetime.now() + timedelta(days=10)).timestamp())
    soon = int((datetime.now() + timedelta(hours=1)).timestamp())
    cookies = [
        {"name": "__Secure-next-auth.session-token", "value": "a", "expires": far_future},
        {"name": "_mfp_session", "value": "b", "expires": None},
        {"name": "cf_clearance", "value": "c", "expires": far_future},
        {"name": "__cf_bm", "value": "d", "expires": soon},
        {"name": "__Host-next-auth.csrf-token", "value": "e", "expires": None},
    ]

    summary = server.summarize_cookie_records(cookies)

    assert summary["missing_critical"] == []
    assert summary["expired_critical"] == []
    assert summary["expiring_soon_critical"] == ["__cf_bm"]
    assert summary["needs_refresh"] is False
    assert summary["refresh_recommended"] is False


def test_import_netscape_cookies_parses_cookie_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "NETSCAPE_COOKIES_FILE",
        tmp_path / "www.myfitnesspal.com_cookies.txt",
    )
    server.NETSCAPE_COOKIES_FILE.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".myfitnesspal.com\tTRUE\t/\tTRUE\t1700000000\tremember_me\tcookie-value",
                ".myfitnesspal.com\tTRUE\t/\tFALSE\t0\t_mfp_session\tsession-cookie",
            ]
        )
    )

    imported = server.import_netscape_cookies()

    assert imported == [
        {
            "domain": ".myfitnesspal.com",
            "path": "/",
            "secure": True,
            "expires": 1700000000,
            "discard": False,
            "name": "remember_me",
            "value": "cookie-value",
            "source": "netscape-export",
        },
        {
            "domain": ".myfitnesspal.com",
            "path": "/",
            "secure": False,
            "expires": None,
            "discard": True,
            "name": "_mfp_session",
            "value": "session-cookie",
            "source": "netscape-export",
        },
    ]


def test_load_cookie_records_prefers_newer_netscape_export(tmp_path, monkeypatch):
    config_dir = tmp_path / ".mfp_mcp"
    cookies_file = config_dir / "cookies.json"
    netscape_file = tmp_path / "www.myfitnesspal.com_cookies.txt"
    config_dir.mkdir()

    cookies_file.write_text(
        json.dumps(
            {
                "cookies": [{"name": "old_cookie", "value": "stale"}],
                "saved_at": "2026-03-20T10:00:00",
            }
        )
    )
    netscape_file.write_text(
        ".myfitnesspal.com\tTRUE\t/\tTRUE\t1700000000\tremember_me\tfresh-cookie\n"
    )

    cookies_file.touch()
    netscape_file.touch()
    older = datetime.now().timestamp() - 100
    newer = datetime.now().timestamp()
    import os

    os.utime(cookies_file, (older, older))
    os.utime(netscape_file, (newer, newer))

    monkeypatch.setattr(server, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(server, "COOKIES_FILE", cookies_file)
    monkeypatch.setattr(server, "NETSCAPE_COOKIES_FILE", netscape_file)
    monkeypatch.setattr(server, "maybe_refresh_cookie_records", lambda records: records)

    loaded = server.load_cookie_records()

    assert loaded == [
        {
            "domain": ".myfitnesspal.com",
            "path": "/",
            "secure": True,
            "expires": 1700000000,
            "discard": False,
            "name": "remember_me",
            "value": "fresh-cookie",
            "source": "netscape-export",
        }
    ]
    saved_payload = json.loads(cookies_file.read_text())
    assert saved_payload["source"] == "netscape-export"


def test_load_cookie_records_falls_back_to_browser_import(tmp_path, monkeypatch):
    config_dir = tmp_path / ".mfp_mcp"
    cookies_file = config_dir / "cookies.json"
    netscape_file = tmp_path / "www.myfitnesspal.com_cookies.txt"
    imported = [
        {
            "name": "__Secure-next-auth.session-token",
            "value": "token",
            "domain": ".myfitnesspal.com",
            "path": "/",
            "secure": True,
            "expires": None,
            "discard": True,
            "source": "browser:brave",
        }
    ]

    monkeypatch.setattr(server, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(server, "COOKIES_FILE", cookies_file)
    monkeypatch.setattr(server, "NETSCAPE_COOKIES_FILE", netscape_file)
    monkeypatch.setattr(
        server,
        "import_browser_cookies",
        lambda preferred_browser=None: (imported, "brave"),
    )

    loaded = server.load_cookie_records()

    assert loaded == imported
    assert cookies_file.exists()
    saved_payload = json.loads(cookies_file.read_text())
    assert saved_payload["source"] == "browser:brave"


SEARCH_RESULTS_HTML = """
<html>
  <body>
    <ul>
      <li>
        <a
          data-external-id="mfp-1"
          data-original-id="food-1"
          data-weight-ids="serv-1,serv-2"
          data-verified="true"
        >
          Chicken Breast
        </a>
        <p class="search-nutritional-info">USDA, 100 g, 165 calories</p>
      </li>
      <li>
        <a
          data-external-id="mfp-2"
          data-original-id="food-2"
          data-weight-ids=""
          data-verified="false"
        >
          Banana
        </a>
        <p class="search-nutritional-info">1 medium, 105 calories</p>
      </li>
    </ul>
  </body>
</html>
"""


def test_raw_search_foods_parses_result_cards():
    session = FakeSession(FakeResponse(text=SEARCH_RESULTS_HTML))

    results = server.raw_search_foods(session, query="banana", date_str="2026-03-23")

    assert session.calls == [
        {
            "url": "https://www.myfitnesspal.com/food/search",
            "params": {"search": "banana", "meal": "0", "date": "2026-03-23"},
        }
    ]
    assert results == [
        {
            "name": "Chicken Breast",
            "brand": "USDA",
            "serving": "100 g",
            "calories": 165.0,
            "food_id": "food-1",
            "mfp_id": "mfp-1",
            "verified": True,
            "serving_id": "serv-1",
            "serving_ids": ["serv-1", "serv-2"],
        },
        {
            "name": "Banana",
            "brand": "",
            "serving": "1 medium",
            "calories": 105.0,
            "food_id": "food-2",
            "mfp_id": "mfp-2",
            "verified": False,
            "serving_id": None,
            "serving_ids": [],
        },
    ]


DIARY_HTML = """
<html>
  <body>
    <table>
      <tr class="meal_header"><td>breakfast</td></tr>
      <tr>
        <td>
          <a data-food-entry-id="11">Eggs</a>
        </td>
      </tr>
      <tr class="total"><td>Total</td></tr>
      <tr class="meal_header"><td>lunch</td></tr>
      <tr>
        <td>
          <div><a data-food-entry-id="22">Chicken and Rice</a></div>
        </td>
      </tr>
      <tr>
        <td>No entry id here</td>
      </tr>
    </table>
  </body>
</html>
"""


def test_raw_get_diary_entries_parses_meals_and_entry_ids():
    session = FakeSession(
        FakeResponse(
            text=DIARY_HTML,
            url="https://www.myfitnesspal.com/food/diary/testpost?date=2026-03-23",
        )
    )

    entries = server.raw_get_diary_entries(session, date_str="2026-03-23", username="testpost")

    assert session.calls == [
        {
            "url": "https://www.myfitnesspal.com/food/diary/testpost",
            "params": {"date": "2026-03-23"},
        }
    ]
    assert entries == [
        {"entry_id": "11", "name": "Eggs", "meal": "Breakfast"},
        {"entry_id": "22", "name": "Chicken and Rice", "meal": "Lunch"},
    ]
