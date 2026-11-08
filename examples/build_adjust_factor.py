#!/usr/bin/env python3
"""Acquire adjustment-factor snapshots for a trading day."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from engine_core.data_pipeline.adjust_factor import run
from engine_core.data_pipeline.job_contract import contract_main


class ContractParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({'contract': 1, 'status': 'failed', 'error_code': 'CONFIG_ERROR',
                          'retryable': False, 'detail': {'message': message}}))
        self.exit(64)


@contract_main('build_adjust_factor', dataset='adjust_factor')
def main(argv=None):
    parser = ContractParser(description=__doc__)
    parser.add_argument('--date', required=True)
    parser.add_argument('--mode', required=True, choices=('incremental', 'bootstrap'))
    args = parser.parse_args(argv)
    code, reply = run({'trade_date': args.date, 'args': {'mode': args.mode}},
                      os.environ.get('CORE_DATA_ROOT', str(Path.home() / 'core-data')),
                      dry_run=os.environ.get('GUARDIAN_DRY_RUN') == '1')
    print(json.dumps(reply, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
