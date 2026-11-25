#!/usr/bin/env python3
"""Print Core TD DDL; execution is opt-in and requires an injected client."""
from __future__ import annotations

import argparse

from engine_core.data_pipeline.td_adapter import DDL, apply_ddl, validate_database_name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="core_data")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--client", help="module:function factory; required with --apply")
    args = parser.parse_args(argv)
    database = validate_database_name(args.database)
    if database != "core_data": parser.error("only --database core_data is permitted by this card")
    statements = [statement for statement in DDL if statement.startswith("CREATE DATABASE IF NOT EXISTS core_data") or "core_data." in statement]
    if not args.apply:
        for statement in statements: print(statement + ";")
        print("DDL_MODE=PRINT_ONLY")
        return 0
    if not args.client: parser.error("--apply requires --client module:function")
    module_name, separator, function = args.client.partition(":")
    if not separator: parser.error("--client must be module:function")
    client = getattr(__import__(module_name, fromlist=[function]), function)()
    apply_ddl(client, database)
    print("DDL_APPLIED database=core_data statement_count=" + str(len(statements)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
