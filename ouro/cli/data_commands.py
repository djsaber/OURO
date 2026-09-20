from __future__ import annotations

import argparse
import json

from ouro.data_commands import data_request, format_data_result
from ouro.local_application import build_local_data_transfer_service


def run_data_command(args: argparse.Namespace) -> int:
    service = build_local_data_transfer_service(
        args.home,
        session_backend=args.command_session_backend or args.session_backend,
    )
    result = service.transfer(data_request(args))
    if args.json:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
    else:
        print(format_data_result(result))
    return 0
