"""Pure-memory card authorization checks; run directly with Python."""
import contextlib
import io
import unittest
from examples import pm

ARGS = ['newcard', '--recipe', 'runner-verify', '--title', '验证', '--serves', '目标', '--plan', 'P0 14 T2', '--approval', '已授权', '--scope', 'examples/pm.py', '--goal', '验证', '--real-data', '真实卡', '--oracle', '一致', '--accept-by', 'real-run', '--question', '验证', '--dry-run']


def run_card(authorization):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = pm.main(ARGS + ['--authorize', authorization])
    return result, out.getvalue()


class AuthorizationTests(unittest.TestCase):
    def test_reads_and_denials(self):
        for text in ('允许读取所有数据源', '只读取行情接口和数据库', '禁止写 TD', '不触发事件', '禁止删除 Redis 键'):
            with self.subTest(text=text):
                code, output = run_card(text)
                self.assertEqual(code, 0)
                self.assertIn('不写 Redis/TD', output)
                self.assertNotIn('【PM 明确允许】', output)

    def test_explicit_targets_and_mixed_read(self):
        for text, target in (('允许写 Redis 目标 core:cache:data:pool', 'core:cache:data:pool'),
                             ('允许写 Redis 目标 custom:pool', 'custom:pool'),
                             ('允许写 TD 目标 core_data.pool', 'core_data.pool'),
                             ('只读行情接口，允许写 Redis 目标 core:cache:data:pool', 'core:cache:data:pool')):
            with self.subTest(text=text):
                code, output = run_card(text)
                self.assertEqual(code, 0)
                self.assertIn('【PM 明确允许】', output)
                self.assertIn(target, output)
                self.assertIn('不触发事件', output)

    def test_unsafe_or_ambiguous(self):
        for text in ('允许写 Redis', '允许写 Redis 目标 q2:abc', '允许写 TD 目标 market_data1.table',
                     '只读 Redis，允许写 TD', '允许触发事件', '可能写入 Redis 目标 core:cache:data:pool',
                     '禁止写 Redis 但允许写 Redis 目标 core:cache:data:pool',
                     '禁止写 Redis，允许写 Redis 目标 core:cache:data:pool',
                     '只读 Redis，允许写 Redis 目标 core:cache:data:pool',
                     '允许写 Redis 目标 core:cache:data:pool 并删除键'):
            with self.subTest(text=text):
                code, output = run_card(text)
                self.assertEqual(code, 2)
                self.assertIn('REFUSED', output)


if __name__ == '__main__':
    unittest.main()
