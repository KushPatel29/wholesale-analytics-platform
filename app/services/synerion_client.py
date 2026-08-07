from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class SynerionError(RuntimeError):
    """Base Synerion integration error."""


class SynerionConfigError(SynerionError):
    """Raised when required Synerion configuration is missing."""


class SynerionAuthError(SynerionError):
    """Raised when Synerion authentication fails."""


class SynerionRequestError(SynerionError):
    """Raised when the transport to Synerion fails."""


class SynerionResponseError(SynerionError):
    """Raised when Synerion returns an unexpected response."""


def _cfg_value(cfg: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    env_value = os.getenv(key)
    if env_value not in (None, ""):
        return env_value
    if cfg is None:
        return os.getenv(key, default)
    getter = cfg.get if hasattr(cfg, "get") else getattr
    try:
        return getter(key, default)  # type: ignore[misc]
    except TypeError:
        return getter(key) if hasattr(cfg, key) else default


@dataclass(frozen=True)
class SynerionSettings:
    base_url: str
    username: str
    password: str
    api_key: str
    subdomain: str
    app_region: str = "CAE"
    per_page: int = 100
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 60
    max_retries: int = 4
    backoff_factor: float = 1.0
    verify_tls: bool = True

    @classmethod
    def from_env(cls, cfg: Mapping[str, Any] | None = None) -> "SynerionSettings":
        return cls(
            base_url=str(_cfg_value(cfg, "SYNERION_BASE_URL", "https://api.synerionagile.com") or "").strip(),
            username=str(_cfg_value(cfg, "SYNERION_USERNAME", "") or "").strip(),
            password=str(_cfg_value(cfg, "SYNERION_PASSWORD", "") or "").strip(),
            api_key=str(_cfg_value(cfg, "SYNERION_API_KEY", "") or "").strip(),
            subdomain=str(_cfg_value(cfg, "SYNERION_SUBDOMAIN", "") or "").strip(),
            app_region=str(_cfg_value(cfg, "SYNERION_APP_REGION", "CAE") or "CAE").strip(),
            per_page=int(_cfg_value(cfg, "SYNERION_PER_PAGE", 100) or 100),
            connect_timeout_seconds=int(_cfg_value(cfg, "SYNERION_CONNECT_TIMEOUT_SECONDS", 10) or 10),
            read_timeout_seconds=int(_cfg_value(cfg, "SYNERION_READ_TIMEOUT_SECONDS", 60) or 60),
            max_retries=int(_cfg_value(cfg, "SYNERION_MAX_RETRIES", 4) or 4),
            backoff_factor=float(_cfg_value(cfg, "SYNERION_BACKOFF_FACTOR", 1.0) or 1.0),
            verify_tls=str(_cfg_value(cfg, "SYNERION_VERIFY_TLS", "true") or "true").strip().lower()
            not in {"0", "false", "no", "off"},
        )

    def validate(self) -> None:
        missing = []
        if not self.base_url:
            missing.append("SYNERION_BASE_URL")
        if not self.username:
            missing.append("SYNERION_USERNAME")
        if not self.password:
            missing.append("SYNERION_PASSWORD")
        if not self.api_key:
            missing.append("SYNERION_API_KEY")
        if not self.subdomain:
            missing.append("SYNERION_SUBDOMAIN")
        if not self.app_region:
            missing.append("SYNERION_APP_REGION")
        if missing:
            raise SynerionConfigError(
                "Missing required Synerion configuration: " + ", ".join(sorted(set(missing)))
            )

    @property
    def timeout(self) -> tuple[int, int]:
        return (
            max(1, int(self.connect_timeout_seconds)),
            max(1, int(self.read_timeout_seconds)),
        )


class SynerionClient:
    def __init__(
        self,
        settings: SynerionSettings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self._session = session or self._build_session(settings)
        self._token: str | None = None

    @property
    def session(self) -> requests.Session:
        return self._session

    @staticmethod
    def _build_session(settings: SynerionSettings) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=max(0, int(settings.max_retries)),
            connect=max(0, int(settings.max_retries)),
            read=max(0, int(settings.max_retries)),
            status=max(0, int(settings.max_retries)),
            backoff_factor=max(0.0, float(settings.backoff_factor)),
            status_forcelist=(408, 429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"Accept": "application/json", "User-Agent": "wa-analytics-labor/1.0"})
        return session

    def _url(self, path: str) -> str:
        return f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"

    def authenticate(self, *, force: bool = False) -> str:
        self.settings.validate()
        if self._token and not force:
            return self._token

        url = self._url("/v1/Authentication/Login")
        payload = {
            "Username": self.settings.username,
            "Password": self.settings.password,
            "Apikey": self.settings.api_key,
            "Subdomain": self.settings.subdomain,
        }
        headers = {
            "AppRegion": self.settings.app_region,
            "Content-Type": "application/json",
        }
        try:
            response = self.session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.settings.timeout,
                verify=self.settings.verify_tls,
            )
        except requests.RequestException as exc:
            raise SynerionRequestError("Unable to reach Synerion authentication endpoint.") from exc

        if response.status_code >= 400:
            logger.warning(
                "synerion.auth_failed",
                extra={"status_code": int(response.status_code), "endpoint": "/v1/Authentication/Login"},
            )
            raise SynerionAuthError("Synerion authentication failed.")

        try:
            response_payload = response.json()
        except ValueError as exc:
            raise SynerionAuthError("Synerion authentication returned an invalid JSON response.") from exc

        token = None
        if isinstance(response_payload, Mapping):
            token = response_payload.get("Token") or response_payload.get("token")
        if not token:
            raise SynerionAuthError("Synerion authentication response did not include a bearer token.")

        self._token = str(token)
        return self._token

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_payload: Mapping[str, Any] | None = None,
        use_auth: bool = False,
        retry_auth: bool = True,
    ) -> Any:
        headers = {"AppRegion": self.settings.app_region}
        if use_auth:
            headers["Authorization"] = f"Bearer {self.authenticate()}"

        try:
            response = self.session.request(
                method=method.upper(),
                url=self._url(path),
                params=params,
                json=json_payload,
                headers=headers,
                timeout=self.settings.timeout,
                verify=self.settings.verify_tls,
            )
        except requests.RequestException as exc:
            raise SynerionRequestError(f"Synerion request failed for {path}.") from exc

        if use_auth and response.status_code == 401 and retry_auth:
            self._token = None
            self.authenticate(force=True)
            return self._request_json(
                method,
                path,
                params=params,
                json_payload=json_payload,
                use_auth=use_auth,
                retry_auth=False,
            )

        if response.status_code in {401, 403}:
            raise SynerionAuthError("Synerion authorization failed for labor data retrieval.")
        if response.status_code >= 400:
            raise SynerionResponseError(f"Synerion returned HTTP {response.status_code} for {path}.")

        try:
            return response.json()
        except ValueError as exc:
            raise SynerionResponseError(f"Synerion returned invalid JSON for {path}.") from exc

    @staticmethod
    def _extract_records(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        if isinstance(payload, Mapping):
            candidates: list[Any] = [payload]
            for wrapper_key in ("item", "Item", "result", "Result", "payload", "Payload"):
                wrapped = payload.get(wrapper_key)
                if isinstance(wrapped, Mapping):
                    candidates.append(wrapped)
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                for key in ("data", "Data", "rows", "Rows", "items", "Items", "results", "Results"):
                    value = candidate.get(key)
                    if isinstance(value, list):
                        return [row for row in value if isinstance(row, Mapping)]
        raise SynerionResponseError("Synerion labor response did not contain a record list.")

    def iter_time_transactions(
        self,
        *,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        include_inactive: bool = True,
        per_page: int | None = None,
        raw_page_handler: Callable[[int, Sequence[dict[str, Any]]], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        page = 1
        page_size = max(1, int(per_page or self.settings.per_page))
        start_iso = self._iso_date(start_date)
        end_iso = self._iso_date(end_date)

        while True:
            params = {
                "IncludeInactive": "true" if include_inactive else "false",
                "StartDate": start_iso,
                "EndDate": end_iso,
                "Page": page,
                "PerPage": page_size,
            }
            payload = self._request_json(
                "GET",
                "/v1/Daily/TimeTransactions",
                params=params,
                use_auth=True,
            )
            rows = self._extract_records(payload)
            if raw_page_handler is not None:
                raw_page_handler(page, rows)
            for row in rows:
                yield dict(row)
            if len(rows) < page_size:
                break
            page += 1

    @staticmethod
    def _iso_date(raw: date | datetime | str) -> str:
        if isinstance(raw, datetime):
            return raw.date().isoformat()
        if isinstance(raw, date):
            return raw.isoformat()
        text = str(raw or "").strip()
        if not text:
            raise SynerionResponseError("Synerion request date was empty.")
        return text
