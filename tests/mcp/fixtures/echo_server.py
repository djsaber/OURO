from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP


def build_server(*, host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    server = FastMCP(
        "OURO MCP integration test",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )

    @server.tool()
    def echo(value: str) -> dict[str, str]:
        """Echo a value through MCP."""

        return {"value": value}

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "http", "sse"), required=True)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = build_server(port=args.port)
    if args.transport == "stdio":
        server.run(transport="stdio")
    elif args.transport == "http":
        server.run(transport="streamable-http")
    else:
        server.run(transport="sse")


if __name__ == "__main__":
    main()
