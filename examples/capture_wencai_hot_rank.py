"""Fetch a dated WenCai hot rank; publish only with --execute."""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from engine_core.data_pipeline.job_contract import contract_main
from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
from engine_core.data_pipeline.providers.wencai_hot_rank import fetch


@contract_main('capture_wencai_hot_rank')
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trade-date', required=True)
    parser.add_argument('--artifact-root', type=Path,
                        default=Path(os.environ.get('CORE_DATA_ROOT', Path.home() / 'core-data')))
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    client = None
    if args.execute:
        import redis
        url = os.environ.get('REDIS_URL')
        if not url:
            parser.error('REDIS_URL is required for --execute')
        client = redis.Redis.from_url(url, socket_timeout=10, socket_connect_timeout=5)
    try:
        now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='microseconds')
        runner = JobRunner(artifact_root=args.artifact_root, redis=client,
                           budget=RequestBudget(min_interval_seconds=10))
        result = runner.run('hot_rank', args.trade_date, fetch=lambda day: fetch(day), now=now,
                            source='wencai_hot_rank', publish=args.execute, dry_run=not args.execute)
        print(f"ROW_COUNT {result.manifest['row_count']}")
        print(f"VERSION {result.manifest.get('version', 'DRY_RUN')}")
        return 0
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
