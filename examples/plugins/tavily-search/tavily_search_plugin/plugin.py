"""OURO tools backed by the Tavily Search and Extract APIs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import cast

from ouro.sdk.plugins import v5 as sdk

from .client import (
    MAX_EXTRACT_CHUNKS,
    MAX_EXTRACT_URLS,
    MAX_RESULTS,
    MAX_TRAVERSAL_BREADTH,
    MAX_TRAVERSAL_DEPTH,
    MAX_URL_CHARS,
    AnswerMode,
    ContentFormat,
    CrawlResponse,
    ExtractDepth,
    ExtractResponse,
    MapResponse,
    SearchDepth,
    SearchResponse,
    Settings,
    TavilyAPIError,
    TavilyClient,
    TimeRange,
    Topic,
)

# The Host requires these descriptions and tool policies to match the manifest exactly.
SEARCH_TOOL_DESCRIPTION = (
    "Search the web with Tavily and return ranked results with titles, URLs, relevance "
    "scores, publication dates, and content snippets. Supports search depth, topic, time "
    "range, domain filters, and an optional LLM-generated answer. Use tavily__extract to "
    "retrieve full page content."
)
EXTRACT_TOOL_DESCRIPTION = (
    "Extract page content from one or more public HTTP(S) URLs with Tavily. Accepts up to "
    "20 URLs per call, returns Markdown or plain text, and reports per-URL failures."
)
CRAWL_TOOL_DESCRIPTION = (
    "Crawl a website from a root URL with Tavily and return extracted page content for "
    "discovered pages. Supports depth, breadth, page limits, path and domain regex filters, "
    "optional natural-language instructions, and Markdown or plain text. Use tavily__map to "
    "discover URLs without extracting content."
)
MAP_TOOL_DESCRIPTION = (
    "Map a website from a root URL with Tavily and return discovered URLs without page "
    "content. Supports depth, breadth, page limits, path and domain regex filters, optional "
    "natural-language instructions, and external-link control."
)

_TRAVERSAL_PROPERTIES: Mapping[str, object] = {
    "max_depth": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_TRAVERSAL_DEPTH,
        "description": (
            f"Maximum link depth from the root URL (1-{MAX_TRAVERSAL_DEPTH}). Defaults to 1."
        ),
    },
    "max_breadth": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_TRAVERSAL_BREADTH,
        "description": (
            f"Maximum links followed per page (1-{MAX_TRAVERSAL_BREADTH}). Defaults to 20."
        ),
    },
    "limit": {
        "type": "integer",
        "minimum": 1,
        "description": (
            "Total links Tavily may process before stopping. Defaults to 50. Higher limits "
            "cost more and can take longer."
        ),
    },
    "select_paths": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": "Regex patterns to include only matching URL paths.",
    },
    "select_domains": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": "Regex patterns to include only matching domains or subdomains.",
    },
    "exclude_paths": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": "Regex patterns to exclude matching URL paths.",
    },
    "exclude_domains": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": "Regex patterns to exclude matching domains or subdomains.",
    },
    "allow_external": {
        "type": "boolean",
        "description": (
            "Include external-domain links in results. Defaults to true; set false to stay "
            "on the starting domain."
        ),
    },
}

SEARCH_PARAMETERS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "The search query to execute with Tavily.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_RESULTS,
            "description": (
                f"Maximum number of results to return (1-{MAX_RESULTS}). Defaults to the "
                "plugin's default_max_results setting."
            ),
        },
        "search_depth": {
            "type": "string",
            "enum": ["advanced", "basic", "fast", "ultra-fast"],
            "description": (
                "Latency versus relevance tradeoff. Basic, fast, and ultra-fast cost 1 "
                "credit; advanced costs 2. Defaults to the plugin's default_search_depth."
            ),
        },
        "topic": {
            "type": "string",
            "enum": ["general", "news", "finance"],
            "description": "Search category. Use news for current events and finance for markets.",
        },
        "time_range": {
            "type": "string",
            "enum": ["day", "week", "month", "year"],
            "description": (
                "Limit results to this recent window. Date filters remove results without "
                "a detectable publication date."
            ),
        },
        "start_date": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "format": "date",
            "description": "Include results published on or after this date (YYYY-MM-DD).",
        },
        "end_date": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "format": "date",
            "description": "Include results published on or before this date (YYYY-MM-DD).",
        },
        "include_domains": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Restrict results to these domains when provided.",
        },
        "exclude_domains": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Exclude results from these domains.",
        },
        "include_answer": {
            "type": "string",
            "enum": ["none", "basic", "advanced"],
            "description": (
                "Include a Tavily-generated answer: none, a quick basic answer, or a more "
                "detailed advanced answer. Defaults to none."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

EXTRACT_PARAMETERS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "urls": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_URL_CHARS,
            },
            "minItems": 1,
            "maxItems": MAX_EXTRACT_URLS,
            "description": "Public HTTP(S) URLs to extract, up to 20 per call.",
        },
        "query": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional user intent. When provided, Tavily reranks extracted chunks "
                "against this query instead of returning the complete page."
            ),
        },
        "chunks_per_source": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_EXTRACT_CHUNKS,
            "description": (
                f"Maximum chunks per URL (1-{MAX_EXTRACT_CHUNKS}); requires query. "
                "Defaults to Tavily's server default."
            ),
        },
        "extract_depth": {
            "type": "string",
            "enum": ["basic", "advanced"],
            "description": (
                "Basic extraction is faster; advanced extraction includes tables and "
                "embedded content and costs 2 credits per 5 successful URLs. Defaults to "
                "the plugin's default_extract_depth."
            ),
        },
        "format": {
            "type": "string",
            "enum": ["markdown", "text"],
            "description": "Extracted content format. Defaults to the plugin's default_format.",
        },
    },
    "required": ["urls"],
    "additionalProperties": False,
}

CRAWL_PARAMETERS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_URL_CHARS,
            "description": "Absolute HTTP(S) root URL to crawl.",
        },
        "instructions": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional natural-language guidance for discovery. Supplying it increases "
                "mapping cost and enables chunked content."
            ),
        },
        "chunks_per_source": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_EXTRACT_CHUNKS,
            "description": (
                f"Maximum chunks per page (1-{MAX_EXTRACT_CHUNKS}); requires instructions."
            ),
        },
        **_TRAVERSAL_PROPERTIES,
        "extract_depth": {
            "type": "string",
            "enum": ["basic", "advanced"],
            "description": (
                "Basic extraction is faster; advanced extraction includes tables and "
                "embedded content and costs 2 credits per 5 successful pages. Defaults to "
                "the plugin's default_extract_depth."
            ),
        },
        "format": {
            "type": "string",
            "enum": ["markdown", "text"],
            "description": "Extracted content format. Defaults to the plugin's default_format.",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

MAP_PARAMETERS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_URL_CHARS,
            "description": "Absolute HTTP(S) root URL to map.",
        },
        "instructions": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional natural-language guidance for discovery. Supplying it increases "
                "mapping cost."
            ),
        },
        **_TRAVERSAL_PROPERTIES,
    },
    "required": ["url"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class _TraversalArgs:
    max_depth: int
    max_breadth: int
    limit: int
    select_paths: tuple[str, ...] | None
    select_domains: tuple[str, ...] | None
    exclude_paths: tuple[str, ...] | None
    exclude_domains: tuple[str, ...] | None
    allow_external: bool


class TavilySearchPlugin:
    def __init__(self, context: sdk.PluginContext) -> None:
        missing = {"credentials.use", "network.outbound"} - set(context.permissions)
        if missing:
            raise RuntimeError(
                "Tavily Search requires authorized permissions: " + ", ".join(sorted(missing))
            )
        self._settings = Settings.from_configuration(context.configuration)
        self._credentials = context.credentials
        self._client: TavilyClient | None = None

    async def prepare(self) -> sdk.ContributionSet:
        return sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="search",
                    exposed_name="tavily__search",
                    description=SEARCH_TOOL_DESCRIPTION,
                    handler=self.search,
                    parameters_schema=SEARCH_PARAMETERS_SCHEMA,
                    permission_mode="auto",
                    effect="read_only",
                ),
                sdk.ToolContribution(
                    id="extract",
                    exposed_name="tavily__extract",
                    description=EXTRACT_TOOL_DESCRIPTION,
                    handler=self.extract,
                    parameters_schema=EXTRACT_PARAMETERS_SCHEMA,
                    permission_mode="auto",
                    effect="read_only",
                ),
                sdk.ToolContribution(
                    id="crawl",
                    exposed_name="tavily__crawl",
                    description=CRAWL_TOOL_DESCRIPTION,
                    handler=self.crawl,
                    parameters_schema=CRAWL_PARAMETERS_SCHEMA,
                    permission_mode="auto",
                    effect="read_only",
                ),
                sdk.ToolContribution(
                    id="map",
                    exposed_name="tavily__map",
                    description=MAP_TOOL_DESCRIPTION,
                    handler=self.map,
                    parameters_schema=MAP_PARAMETERS_SCHEMA,
                    permission_mode="auto",
                    effect="read_only",
                ),
            ]
        )

    async def start(self) -> None:
        if self._client is not None:
            return
        api_key = self._credentials.resolve("tavily_api_key")
        if not api_key.strip():
            raise RuntimeError("Tavily API key is empty")
        self._client = TavilyClient(self._settings, api_key)

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        del reason
        client = self._client
        if client is None:
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError("Tavily Search client shutdown deadline has already passed")
        try:
            async with asyncio.timeout(remaining):
                await client.aclose()
        except TimeoutError:
            raise RuntimeError("Tavily Search client shutdown exceeded its deadline") from None
        if self._client is client:
            self._client = None

    async def search(self, call: sdk.ToolCall) -> sdk.ToolOutput:
        query = _required_text(call.arguments, "query")
        max_results = _integer_argument(
            call.arguments, "max_results", default=self._settings.default_max_results
        )
        search_depth = cast(
            SearchDepth,
            _optional_choice(
                call.arguments,
                "search_depth",
                allowed={"advanced", "basic", "fast", "ultra-fast"},
                default=self._settings.default_search_depth,
            ),
        )
        topic = cast(
            Topic,
            _optional_choice(
                call.arguments,
                "topic",
                allowed={"general", "news", "finance"},
                default="general",
            ),
        )
        time_range = cast(
            TimeRange | None,
            _optional_choice(
                call.arguments,
                "time_range",
                allowed={"day", "week", "month", "year"},
                default=None,
            ),
        )
        start_date = _optional_text(call.arguments, "start_date")
        end_date = _optional_text(call.arguments, "end_date")
        include_domains = _optional_text_list(call.arguments, "include_domains")
        exclude_domains = _optional_text_list(call.arguments, "exclude_domains")
        include_answer = cast(
            AnswerMode,
            _optional_choice(
                call.arguments,
                "include_answer",
                allowed={"none", "basic", "advanced"},
                default="none",
            ),
        )
        try:
            response = await self._require_client().search(
                query,
                max_results=max_results,
                search_depth=search_depth,
                topic=topic,
                time_range=time_range,
                start_date=start_date,
                end_date=end_date,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_answer=include_answer,
            )
        except TavilyAPIError as exc:
            return _error_output(exc)
        return sdk.ToolOutput(
            content=_format_search_response(query, response),
            structured=_search_structured(
                query,
                response,
                search_depth=search_depth,
                topic=topic,
            ),
        )

    async def extract(self, call: sdk.ToolCall) -> sdk.ToolOutput:
        urls = _required_text_list(call.arguments, "urls")
        query = _optional_text(call.arguments, "query")
        chunks_per_source = _optional_integer(call.arguments, "chunks_per_source", default=None)
        extract_depth = cast(
            ExtractDepth,
            _optional_choice(
                call.arguments,
                "extract_depth",
                allowed={"basic", "advanced"},
                default=self._settings.default_extract_depth,
            ),
        )
        content_format = cast(
            ContentFormat,
            _optional_choice(
                call.arguments,
                "format",
                allowed={"markdown", "text"},
                default=self._settings.default_format,
            ),
        )
        try:
            response = await self._require_client().extract(
                urls,
                query=query,
                chunks_per_source=chunks_per_source,
                extract_depth=extract_depth,
                content_format=content_format,
            )
        except TavilyAPIError as exc:
            return _error_output(exc)
        if not response.results and not response.failed_results:
            return sdk.ToolOutput(
                content="Tavily Extract returned no results.",
                structured={"error_type": "tavily_empty_extract"},
                is_error=True,
            )
        return sdk.ToolOutput(
            content=_format_extract_response(response),
            structured=_extract_structured(response),
            is_error=not response.results,
        )

    async def crawl(self, call: sdk.ToolCall) -> sdk.ToolOutput:
        url = _required_text(call.arguments, "url")
        instructions = _optional_text(call.arguments, "instructions")
        chunks_per_source = _optional_integer(call.arguments, "chunks_per_source", default=None)
        traversal = _traversal_args(call.arguments)
        extract_depth = cast(
            ExtractDepth,
            _optional_choice(
                call.arguments,
                "extract_depth",
                allowed={"basic", "advanced"},
                default=self._settings.default_extract_depth,
            ),
        )
        content_format = cast(
            ContentFormat,
            _optional_choice(
                call.arguments,
                "format",
                allowed={"markdown", "text"},
                default=self._settings.default_format,
            ),
        )
        try:
            response = await self._require_client().crawl(
                url,
                extract_depth=extract_depth,
                content_format=content_format,
                instructions=instructions,
                chunks_per_source=chunks_per_source,
                max_depth=traversal.max_depth,
                max_breadth=traversal.max_breadth,
                limit=traversal.limit,
                select_paths=traversal.select_paths,
                select_domains=traversal.select_domains,
                exclude_paths=traversal.exclude_paths,
                exclude_domains=traversal.exclude_domains,
                allow_external=traversal.allow_external,
            )
        except TavilyAPIError as exc:
            return _error_output(exc)
        return sdk.ToolOutput(
            content=_format_crawl_response(response),
            structured=_crawl_structured(response),
        )

    async def map(self, call: sdk.ToolCall) -> sdk.ToolOutput:
        url = _required_text(call.arguments, "url")
        instructions = _optional_text(call.arguments, "instructions")
        traversal = _traversal_args(call.arguments)
        try:
            response = await self._require_client().map(
                url,
                instructions=instructions,
                max_depth=traversal.max_depth,
                max_breadth=traversal.max_breadth,
                limit=traversal.limit,
                select_paths=traversal.select_paths,
                select_domains=traversal.select_domains,
                exclude_paths=traversal.exclude_paths,
                exclude_domains=traversal.exclude_domains,
                allow_external=traversal.allow_external,
            )
        except TavilyAPIError as exc:
            return _error_output(exc)
        return sdk.ToolOutput(
            content=_format_map_response(response),
            structured=_map_structured(response),
        )

    def _require_client(self) -> TavilyClient:
        client = self._client
        if client is None:
            raise RuntimeError("Tavily client is not running")
        return client


def _required_text(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{key} must not be blank")
    return text


def _optional_text(arguments: Mapping[str, object], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    text = value.strip()
    return text or None


def _required_text_list(arguments: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = arguments.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array of strings")
    items = _text_list(value, key)
    if not items:
        raise ValueError(f"{key} must not be empty")
    return items


def _optional_text_list(arguments: Mapping[str, object], key: str) -> tuple[str, ...] | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array of strings")
    return _text_list(value, key) or None


def _text_list(value: list[object], key: str) -> tuple[str, ...]:
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} must contain nonempty strings")
        items.append(item.strip())
    return tuple(items)


def _optional_integer(
    arguments: Mapping[str, object], key: str, *, default: int | None
) -> int | None:
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _integer_argument(arguments: Mapping[str, object], key: str, *, default: int) -> int:
    value = _optional_integer(arguments, key, default=default)
    assert value is not None
    return value


def _boolean_argument(arguments: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _traversal_args(arguments: Mapping[str, object]) -> _TraversalArgs:
    return _TraversalArgs(
        max_depth=_integer_argument(arguments, "max_depth", default=1),
        max_breadth=_integer_argument(arguments, "max_breadth", default=20),
        limit=_integer_argument(arguments, "limit", default=50),
        select_paths=_optional_text_list(arguments, "select_paths"),
        select_domains=_optional_text_list(arguments, "select_domains"),
        exclude_paths=_optional_text_list(arguments, "exclude_paths"),
        exclude_domains=_optional_text_list(arguments, "exclude_domains"),
        allow_external=_boolean_argument(arguments, "allow_external", default=True),
    )


def _optional_choice(
    arguments: Mapping[str, object],
    key: str,
    *,
    allowed: set[str],
    default: str | None,
) -> str | None:
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    text = value.strip()
    if text not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(sorted(allowed))}")
    return text


def _format_search_response(query: str, response: SearchResponse) -> str:
    lines: list[str] = []
    if response.answer:
        lines.extend(("Answer:", response.answer))
    if response.results:
        if lines:
            lines.append("")
        label = "result" if len(response.results) == 1 else "results"
        lines.append(
            f"Search results for {json.dumps(query, ensure_ascii=False)} "
            f"({len(response.results)} {label}):"
        )
        for index, result in enumerate(response.results, start=1):
            lines.extend(("", f"{index}. {result.title}", f"URL: {result.url}"))
            metadata: list[str] = []
            if result.score is not None:
                metadata.append(f"Score: {result.score:g}")
            if result.published_date:
                metadata.append(f"Published: {result.published_date}")
            if metadata:
                lines.append(" | ".join(metadata))
            if result.content:
                lines.extend(("Content:", result.content))
    else:
        if lines:
            lines.append("")
        lines.append(f"No web results found for {json.dumps(query, ensure_ascii=False)}.")
    return "\n".join(lines)


def _format_extract_response(response: ExtractResponse) -> str:
    total = len(response.results) + len(response.failed_results)
    lines = [f"Extracted {len(response.results)} of {total} URLs."]
    for result in response.results:
        lines.extend(("", f"URL: {result.url}"))
        if result.content:
            lines.extend(("Content:", result.content))
        else:
            lines.append("Content: (empty)")
    if response.failed_results:
        lines.extend(("", "Failed URLs:"))
        for failure in response.failed_results:
            lines.append(f"- {failure.url}: {failure.error}")
    return "\n".join(lines)


def _format_crawl_response(response: CrawlResponse) -> str:
    source = response.base_url or "the requested site"
    count = len(response.results)
    if not count:
        return f"No pages were crawled from {source}."
    label = "page" if count == 1 else "pages"
    lines = [f"Crawled {count} {label} from {source}."]
    for result in response.results:
        lines.extend(("", f"URL: {result.url}"))
        if result.content:
            lines.extend(("Content:", result.content))
        else:
            lines.append("Content: (empty)")
    return "\n".join(lines)


def _format_map_response(response: MapResponse) -> str:
    source = response.base_url or "the requested site"
    count = len(response.results)
    if not count:
        return f"No URLs were discovered from {source}."
    lines = [f"Mapped {count} URLs from {source}:"]
    lines.extend(f"- {url}" for url in response.results)
    return "\n".join(lines)


def _search_structured(
    query: str,
    response: SearchResponse,
    *,
    search_depth: SearchDepth,
    topic: Topic,
) -> dict[str, object]:
    structured: dict[str, object] = {
        "query": query,
        "search_depth": search_depth,
        "topic": topic,
        "answer": response.answer,
        "results": [
            {
                "title": result.title,
                "url": result.url,
                "score": result.score,
                "published_date": result.published_date,
                "content_chars": len(result.content),
                "id": result.result_id,
            }
            for result in response.results
        ],
    }
    for key, value in (
        ("response_time", response.response_time),
        ("usage", response.usage),
        ("request_id", response.request_id),
        ("auto_parameters", response.auto_parameters),
    ):
        if value is not None:
            structured[key] = value
    return structured


def _extract_structured(response: ExtractResponse) -> dict[str, object]:
    structured: dict[str, object] = {
        "results": [
            {"url": result.url, "content_chars": len(result.content)} for result in response.results
        ],
        "failed_results": [
            {"url": failure.url, "error": failure.error} for failure in response.failed_results
        ],
    }
    for key, value in (
        ("response_time", response.response_time),
        ("usage", response.usage),
        ("request_id", response.request_id),
    ):
        if value is not None:
            structured[key] = value
    return structured


def _crawl_structured(response: CrawlResponse) -> dict[str, object]:
    structured: dict[str, object] = {
        "base_url": response.base_url,
        "results": [
            {"url": result.url, "content_chars": len(result.content)} for result in response.results
        ],
    }
    for key, value in (
        ("response_time", response.response_time),
        ("usage", response.usage),
        ("request_id", response.request_id),
    ):
        if value is not None:
            structured[key] = value
    return structured


def _map_structured(response: MapResponse) -> dict[str, object]:
    structured: dict[str, object] = {
        "base_url": response.base_url,
        "results": list(response.results),
    }
    for key, value in (
        ("response_time", response.response_time),
        ("usage", response.usage),
        ("request_id", response.request_id),
    ):
        if value is not None:
            structured[key] = value
    return structured


def _error_output(error: TavilyAPIError) -> sdk.ToolOutput:
    structured: dict[str, object] = {
        "error_type": "tavily_api_error",
        "message": str(error),
    }
    if error.status_code is not None:
        structured["status_code"] = error.status_code
    return sdk.ToolOutput(content=str(error), is_error=True, structured=structured)
