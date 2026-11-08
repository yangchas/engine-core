#!/usr/bin/env python3
"""Fetch and activate a versioned stock listing snapshot."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from engine_core.data_pipeline.job_contract import contract_main
from engine_core.data_pipeline.stock_listing import run


class ContractParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({'contract': 1, 'status': 'failed', 'error_code': 'CONFIG_ERROR',
                          'retryable': False, 'detail': {'message': message}}))
        self.exit(64)


@contract_main('build_stock_listing', dataset='stock_listing')
def main(argv=None):
    parser = ContractParser(description=__doc__)
    parser.add_argument('--date', required=True)
    args = parser.parse_args(argv)
    code, reply = run(args.date)
    print(json.dumps(reply, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
