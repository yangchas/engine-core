"""Authorization contract checks with only in-memory dry runs."""
import contextlib
import io
import unittest
from examples import pm

ARGS = ['newcard', '--recipe', 'runner-verify', '--title', '验证', '--serves', '目标', '--plan', 'P0 14 T2', '--approval', '已授权', '--scope', 'examples/pm.py', '--goal', '验证', '--real-data', '真实卡', '--oracle', '一致', '--accept-by', 'real-run', '--question', '验证', '--dry-run']


def card(text):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = pm.main(ARGS + ['--authorize', text])
    return code, out.getvalue()


class AuthorizationContract(unittest.TestCase):
    def test_read_descriptions(self):
        for text in ('只读查看发布日志', '只读查看写入路径和配置', '读取 Redis 写入路径', '只读查看可能更新的发布日志'):
            with self.subTest(text=text):
                code, output = card(text)
                self.assertEqual(code, 0)
                self.assertNotIn('【PM 明确允许】', output)
                self.assertIn('不写 Redis/TD', output)

    def test_conflicting_restrictions(self):
        for text in ('所有数据源只读，允许写 Redis 目标 core:cache:data:pool',
                     '禁止写 Redis，允许写 Redis 目标 custom:pool',
                     '禁止写 Redis 目标 custom:one，允许写 Redis 目标 custom:one',
                     '不允许写 Redis 目标 custom:pool，允许写 Redis 目标 custom:pool'):
            with self.subTest(text=text):
                self.assertEqual(card(text)[0], 2)

    def test_mixed_targets(self):
        for text in ('只读 Redis，允许写 TD 目标 core_data.pool',
                     '只读查看 Redis 写入路径，允许写 Redis 目标 custom:two',
                     '禁止写 Redis 目标 custom:one，允许写 Redis 目标 custom:two',
                     '允许写 Redis 目标 custom:pool，允许写 TD 目标 core_data.pool'):
            with self.subTest(text=text):
                code, output = card(text)
                self.assertEqual(code, 0)
                self.assertIn('【PM 明确允许】', output)
                self.assertIn('既有有效用户授权或当次明确授权', output)

    def test_protection_and_ambiguity(self):
        for text in ('允许写 Redis 目标 legacy:pool', '允许写 TD 目标 market_data1.pool',
                     '允许写 Redis', '允许触发事件', '可能写入 Redis 目标 custom:pool'):
            with self.subTest(text=text):
                self.assertEqual(card(text)[0], 2)


if __name__ == '__main__':
    result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(AuthorizationContract))
    print('tests=%d failures=%d errors=%d' % (result.testsRun, len(result.failures), len(result.errors)))
    raise SystemExit(not result.wasSuccessful())
