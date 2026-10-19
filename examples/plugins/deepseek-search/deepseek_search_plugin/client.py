"""HTTP adapter for the DeepSeek Search and Fetch tool APIs."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast
from urllib.parse import urlsplit

import httpx

MAX_QUERY_CHARS = 128
MAX_ORI_QUESTION_CHARS = 128
MIN_SEARCH_COUNT = 1
MAX_SEARCH_COUNT = 50
MAX_FETCH_URL_CHARS = 8192

_RETRYABLE_STATUS_CODES = frozenset({408, 429})
_MAX_RETRY_DELAY_SECONDS = 8.0
_MAX_ERROR_DETAIL_CHARS = 500
_MAX_ERROR_CODE_CHARS = 128


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated configuration snapshot for one plugin generation."""

    base_url: str
    timeout_seconds: float
    max_retries: int
    default_count: int
    default_need_content: bool
    max_content_chars: int

    @classmethod
    def from_configuration(cls, values: Mapping[str, object]) -> Settings:
        timeout_seconds = _config_number(values, "timeout_seconds")
        max_retries = _config_integer(values, "max_retries")
        default_count = _config_integer(values, "default_count")
        max_content_chars = _config_integer(values, "max_content_chars")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("configuration field 'timeout_seconds' must be positive and finite")
        if not 0 <= max_retries <= 5:
            raise ValueError("configuration field 'max_retries' must be between 0 and 5")
        _validate_count(default_count)
        if max_content_chars <= 0:
            raise ValueError("configuration field 'max_content_chars' must be positive")
        return cls(
            base_url=_validate_base_url(_config_string(values, "base_url")),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            default_count=default_count,
            default_need_content=_config_boolean(values, "default_need_content"),
            max_content_chars=max_content_chars,
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    summary: str
    content: str
    site_name: str | None
    date: str | None
    authority: int | None
    rerank_score: float | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    title: str
    content: str


class DeepSeekAPIError(RuntimeError):
    """A classified DeepSeek API or transport failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class DeepSeekSearchClient:
    """One pooled HTTP client for both tools of a plugin generation."""

    def __init__(self, settings: Settings, api_key: str) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(settings.timeout_seconds),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(
        self,
        query: str,
        *,
        count: int,
        need_content: bool,
        ori_question: str | None = None,
    ) -> tuple[SearchResult, ...]:
        _validate_query(query)
        _validate_count(count)
        _validate_ori_question(ori_question)
        payload: dict[str, object] = {
            "query": query,
            "count": count,
            "need_content": need_content,
        }
        if ori_question is not None:
            payload["ori_question"] = ori_question
        response = await self._post("/v1/tools/search", payload, operation="Search")
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise DeepSeekAPIError(
                "DeepSeek Search returned a response without a results array.",
                status_code=200,
            )
        results: list[SearchResult] = []
        for item in raw_results:
            parsed = _parse_search_result(item)
            if parsed is not None:
                results.append(parsed)
        if raw_results and not results:
            raise DeepSeekAPIError(
                "DeepSeek Search returned no usable result entries.",
                status_code=200,
            )
        return tuple(results)

    async def fetch(self, url: str) -> FetchResult:
        _validate_fetch_url(url)
        response = await self._post("/v1/tools/fetch", {"url": url}, operation="Fetch")
        content = response.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekAPIError(
                "DeepSeek Fetch returned no usable page content.",
                status_code=200,
            )
        title = _text_field(response, "title", compact=True)
        return FetchResult(url=url, title=title, content=content.strip())

    async def _post(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        operation: str,
    ) -> dict[str, object]:
        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                return await self._post_with_retries(path, payload, operation=operation)
        except TimeoutError:
            raise DeepSeekAPIError(
                f"DeepSeek {operation} timed out after {self._settings.timeout_seconds:g} seconds.",
                retryable=True,
            ) from None

    async def _post_with_retries(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        operation: str,
    ) -> dict[str, object]:
        url = self._settings.base_url + path
        last_error: DeepSeekAPIError | None = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                response = await self._http.post(url, json=payload)
            except httpx.RequestError as exc:
                last_error = _network_error(exc, operation)
            else:
                if response.is_success:
                    return _decode_json(response, operation)
                last_error = _status_error(response, operation)
                if not last_error.retryable:
                    raise last_error
            if attempt >= self._settings.max_retries:
                assert last_error is not None
                raise last_error
            await asyncio.sleep(_retry_delay(last_error, attempt))
        raise AssertionError("DeepSeek request retry loop did not terminate")


def _config_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"configuration field {key!r} must be a nonempty string")
    return value


def _config_number(values: Mapping[str, object], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"configuration field {key!r} must be a number")
    return float(value)


def _config_integer(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"configuration field {key!r} must be an integer")
    return value


def _config_boolean(values: Mapping[str, object], key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"configuration field {key!r} must be a boolean")
    return value


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        _ = parsed.port  # Validate an explicit port without using its value.
    except ValueError as exc:
        raise ValueError("base_url must be a valid HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    if any(_is_invalid_url_character(character) for character in normalized):
        raise ValueError("base_url must not contain whitespace or control characters")
    return normalized


def _validate_query(query: str) -> None:
    if not query.strip():
        raise ValueError("query must not be blank")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must not exceed {MAX_QUERY_CHARS} characters")


def _validate_ori_question(ori_question: str | None) -> None:
    if ori_question is None:
        return
    if not ori_question.strip():
        raise ValueError("ori_question must not be blank when provided")
    if len(ori_question) > MAX_ORI_QUESTION_CHARS:
        raise ValueError(f"ori_question must not exceed {MAX_ORI_QUESTION_CHARS} characters")


def _validate_count(count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("count must be an integer")
    if not MIN_SEARCH_COUNT <= count <= MAX_SEARCH_COUNT:
        raise ValueError(f"count must be between {MIN_SEARCH_COUNT} and {MAX_SEARCH_COUNT}")


def _validate_fetch_url(url: str) -> None:
    if len(url) > MAX_FETCH_URL_CHARS:
        raise ValueError(f"url must not exceed {MAX_FETCH_URL_CHARS} characters")
    if any(_is_invalid_url_character(character) for character in url):
        raise ValueError("url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port  # Validate an explicit port without using its value.
    except ValueError as exc:
        raise ValueError("url must be a valid absolute HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("url must be an absolute HTTP(S) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain embedded credentials")


def _is_invalid_url_character(character: str) -> bool:
    return character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F


def _parse_search_result(item: object) -> SearchResult | None:
    if not isinstance(item, Mapping):
        return None
    fields = cast(Mapping[str, object], item)
    url = _text_field(fields, "url", compact=True)
    if not url:
        return None
    content = fields.get("content")
    return SearchResult(
        title=_text_field(fields, "title", compact=True) or url,
        url=url,
        snippet=_text_field(fields, "snippet", compact=True),
        summary=_text_field(fields, "summary", compact=True),
        content=content.strip() if isinstance(content, str) else "",
        site_name=_optional_text_field(fields, "site_name"),
        date=_optional_text_field(fields, "date"),
        authority=_authority_field(fields),
        rerank_score=_score_field(fields),
    )


def _text_field(item: Mapping[str, object], key: str, *, compact: bool) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        return ""
    if compact:
        return " ".join(value.split())
    return value.strip()


def _optional_text_field(item: Mapping[str, object], key: str) -> str | None:
    value = _text_field(item, key, compact=True)
    return value or None


def _authority_field(item: Mapping[str, object]) -> int | None:
    value = item.get("authority")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -1 <= value <= 4 else None


def _score_field(item: Mapping[str, object]) -> float | None:
    value = item.get("rerank_score")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    score = float(value)
    return score if math.isfinite(score) else None


def _decode_json(response: httpx.Response, operation: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise DeepSeekAPIError(
            f"DeepSeek {operation} returned an invalid JSON response "
            f"(HTTP {response.status_code}).",
            status_code=response.status_code,
        ) from None
    if not isinstance(payload, dict):
        raise DeepSeekAPIError(
            f"DeepSeek {operation} returned a JSON value that is not an object.",
            status_code=response.status_code,
        )
    return cast(dict[str, object], payload)


def _status_error(response: httpx.Response, operation: str) -> DeepSeekAPIError:
    status = response.status_code
    detail, code = _error_payload(response)
    if status == 400:
        message = f"DeepSeek {operation} rejected the request (HTTP 400)."
    elif status == 401:
        message = (
            f"DeepSeek {operation} authentication failed (HTTP 401). "
            "Check the deepseek_api_key credential."
        )
    elif status == 402:
        message = (
            f"DeepSeek {operation} could not run because the account balance is insufficient "
            "(HTTP 402). Top up the account, then retry."
        )
    elif status == 403:
        message = f"DeepSeek {operation} was rejected by a safety or access policy (HTTP 403)."
    elif status == 404 and operation == "Fetch":
        message = "DeepSeek Fetch could not retrieve usable page content (HTTP 404)."
    elif status == 408:
        message = f"DeepSeek {operation} timed out upstream (HTTP 408)."
    elif status == 413:
        message = f"DeepSeek {operation} exceeded the service request size limit (HTTP 413)."
    elif status == 429:
        message = f"DeepSeek {operation} exceeded the account rate limit (HTTP 429)."
    elif status >= 500:
        message = f"DeepSeek {operation} is temporarily unavailable (HTTP {status})."
    else:
        message = f"DeepSeek {operation} failed (HTTP {status})."
    if detail:
        message += f" Server message: {detail}"
    return DeepSeekAPIError(
        message,
        status_code=status,
        code=code,
        retryable=status in _RETRYABLE_STATUS_CODES or status >= 500,
        retry_after=_retry_after(response),
    )


def _error_payload(response: httpx.Response) -> tuple[str, str | None]:
    fallback = _compact(response.text)
    try:
        document = response.json()
    except ValueError:
        return fallback, None
    if not isinstance(document, dict):
        return fallback, None
    error = document.get("error")
    if not isinstance(error, dict):
        return fallback, None
    message = error.get("message")
    detail = _compact(message) if isinstance(message, str) else fallback
    raw_code = error.get("code")
    code: str | None = raw_code.strip() if isinstance(raw_code, str) else None
    if code is not None and (not code or len(code) > _MAX_ERROR_CODE_CHARS):
        code = None
    return detail, code


def _compact(value: str) -> str:
    # Bound tokenization work before the server controls the response size.
    sample = value[: _MAX_ERROR_DETAIL_CHARS * 8]
    return " ".join(sample.split())[:_MAX_ERROR_DETAIL_CHARS]


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            seconds = (moment - datetime.now(UTC)).total_seconds()
        except TypeError, ValueError, OverflowError:
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _retry_delay(error: DeepSeekAPIError, attempt: int) -> float:
    if error.retry_after is not None:
        return min(error.retry_after, _MAX_RETRY_DELAY_SECONDS)
    return min(0.5 * (2**attempt), _MAX_RETRY_DELAY_SECONDS)


def _network_error(exc: httpx.RequestError, operation: str) -> DeepSeekAPIError:
    # Local protocol errors can quote outgoing header values; keep only the type.
    detail = "" if isinstance(exc, httpx.LocalProtocolError) else str(exc).strip()
    description = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return DeepSeekAPIError(
        f"DeepSeek {operation} connection failed: {description}",
        retryable=not isinstance(exc, httpx.LocalProtocolError),
    )
