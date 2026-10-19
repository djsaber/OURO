"""OURO tools backed by the DeepSeek Search and Fetch APIs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from time import monotonic

from ouro.sdk.plugins import v5 as sdk

from .client import (
    MAX_FETCH_URL_CHARS,
    MAX_ORI_QUESTION_CHARS,
    MAX_QUERY_CHARS,
    MAX_SEARCH_COUNT,
    MIN_SEARCH_COUNT,
    DeepSeekAPIError,
    DeepSeekSearchClient,
    FetchResult,
    SearchResult,
    Settings,
)

# The Host requires these descriptions and tool policies to match the manifest exactly.
SEARCH_TOOL_DESCRIPTION = (
    "Search the web with DeepSeek Search and return ranked results with titles, URLs, "
    "publication dates, summaries, and optional page content. Queries support site:, "
    '-site:, -term, "exact phrase", lang:, after:, and before: operators. '
    "Use deepseek__fetch to read a specific URL in full."
)
FETCH_TOOL_DESCRIPTION = (
    "Fetch one public HTTP(S) page with DeepSeek Fetch and return its title and Markdown "
    "content. The URL must include a host and cannot contain embedded credentials."
)

SEARCH_PARAMETERS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_QUERY_CHARS,
            "description": (
                f"Search query (1-{MAX_QUERY_CHARS} Unicode characters). Supports site:, "
                '-site:, -term, "exact phrase", lang:, after:, and before: operators.'
            ),
        },
        "count": {
            "type": "integer",
            "minimum": MIN_SEARCH_COUNT,
            "maximum": MAX_SEARCH_COUNT,
            "description": (
                f"Number of results to return ({MIN_SEARCH_COUNT}-{MAX_SEARCH_COUNT}). "
                "Defaults to the plugin's default_count setting."
            ),
        },
        "ori_question": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_ORI_QUESTION_CHARS,
            "description": (
                f"The user's original question (1-{MAX_ORI_QUESTION_CHARS} characters) "
                "when query is a reformulation. Omit it if unknown."
            ),
        },
        "need_content": {
            "type": "boolean",
            "description": (
                "Return each result's page content in addition to its summary. Defaults "
                "to the plugin's default_need_content setting."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

FETCH_PARAMETERS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_FETCH_URL_CHARS,
            "format": "uri",
            "description": (
                "Absolute HTTP(S) URL with a host and no embedded username or password."
            ),
        }
    },
    "required": ["url"],
    "additionalProperties": False,
}


class DeepSeekSearchPlugin:
    def __init__(self, context: sdk.PluginContext) -> None:
        missing = {"credentials.use", "network.outbound"} - set(context.permissions)
        if missing:
            raise RuntimeError(
                "Deepseek Search requires authorized permissions: " + ", ".join(sorted(missing))
            )
        self._settings = Settings.from_configuration(context.configuration)
        self._credentials = context.credentials
        self._client: DeepSeekSearchClient | None = None

    async def prepare(self) -> sdk.ContributionSet:
        return sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="search",
                    exposed_name="deepseek__search",
                    description=SEARCH_TOOL_DESCRIPTION,
                    handler=self.search,
                    parameters_schema=SEARCH_PARAMETERS_SCHEMA,
                    permission_mode="auto",
                    effect="read_only",
                ),
                sdk.ToolContribution(
                    id="fetch",
                    exposed_name="deepseek__fetch",
                    description=FETCH_TOOL_DESCRIPTION,
                    handler=self.fetch,
                    parameters_schema=FETCH_PARAMETERS_SCHEMA,
                    permission_mode="auto",
                    effect="read_only",
                ),
            ]
        )

    async def start(self) -> None:
        if self._client is not None:
            return
        api_key = self._credentials.resolve("deepseek_api_key")
        if not api_key.strip():
            raise RuntimeError("DeepSeek API key is empty")
        self._client = DeepSeekSearchClient(self._settings, api_key)

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        del reason
        client = self._client
        if client is None:
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError("Deepseek Search client shutdown deadline has already passed")
        try:
            async with asyncio.timeout(remaining):
                await client.aclose()
        except TimeoutError:
            raise RuntimeError("Deepseek Search client shutdown exceeded its deadline") from None
        if self._client is client:
            self._client = None

    async def search(self, call: sdk.ToolCall) -> sdk.ToolOutput:
        query = _required_text(call.arguments, "query")
        count = _optional_count(call.arguments, default=self._settings.default_count)
        need_content = _optional_boolean(
            call.arguments, "need_content", default=self._settings.default_need_content
        )
        ori_question = _optional_text(call.arguments, "ori_question")
        try:
            results = await self._require_client().search(
                query,
                count=count,
                need_content=need_content,
                ori_question=ori_question,
            )
        except DeepSeekAPIError as exc:
            return _error_output(exc)
        return sdk.ToolOutput(
            content=_format_search_results(
                query,
                results,
                max_content_chars=self._settings.max_content_chars,
            ),
            structured=_search_structured(
                query,
                results,
                content_included=any(result.content for result in results),
            ),
        )

    async def fetch(self, call: sdk.ToolCall) -> sdk.ToolOutput:
        url = _required_text(call.arguments, "url")
        try:
            result = await self._require_client().fetch(url)
        except DeepSeekAPIError as exc:
            return _error_output(exc)
        return sdk.ToolOutput(
            content=_format_fetch(result),
            structured={
                "url": result.url,
                "title": result.title,
                "content_chars": len(result.content),
            },
        )

    def _require_client(self) -> DeepSeekSearchClient:
        client = self._client
        if client is None:
            raise RuntimeError("DeepSeek client is not running")
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


def _optional_count(arguments: Mapping[str, object], *, default: int) -> int:
    value = arguments.get("count")
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("count must be an integer")
    return value


def _optional_boolean(arguments: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _format_search_results(
    query: str,
    results: tuple[SearchResult, ...],
    *,
    max_content_chars: int,
) -> str:
    if not results:
        return f"No web results found for {json.dumps(query, ensure_ascii=False)}."
    label = "result" if len(results) == 1 else "results"
    lines = [
        f"Search results for {json.dumps(query, ensure_ascii=False)} ({len(results)} {label}):"
    ]
    for index, result in enumerate(results, start=1):
        lines.extend(("", f"{index}. {result.title}", f"URL: {result.url}"))
        metadata: list[str] = []
        if result.site_name:
            metadata.append(result.site_name)
        if result.date:
            metadata.append(f"Published: {result.date}")
        if result.rerank_score is not None:
            metadata.append(f"Score: {result.rerank_score:g}")
        if result.authority is not None and result.authority >= 0:
            metadata.append(f"Authority: {result.authority}")
        if metadata:
            lines.append(" | ".join(metadata))
        if result.summary:
            lines.append(f"Summary: {result.summary}")
        elif result.snippet:
            lines.append(f"Snippet: {result.snippet}")
        if result.content:
            content = result.content
            if len(content) > max_content_chars:
                content = content[:max_content_chars].rstrip() + (
                    f"\n...[content truncated at {max_content_chars} characters]"
                )
            lines.extend(("", "Content:", content))
    return "\n".join(lines)


def _format_fetch(result: FetchResult) -> str:
    lines = [f"URL: {result.url}"]
    if result.title:
        lines.append(f"Title: {result.title}")
    lines.extend(("", result.content))
    return "\n".join(lines)


def _search_structured(
    query: str,
    results: tuple[SearchResult, ...],
    *,
    content_included: bool,
) -> dict[str, object]:
    return {
        "query": query,
        "count": len(results),
        "content_included": content_included,
        "results": [
            {
                "title": result.title,
                "url": result.url,
                "snippet": result.snippet,
                "summary": result.summary,
                "site_name": result.site_name,
                "date": result.date,
                "authority": result.authority,
                "rerank_score": result.rerank_score,
                "content_chars": len(result.content),
            }
            for result in results
        ],
    }


def _error_output(error: DeepSeekAPIError) -> sdk.ToolOutput:
    structured: dict[str, object] = {
        "error_type": "deepseek_api_error",
        "message": str(error),
    }
    if error.status_code is not None:
        structured["status_code"] = error.status_code
    if error.code is not None:
        structured["code"] = error.code
    return sdk.ToolOutput(content=str(error), is_error=True, structured=structured)
