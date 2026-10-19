# Deepseek Search

A `python-inprocess` OURO example plugin that adds two read-only tools backed by the
[DeepSeek Search and Fetch APIs](https://api-docs.deepseek.com/zh-cn/guides/search):

| Tool | Capability |
|---|---|
| `deepseek__search` | Search the web and return ranked titles, URLs, publication dates, summaries, and optional page content. |
| `deepseek__fetch` | Fetch one public HTTP(S) page and return its title and Markdown content. |

Both tools use one pooled `httpx.AsyncClient` per enabled plugin generation. Search retries HTTP 408,
429, and 5xx responses or transport failures, honors `Retry-After` up to a bounded delay, and caps the
model-visible content of each search result. Full pages are read with `deepseek__fetch`. The package
bundles no third-party code and relies on OURO's existing `httpx` dependency.

## Configuration

The required `deepseek_api_key` credential accepts either an environment reference or a directly saved
key. Ordinary values control the endpoint and request behavior:

| Field | Default | Meaning |
|---|---|---|
| `base_url` | `https://api.deepseek.com` | DeepSeek API root used by both tools. Do not append `/v1` or an endpoint path. |
| `timeout_seconds` | `30` | Total time budget for one call, including retries. Range: 1-120. |
| `max_retries` | `2` | Additional attempts for retryable failures. Range: 0-5. |
| `default_count` | `10` | Result count used when the search tool omits `count`. Range: 1-50. |
| `default_need_content` | `false` | Whether search returns page content when the tool omits `need_content`. |
| `max_content_chars` | `4000` | Per-result content limit in model-visible search output. Range: 500-20000. |

The API key is sent only to `base_url`. Use an HTTPS endpoint outside loopback; a plain HTTP base URL
transmits the key without transport encryption. Search supports the DeepSeek query operators `site:`,
`-site:`, `-term`, `"exact phrase"`, `lang:`, `after:`, and `before:`. The manifest requests
`credentials.use` for the key and `network.outbound` to disclose remote requests.

## Install and enable

From the repository root in PowerShell:

```powershell
uv run --frozen ouro plugin install ./examples/plugins/deepseek-search --home ./.ouro-dev --json
uv run --frozen ouro plugin configure dev.ouro.examples.deepseek_search --home ./.ouro-dev --values '{}' --credentials '{"deepseek_api_key":"env:DEEPSEEK_API_KEY"}' --json
uv run --frozen ouro plugin enable dev.ouro.examples.deepseek_search --home ./.ouro-dev --json
```

`--values '{}'` accepts the schema defaults. To customize ordinary values, submit another complete
draft; omitted fields return to their schema defaults:

```powershell
uv run --frozen ouro plugin configure dev.ouro.examples.deepseek_search --home ./.ouro-dev --values '{"base_url":"https://api.deepseek.com","timeout_seconds":45,"default_count":8,"default_need_content":true}' --credentials '{"deepseek_api_key":"env:DEEPSEEK_API_KEY"}' --json
```

A directly saved key uses the same slot:

```powershell
uv run --frozen ouro plugin configure dev.ouro.examples.deepseek_search --home ./.ouro-dev --values '{}' --credentials '{"deepseek_api_key":{"type":"inline","value":"sk-..."}}' --json
```

Direct keys are stored in protected local plugin state and are masked by ordinary queries. Do not
commit exported configuration drafts or place a real key in a plugin package.

## Tool behavior

- `deepseek__search` requires `query`; `count`, `ori_question`, and `need_content` are optional.
  `ori_question` can improve intent matching when the model reformulates the user's request.
- `deepseek__fetch` requires an absolute HTTP(S) `url` with a host and without embedded credentials.
- Expected API failures return an error tool result with the HTTP status and, when available, the
  DeepSeek error code and message. Invalid tool arguments are rejected by the Host schema before
  execution.
- Result `structured` data contains search metadata and content lengths; full text is returned in the
  model-visible `content` field.

The plugin makes no local business-data writes and keeps no search cache.
