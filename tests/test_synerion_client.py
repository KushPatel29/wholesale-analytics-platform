from __future__ import annotations

from typing import Any

from app.services.synerion_client import SynerionClient, SynerionSettings


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.post_calls: list[dict[str, Any]] = []
        self.request_calls: list[dict[str, Any]] = []
        self._page_payloads = {
            1: [{"EmployeeCode": "E1"}, {"EmployeeCode": "E2"}],
            2: [{"EmployeeCode": "E3"}],
        }

    def mount(self, *_args, **_kwargs):  # pragma: no cover - not used by test
        return None

    @property
    def headers(self):
        return {}

    def post(self, url: str, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return _FakeResponse(200, {"Token": "token-123"})

    def request(self, method: str, url: str, **kwargs):
        self.request_calls.append({"method": method, "url": url, **kwargs})
        page = int((kwargs.get("params") or {}).get("Page") or 1)
        return _FakeResponse(200, self._page_payloads.get(page, []))


def test_synerion_auth_and_pagination_fetch():
    session = _FakeSession()
    settings = SynerionSettings(
        base_url="https://example.test",
        username="user",
        password="pass",
        api_key="api",
        subdomain="trs",
        app_region="CAE",
        per_page=2,
    )
    client = SynerionClient(settings, session=session)

    rows = list(client.iter_time_transactions(start_date="2024-02-01", end_date="2024-02-02", per_page=2))

    assert len(session.post_calls) == 1
    assert len(session.request_calls) == 2
    assert [row["EmployeeCode"] for row in rows] == ["E1", "E2", "E3"]
    auth_headers = session.post_calls[0]["headers"]
    assert auth_headers["AppRegion"] == "CAE"
    request_headers = session.request_calls[0]["headers"]
    assert request_headers["Authorization"] == "Bearer token-123"


def test_synerion_handles_item_data_envelope():
    session = _FakeSession()
    session._page_payloads = {
        1: {"Item": {"Data": [{"EmployeeCode": "E1"}, {"EmployeeCode": "E2"}]}},
        2: {"Item": {"Data": [{"EmployeeCode": "E3"}]}},
    }
    settings = SynerionSettings(
        base_url="https://example.test",
        username="user",
        password="pass",
        api_key="api",
        subdomain="trs",
        app_region="CAE",
        per_page=2,
    )
    client = SynerionClient(settings, session=session)

    rows = list(client.iter_time_transactions(start_date="2024-02-01", end_date="2024-02-02", per_page=2))

    assert [row["EmployeeCode"] for row in rows] == ["E1", "E2", "E3"]
