# Tavily Search

A `python-inprocess` OURO example plugin that adds four read-only tools backed by the
[Tavily Search, Extract, Crawl and Map APIs](https://docs.tavily.com/documentation/api-reference/introduction):

| Tool | Capability |
|---|---|
| `tavily__search` | Search the web with configurable depth, topic, date filters, domain filters, and an optional Tavily-generated answer. |
| `tavily__extract` | Extract page content from one to 20 URLs, with Markdown or plain-text output and per-URL failure reporting. |
| `tavily__crawl` | Crawl a website from a root URL and return extracted page content, with depth, breadth, page-limit, regex path/domain filters, and optional instructions. |
| `tavily__map` | Discover URLs from a root URL without extracting page content, with the same traversal controls. |

All four tools use one pooled `httpx.AsyncClient` per enabled plugin generation. Requests retry
HTTP 408, 429, and 5xx responses or transport failures, honor `Retry-After` up to a bounded delay,
and return classified errors for authentication, plan and pay-as-you-go limits. The package bundles
no third-party code and relies on OURO's existing `httpx` dependency.

## Configuration

The required `tavily_api_key` credential accepts either an environment reference or a directly saved
key. Ordinary values control the endpoint and request behavior:

| Field | Default | Meaning |
|---|---|---|
| `base_url` | `https://api.tavily.com` | Tavily API root used by all four tools. Do not append an endpoint path. |
| `timeout_seconds` | `30` | Total time budget for one Search or Extract call, including retries. Range: 1-120. |
| `site_timeout_seconds` | `180` | Total client-side time budget for one Crawl or Map call. Range: 10-300. Tavily's server-side default is 150 seconds. |
| `max_retries` | `2` | Additional attempts for retryable failures. Range: 0-5. |
| `default_max_results` | `10` | Maximum results used when `max_results` is omitted. Range: 1-20. |
| `default_search_depth` | `basic` | Search depth used when `search_depth` is omitted. |
| `chunks_per_source` | `3` | Content chunks per search result. Range: 1-3. |
| `default_extract_depth` | `basic` | Extraction depth used when `extract_depth` is omitted. |
| `default_format` | `markdown` | Extracted content format used when `format` is omitted. |

The API key is sent only to `base_url`. Use an HTTPS endpoint outside loopback; a plain HTTP base URL
transmits the key without transport encryption. The manifest requests `credentials.use` for the key
and `network.outbound` to disclose remote requests.

Tavily charges by request depth: basic, fast, and ultra-fast search cost 1 credit, while advanced
search costs 2. Basic extraction costs 1 credit per 5 successful URLs and advanced extraction costs
2 credits per 5 successful URLs. Crawl combines mapping and extraction costs. Map with instructions
costs 2 credits per 10 successful pages instead of 1; Crawl with instructions uses the same mapping
rate before adding extraction credits.

## Install and enable

From the repository root in PowerShell:

```powershell
uv run --frozen ouro plugin install ./examples/plugins/tavily-search --home ./.ouro-dev --json
uv run --frozen ouro plugin configure dev.ouro.examples.tavily_search --home ./.ouro-dev --values '{}' --credentials '{"tavily_api_key":"env:TAVILY_API_KEY"}' --json
uv run --frozen ouro plugin enable dev.ouro.examples.tavily_search --home ./.ouro-dev --json
```

`--values '{}'` accepts the schema defaults. To customize ordinary values, submit another complete
draft; omitted fields return to their schema defaults:

```powershell
uv run --frozen ouro plugin configure dev.ouro.examples.tavily_search --home ./.ouro-dev --values '{"base_url":"https://api.tavily.com","timeout_seconds":45,"default_max_results":8,"default_search_depth":"advanced"}' --credentials '{"tavily_api_key":"env:TAVILY_API_KEY"}' --json
```

A directly saved key uses the same slot:

```powershell
uv run --frozen ouro plugin configure dev.ouro.examples.tavily_search --home ./.ouro-dev --values '{}' --credentials '{"tavily_api_key":{"type":"inline","value":"tvly-..."}}' --json
```

Direct keys are stored in protected local plugin state and are masked by ordinary queries. Do not
commit exported configuration drafts or place a real key in a plugin package.

## Tool behavior

- `tavily__search` requires `query`. Optional `max_results`, `search_depth`, `topic`, `time_range`,
  `start_date`, `end_date`, `include_domains`, `exclude_domains`, and `include_answer` refine the
  request. `include_answer` accepts `none`, `basic`, or `advanced`.
- `include_domains` restricts results to the listed domains. Date filters automatically enable
  Tavily's `filter_by_published_date`, so results without a detectable publication date are removed.
- Search always requests publication dates and credit usage. Full page content is fetched separately
  with `tavily__extract` or `tavily__crawl` instead of requesting raw content from every search result.
- `tavily__extract` requires `urls` with 1-20 absolute HTTP(S) URLs. Without `query`, Tavily returns
  full page content. With `query`, Tavily reranks extracted chunks; `chunks_per_source` then limits
  each URL to 1-5 chunks.
- Extract reports successful and failed URLs separately. A partial failure is returned as a normal
  result containing the success and failure lists; a complete failure is marked as an error.
- `tavily__crawl` requires `url` and returns extracted page content. `max_depth` (1-5, default 1),
  `max_breadth` (1-500, default 20), `limit` (default 50), path/domain regex filters, and
  `allow_external` control the traversal. `instructions` enables natural-language guidance and
  optional `chunks_per_source`; `extract_depth` and `format` control page content.
- `tavily__map` requires `url` and returns discovered URLs without page content. It shares the same
  traversal controls and supports `instructions`.
- Crawl and Map default `allow_external` to true, matching Tavily. Set it to false to stay on the
  starting domain. Start with a shallow depth and a small limit before crawling a large site.
- Crawl and Map use `site_timeout_seconds`; Tavily applies its own 150-second server default. Tool
  output is bounded by the Host's configured tool result limit, so extract fewer URLs or crawl fewer
  pages when reading long content.
- Expected API failures return an error tool result with the HTTP status and, when available, the
  Tavily error detail. Invalid tool arguments are rejected by the Host schema or handler before a
  request is sent.

The plugin makes no local business-data writes and keeps no search cache.
