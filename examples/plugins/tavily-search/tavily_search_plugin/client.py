"""HTTP adapter for the Tavily Search and Extract APIs."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx

SearchDepth = Literal["advanced", "basic", "fast", "ultra-fast"]
Topic = Literal["general", "news", "finance"]
TimeRange = Literal["day", "week", "month", "year"]
AnswerMode = Literal["none", "basic", "advanced"]
ExtractDepth = Literal["basic", "advanced"]
ContentFormat = Literal["markdown", "text"]

MAX_RESULTS = 20
MAX_EXTRACT_URLS = 20
MAX_URL_CHARS = 8192
MAX_SEARCH_CHUNKS = 3
MAX_EXTRACT_CHUNKS = 5
MAX_TRAVERSAL_DEPTH = 5
MAX_TRAVERSAL_BREADTH = 500

_SEARCH_DEPTHS = frozenset({"advanced", "basic", "fast", "ultra-fast"})
_TOPICS = frozenset({"general", "news", "finance"})
_TIME_RANGES = frozenset({"day", "week", "month", "year"})
_ANSWER_MODES = frozenset({"none", "basic", "advanced"})
_EXTRACT_DEPTHS = frozenset({"basic", "advanced"})
_FORMATS = frozenset({"markdown", "text"})

_RETRYABLE_STATUS_CODES = frozenset({408, 429})
_MAX_RETRY_DELAY_SECONDS = 8.0
_MAX_ERROR_DETAIL_CHARS = 500
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated configuration snapshot for one plugin generation."""

    base_url: str
    timeout_seconds: float
    site_timeout_seconds: float
    max_retries: int
    default_max_results: int
    default_search_depth: SearchDepth
    chunks_per_source: int
    default_extract_depth: ExtractDepth
    default_format: ContentFormat

    @classmethod
    def from_configuration(cls, values: Mapping[str, object]) -> Settings:
        timeout_seconds = _config_number(values, "timeout_seconds")
        site_timeout_seconds = _config_number(values, "site_timeout_seconds")
        max_retries = _config_integer(values, "max_retries")
        default_max_results = _config_integer(values, "default_max_results")
        chunks_per_source = _config_integer(values, "chunks_per_source")
        search_depth = _config_choice(values, "default_search_depth", _SEARCH_DEPTHS)
        extract_depth = _config_choice(values, "default_extract_depth", _EXTRACT_DEPTHS)
        content_format = _config_choice(values, "default_format", _FORMATS)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("configuration field 'timeout_seconds' must be positive and finite")
        if not math.isfinite(site_timeout_seconds) or not 10 <= site_timeout_seconds <= 300:
            raise ValueError(
                "configuration field 'site_timeout_seconds' must be between 10 and 300"
            )
        if not 0 <= max_retries <= 5:
            raise ValueError("configuration field 'max_retries' must be between 0 and 5")
        if not 1 <= default_max_results <= MAX_RESULTS:
            raise ValueError(
                f"configuration field 'default_max_results' must be between 1 and {MAX_RESULTS}"
            )
        if not 1 <= chunks_per_source <= MAX_SEARCH_CHUNKS:
            raise ValueError(
                f"configuration field 'chunks_per_source' must be between 1 and {MAX_SEARCH_CHUNKS}"
            )
        return cls(
            base_url=_validate_base_url(_config_string(values, "base_url")),
            timeout_seconds=timeout_seconds,
            site_timeout_seconds=site_timeout_seconds,
            max_retries=max_retries,
            default_max_results=default_max_results,
            default_search_depth=cast(SearchDepth, search_depth),
            chunks_per_source=chunks_per_source,
            default_extract_depth=cast(ExtractDepth, extract_depth),
            default_format=cast(ContentFormat, content_format),
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    content: str
    score: float | None
    published_date: str | None
    result_id: str | None


@dataclass(frozen=True, slots=True)
class SearchResponse:
    results: tuple[SearchResult, ...]
    answer: str | None
    response_time: float | None
    usage: Mapping[str, object] | None
    request_id: str | None
    auto_parameters: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ExtractResult:
    url: str
    content: str


@dataclass(frozen=True, slots=True)
class ExtractFailure:
    url: str
    error: str


@dataclass(frozen=True, slots=True)
class ExtractResponse:
    results: tuple[ExtractResult, ...]
    failed_results: tuple[ExtractFailure, ...]
    response_time: float | None
    usage: Mapping[str, object] | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class CrawlResult:
    url: str
    content: str


@dataclass(frozen=True, slots=True)
class CrawlResponse:
    base_url: str
    results: tuple[CrawlResult, ...]
    response_time: float | None
    usage: Mapping[str, object] | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class MapResponse:
    base_url: str
    results: tuple[str, ...]
    response_time: float | None
    usage: Mapping[str, object] | None
    request_id: str | None


class TavilyAPIError(RuntimeError):
    """A classified Tavily API or transport failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


class TavilyClient:
    """One pooled HTTP client for Search and Extract of a plugin generation."""

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
        max_results: int,
        search_depth: SearchDepth,
        topic: Topic,
        time_range: TimeRange | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        include_domains: tuple[str, ...] | None = None,
        exclude_domains: tuple[str, ...] | None = None,
        include_answer: AnswerMode = "none",
    ) -> SearchResponse:
        _validate_query(query)
        _validate_max_results(max_results)
        _validate_search_depth(search_depth)
        _validate_topic(topic)
        if time_range is not None:
            _validate_time_range(time_range)
        _validate_date_range(start_date, end_date)
        _validate_domains(include_domains, "include_domains")
        _validate_domains(exclude_domains, "exclude_domains")
        _validate_answer_mode(include_answer)

        payload: dict[str, object] = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "topic": topic,
            "include_published_date": True,
            "include_usage": True,
        }
        if search_depth != "ultra-fast":
            payload["chunks_per_source"] = self._settings.chunks_per_source
        if time_range is not None:
            payload["time_range"] = time_range
        if start_date is not None:
            payload["start_date"] = start_date
        if end_date is not None:
            payload["end_date"] = end_date
        if time_range is not None or start_date is not None or end_date is not None:
            payload["filter_by_published_date"] = True
        if include_domains:
            payload["include_domains"] = list(include_domains)
        if exclude_domains:
            payload["exclude_domains"] = list(exclude_domains)
        if include_answer != "none":
            payload["include_answer"] = include_answer

        response = await self._post("/search", payload, operation="Search")
        return _parse_search_response(response)

    async def extract(
        self,
        urls: tuple[str, ...],
        *,
        query: str | None = None,
        chunks_per_source: int | None = None,
        extract_depth: ExtractDepth,
        content_format: ContentFormat,
    ) -> ExtractResponse:
        _validate_urls(urls)
        if query is not None:
            _validate_query(query)
        if chunks_per_source is not None:
            if query is None:
                raise ValueError("chunks_per_source requires query")
            if not 1 <= chunks_per_source <= MAX_EXTRACT_CHUNKS:
                raise ValueError(f"chunks_per_source must be between 1 and {MAX_EXTRACT_CHUNKS}")
        _validate_extract_depth(extract_depth)
        _validate_format(content_format)

        payload: dict[str, object] = {
            "urls": list(urls),
            "extract_depth": extract_depth,
            "format": content_format,
            "include_usage": True,
        }
        if query is not None:
            payload["query"] = query
            if chunks_per_source is not None:
                payload["chunks_per_source"] = chunks_per_source

        response = await self._post("/extract", payload, operation="Extract")
        return _parse_extract_response(response)

    async def crawl(
        self,
        url: str,
        *,
        extract_depth: ExtractDepth,
        content_format: ContentFormat,
        instructions: str | None = None,
        chunks_per_source: int | None = None,
        max_depth: int = 1,
        max_breadth: int = 20,
        limit: int = 50,
        select_paths: tuple[str, ...] | None = None,
        select_domains: tuple[str, ...] | None = None,
        exclude_paths: tuple[str, ...] | None = None,
        exclude_domains: tuple[str, ...] | None = None,
        allow_external: bool = True,
    ) -> CrawlResponse:
        _validate_http_url(url)
        if instructions is not None:
            _validate_query(instructions)
        if chunks_per_source is not None:
            if instructions is None:
                raise ValueError("chunks_per_source requires instructions")
            if not 1 <= chunks_per_source <= MAX_EXTRACT_CHUNKS:
                raise ValueError(f"chunks_per_source must be between 1 and {MAX_EXTRACT_CHUNKS}")
        _validate_traversal_depth(max_depth)
        _validate_traversal_breadth(max_breadth)
        _validate_limit(limit)
        _validate_patterns(select_paths, "select_paths")
        _validate_patterns(select_domains, "select_domains")
        _validate_patterns(exclude_paths, "exclude_paths")
        _validate_patterns(exclude_domains, "exclude_domains")
        _validate_extract_depth(extract_depth)
        _validate_format(content_format)

        payload: dict[str, object] = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "allow_external": allow_external,
            "extract_depth": extract_depth,
            "format": content_format,
            "include_usage": True,
        }
        if instructions is not None:
            payload["instructions"] = instructions
            if chunks_per_source is not None:
                payload["chunks_per_source"] = chunks_per_source
        _add_patterns(payload, "select_paths", select_paths)
        _add_patterns(payload, "select_domains", select_domains)
        _add_patterns(payload, "exclude_paths", exclude_paths)
        _add_patterns(payload, "exclude_domains", exclude_domains)

        response = await self._post(
            "/crawl",
            payload,
            operation="Crawl",
            timeout_seconds=self._settings.site_timeout_seconds,
        )
        return _parse_crawl_response(response, url)

    async def map(
        self,
        url: str,
        *,
        instructions: str | None = None,
        max_depth: int = 1,
        max_breadth: int = 20,
        limit: int = 50,
        select_paths: tuple[str, ...] | None = None,
        select_domains: tuple[str, ...] | None = None,
        exclude_paths: tuple[str, ...] | None = None,
        exclude_domains: tuple[str, ...] | None = None,
        allow_external: bool = True,
    ) -> MapResponse:
        _validate_http_url(url)
        if instructions is not None:
            _validate_query(instructions)
        _validate_traversal_depth(max_depth)
        _validate_traversal_breadth(max_breadth)
        _validate_limit(limit)
        _validate_patterns(select_paths, "select_paths")
        _validate_patterns(select_domains, "select_domains")
        _validate_patterns(exclude_paths, "exclude_paths")
        _validate_patterns(exclude_domains, "exclude_domains")

        payload: dict[str, object] = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "allow_external": allow_external,
            "include_usage": True,
        }
        if instructions is not None:
            payload["instructions"] = instructions
        _add_patterns(payload, "select_paths", select_paths)
        _add_patterns(payload, "select_domains", select_domains)
        _add_patterns(payload, "exclude_paths", exclude_paths)
        _add_patterns(payload, "exclude_domains", exclude_domains)

        response = await self._post(
            "/map",
            payload,
            operation="Map",
            timeout_seconds=self._settings.site_timeout_seconds,
        )
        return _parse_map_response(response, url)

    async def _post(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        operation: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        budget = self._settings.timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            async with asyncio.timeout(budget):
                return await self._post_with_retries(
                    path,
                    payload,
                    operation=operation,
                    timeout_seconds=budget,
                )
        except TimeoutError:
            raise TavilyAPIError(
                f"Tavily {operation} timed out after {budget:g} seconds.",
                retryable=True,
            ) from None

    async def _post_with_retries(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        operation: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        url = self._settings.base_url + path
        last_error: TavilyAPIError | None = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                response = await self._http.post(
                    url,
                    json=payload,
                    timeout=httpx.Timeout(timeout_seconds),
                )
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
        raise AssertionError("Tavily request retry loop did not terminate")


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


def _config_choice(values: Mapping[str, object], key: str, allowed: frozenset[str]) -> str:
    value = _config_string(values, key)
    if value not in allowed:
        raise ValueError(f"configuration field {key!r} must be one of {', '.join(sorted(allowed))}")
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


def _validate_max_results(max_results: int) -> None:
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValueError("max_results must be an integer")
    if not 1 <= max_results <= MAX_RESULTS:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS}")


def _validate_search_depth(search_depth: str) -> None:
    if search_depth not in _SEARCH_DEPTHS:
        raise ValueError("search_depth must be one of " + ", ".join(sorted(_SEARCH_DEPTHS)))


def _validate_topic(topic: str) -> None:
    if topic not in _TOPICS:
        raise ValueError("topic must be one of " + ", ".join(sorted(_TOPICS)))


def _validate_time_range(time_range: str) -> None:
    if time_range not in _TIME_RANGES:
        raise ValueError("time_range must be one of " + ", ".join(sorted(_TIME_RANGES)))


def _validate_answer_mode(include_answer: str) -> None:
    if include_answer not in _ANSWER_MODES:
        raise ValueError("include_answer must be one of " + ", ".join(sorted(_ANSWER_MODES)))


def _validate_extract_depth(extract_depth: str) -> None:
    if extract_depth not in _EXTRACT_DEPTHS:
        raise ValueError("extract_depth must be one of " + ", ".join(sorted(_EXTRACT_DEPTHS)))


def _validate_format(content_format: str) -> None:
    if content_format not in _FORMATS:
        raise ValueError("format must be one of " + ", ".join(sorted(_FORMATS)))


def _validate_domains(domains: tuple[str, ...] | None, field: str) -> None:
    if domains is None:
        return
    for domain in domains:
        if not isinstance(domain, str) or not domain.strip():
            raise ValueError(f"{field} must contain nonempty strings")


def _validate_urls(urls: tuple[str, ...]) -> None:
    if not 1 <= len(urls) <= MAX_EXTRACT_URLS:
        raise ValueError(f"urls must contain between 1 and {MAX_EXTRACT_URLS} entries")
    for index, url in enumerate(urls):
        try:
            _validate_http_url(url)
        except ValueError as exc:
            raise ValueError(f"urls[{index}]: {exc}") from exc


def _validate_traversal_depth(max_depth: int) -> None:
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        raise ValueError("max_depth must be an integer")
    if not 1 <= max_depth <= MAX_TRAVERSAL_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_TRAVERSAL_DEPTH}")


def _validate_traversal_breadth(max_breadth: int) -> None:
    if isinstance(max_breadth, bool) or not isinstance(max_breadth, int):
        raise ValueError("max_breadth must be an integer")
    if not 1 <= max_breadth <= MAX_TRAVERSAL_BREADTH:
        raise ValueError(f"max_breadth must be between 1 and {MAX_TRAVERSAL_BREADTH}")


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be at least 1")


def _validate_patterns(patterns: tuple[str, ...] | None, field: str) -> None:
    if patterns is None:
        return
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"{field} must contain nonempty strings")


def _add_patterns(payload: dict[str, object], key: str, patterns: tuple[str, ...] | None) -> None:
    if patterns:
        payload[key] = list(patterns)


def _validate_http_url(url: str) -> None:
    if len(url) > MAX_URL_CHARS:
        raise ValueError(f"url must not exceed {MAX_URL_CHARS} characters")
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


def _validate_date_range(start_date: str | None, end_date: str | None) -> None:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start is not None and end is not None and start > end:
        raise ValueError("start_date must not be later than end_date")


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    if _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must use YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid calendar date") from exc


def _is_invalid_url_character(character: str) -> bool:
    return character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F


def _parse_search_response(payload: Mapping[str, object]) -> SearchResponse:
    raw_results = payload.get("results", [])
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        raise TavilyAPIError("Tavily Search returned a response without a results array.")
    results: list[SearchResult] = []
    for item in raw_results:
        parsed = _parse_search_result(item)
        if parsed is not None:
            results.append(parsed)
    if raw_results and not results:
        raise TavilyAPIError("Tavily Search returned no usable result entries.")
    return SearchResponse(
        results=tuple(results),
        answer=_text_field(payload, "answer", compact=False) or None,
        response_time=_number_field(payload, "response_time"),
        usage=_mapping_field(payload, "usage"),
        request_id=_text_field(payload, "request_id", compact=True) or None,
        auto_parameters=_mapping_field(payload, "auto_parameters"),
    )


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
        content=content.strip() if isinstance(content, str) else "",
        score=_number_field(fields, "score"),
        published_date=_text_field(fields, "published_date", compact=True) or None,
        result_id=_text_field(fields, "id", compact=True) or None,
    )


def _parse_extract_response(payload: Mapping[str, object]) -> ExtractResponse:
    raw_results = payload.get("results", [])
    raw_failures = payload.get("failed_results", [])
    if raw_results is None:
        raw_results = []
    if raw_failures is None:
        raw_failures = []
    if not isinstance(raw_results, list) or not isinstance(raw_failures, list):
        raise TavilyAPIError("Tavily Extract returned an invalid response shape.")
    results: list[ExtractResult] = []
    for item in raw_results:
        parsed = _parse_extract_result(item)
        if parsed is not None:
            results.append(parsed)
    failures = [
        failure for item in raw_failures if (failure := _parse_extract_failure(item)) is not None
    ]
    if raw_results and not results and not failures:
        raise TavilyAPIError("Tavily Extract returned no usable content entries.")
    return ExtractResponse(
        results=tuple(results),
        failed_results=tuple(failures),
        response_time=_number_field(payload, "response_time"),
        usage=_mapping_field(payload, "usage"),
        request_id=_text_field(payload, "request_id", compact=True) or None,
    )


def _parse_extract_result(item: object) -> ExtractResult | None:
    if not isinstance(item, Mapping):
        return None
    fields = cast(Mapping[str, object], item)
    url = _text_field(fields, "url", compact=True)
    if not url:
        return None
    content = fields.get("raw_content")
    return ExtractResult(
        url=url,
        content=content.strip() if isinstance(content, str) else "",
    )


def _parse_extract_failure(item: object) -> ExtractFailure | None:
    if not isinstance(item, Mapping):
        return None
    fields = cast(Mapping[str, object], item)
    url = _text_field(fields, "url", compact=True) or "<unknown URL>"
    error = _text_field(fields, "error", compact=True) or "Unknown extraction error"
    return ExtractFailure(url=url, error=error)


def _parse_crawl_response(payload: Mapping[str, object], requested_url: str) -> CrawlResponse:
    raw_results = payload.get("results", [])
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        raise TavilyAPIError("Tavily Crawl returned a response without a results array.")
    results: list[CrawlResult] = []
    for item in raw_results:
        parsed = _parse_crawl_result(item)
        if parsed is not None:
            results.append(parsed)
    if raw_results and not results:
        raise TavilyAPIError("Tavily Crawl returned no usable page entries.")
    return CrawlResponse(
        base_url=_text_field(payload, "base_url", compact=True) or requested_url,
        results=tuple(results),
        response_time=_number_field(payload, "response_time"),
        usage=_mapping_field(payload, "usage"),
        request_id=_text_field(payload, "request_id", compact=True) or None,
    )


def _parse_crawl_result(item: object) -> CrawlResult | None:
    if not isinstance(item, Mapping):
        return None
    fields = cast(Mapping[str, object], item)
    url = _text_field(fields, "url", compact=True)
    if not url:
        return None
    content = fields.get("raw_content")
    return CrawlResult(
        url=url,
        content=content.strip() if isinstance(content, str) else "",
    )


def _parse_map_response(payload: Mapping[str, object], requested_url: str) -> MapResponse:
    raw_results = payload.get("results", [])
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        raise TavilyAPIError("Tavily Map returned a response without a results array.")
    results = [result for item in raw_results if isinstance(item, str) and (result := item.strip())]
    if raw_results and not results:
        raise TavilyAPIError("Tavily Map returned no usable URLs.")
    return MapResponse(
        base_url=_text_field(payload, "base_url", compact=True) or requested_url,
        results=tuple(results),
        response_time=_number_field(payload, "response_time"),
        usage=_mapping_field(payload, "usage"),
        request_id=_text_field(payload, "request_id", compact=True) or None,
    )


def _text_field(item: Mapping[str, object], key: str, *, compact: bool) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        return ""
    if compact:
        return " ".join(value.split())
    return value.strip()


def _number_field(item: Mapping[str, object], key: str) -> float | None:
    value = item.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _mapping_field(item: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    value = item.get(key)
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _decode_json(response: httpx.Response, operation: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise TavilyAPIError(
            f"Tavily {operation} returned an invalid JSON response (HTTP {response.status_code}).",
            status_code=response.status_code,
        ) from None
    if not isinstance(payload, dict):
        raise TavilyAPIError(
            f"Tavily {operation} returned a JSON value that is not an object.",
            status_code=response.status_code,
        )
    return cast(dict[str, object], payload)


def _status_error(response: httpx.Response, operation: str) -> TavilyAPIError:
    status = response.status_code
    detail = _error_detail(response)
    if status == 400:
        message = (
            f"Tavily {operation} rejected the request (HTTP 400). "
            "Check the request parameters, URL, dates, filters, and traversal options."
        )
    elif status == 401:
        message = (
            f"Tavily {operation} authentication failed (HTTP 401). "
            "Check the tavily_api_key credential."
        )
    elif status == 403:
        message = f"Tavily {operation} was rejected (HTTP 403)."
    elif status == 429:
        message = f"Tavily {operation} exceeded the account rate limit (HTTP 429)."
    elif status == 432:
        message = (
            f"Tavily {operation} exceeded the plan usage limit (HTTP 432). "
            "Upgrade the plan or wait for the next billing period."
        )
    elif status == 433:
        message = (
            f"Tavily {operation} exceeded the pay-as-you-go limit (HTTP 433). "
            "Increase the limit in the Tavily dashboard."
        )
    elif status >= 500:
        message = f"Tavily {operation} is temporarily unavailable (HTTP {status})."
    else:
        message = f"Tavily {operation} failed (HTTP {status})."
    if detail:
        message += f" Server message: {detail}"
    return TavilyAPIError(
        message,
        status_code=status,
        retryable=status in _RETRYABLE_STATUS_CODES or status >= 500,
        retry_after=_retry_after(response),
    )


def _error_detail(response: httpx.Response) -> str:
    fallback = _compact(response.text)
    try:
        document = response.json()
    except ValueError:
        return fallback
    if not isinstance(document, dict):
        return fallback
    detail = document.get("detail")
    if isinstance(detail, str):
        return _compact(detail)
    if isinstance(detail, dict):
        message = detail.get("error")
        return _compact(message) if isinstance(message, str) else fallback
    if isinstance(detail, list):
        messages: list[str] = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            message = item.get("msg")
            if not isinstance(message, str) or not message.strip():
                continue
            location = item.get("loc")
            prefix = ".".join(str(part) for part in location) if isinstance(location, list) else ""
            messages.append(f"{prefix}: {message}" if prefix else message)
        if messages:
            return _compact("; ".join(messages))
        return fallback
    error = document.get("error")
    if isinstance(error, str):
        return _compact(error)
    return fallback


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


def _retry_delay(error: TavilyAPIError, attempt: int) -> float:
    if error.retry_after is not None:
        return min(error.retry_after, _MAX_RETRY_DELAY_SECONDS)
    return min(0.5 * (2**attempt), _MAX_RETRY_DELAY_SECONDS)


def _network_error(exc: httpx.RequestError, operation: str) -> TavilyAPIError:
    # Local protocol errors can quote outgoing header values; keep only the type.
    detail = "" if isinstance(exc, httpx.LocalProtocolError) else str(exc).strip()
    description = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return TavilyAPIError(
        f"Tavily {operation} connection failed: {description}",
        retryable=not isinstance(exc, httpx.LocalProtocolError),
    )
