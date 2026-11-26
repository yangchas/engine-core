"""Shadow strategy mail rendering and isolated delivery state."""
import hashlib
import html
import os
import sqlite3
import time
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

from engine_core import report_mailer

RECIPIENT = '412826313@qq.com'
NAMES = ('main_a_p2', 'main_a_p2_lu_hold', 'baseline_pos20')


def _message(subject, lines, recipients):
    addresses = list(recipients)
    if not addresses or any(a.strip().lower() != RECIPIENT for a in addresses):
        raise ValueError('shadow email recipient policy rejected message')
    msg = EmailMessage()
    msg['To'] = ', '.join(addresses)
    msg['Subject'] = subject
    body = '\n'.join(lines)
    msg.set_content(body)
    msg.add_alternative('<html><body><pre>' + html.escape(body) + '</pre></body></html>', subtype='html')
    msg['X-Core-Report-SHA256'] = hashlib.sha256((subject + '\n' + body).encode()).hexdigest()
    return msg


def _by_name(records):
    if isinstance(records, dict):
        return records
    return {record['strategy']: record for record in records}


def _positions(positions):
    if not positions:
        return '无'
    return '; '.join(f"{p['symbol']} 数量={p['qty']:.2f} 成本={p['cost_basis']:.2f}" for p in positions)


def build_intraday_shadow_email(trade_date, decisions, *, recipients, subject_prefix='[影子策略 09:46]'):
    records = _by_name(decisions)
    main = records['main_a_p2']
    regime = main['regime']
    metrics = regime['metrics']
    trades = main['execution']['trades']
    buys = sum(t['side'] == 'BUY' for t in trades)
    sells = sum(t['side'] == 'SELL' for t in trades)
    subject = f"{subject_prefix} {trade_date} {regime['label']} | 买{buys}卖{sells}"
    raw = main.get('regime_raw', {})
    selection = main['selection']
    funnel = selection['funnel_counts']
    keys = ('theme_pool', 'after_universe', 'after_landmine', 'after_buyable', 'after_guardrails', 'selected', 'gem_count')
    lines = [f'影子策略 {trade_date}', '大盘环境与因子 10：',
             f"temp45={metrics.get('temperature')} top100={metrics.get('top100_avg_pct')} gap={metrics.get('gap')} pos20={raw.get('pos20_med')} regime={regime['label']} target_exposure={regime['target_exposure']}",
             'B1 Top3 主线板块：' + ', '.join(selection['themes']),
             '选股漏斗：' + ' → '.join(f'{key}={funnel.get(key, "未知")}' for key in keys)]
    for name in NAMES:
        record = records[name]
        execution = record['execution']
        lines.append(f"\n{name}：现金={execution['cash']:.2f} 持仓={_positions(record.get('positions', []))}")
        if not execution['trades']:
            lines.append('无买卖')
        for trade in execution['trades']:
            lines.append(f"{trade['symbol']} {trade['side']} 价格={trade['price']:.2f} 数量={trade['qty']:.2f} 手续费={trade['fee']:.2f}")
    sources = main.get('source_metadata') or {}
    lines.append('\n数据来源：' + '；'.join(f'{k}={sources.get(k, "未知")}' for k in ('membership', 'kaipan', 'stock_names')))
    return _message(subject, lines, recipients)


def build_eod_shadow_email(trade_date, marks, *, recipients, subject_prefix='[影子策略 15:25盯市]'):
    records = _by_name(marks)
    nav = records['main_a_p2']['nav']
    subject = f'{subject_prefix} {trade_date} | main_a_p2 NAV={nav:.4f} ({(nav-1)*100:+.2f}%)'
    lines = [f'影子策略盯市 {trade_date}']
    for name in NAMES:
        mark = records[name]
        lines.append(f"\n{name}: NAV={mark['nav']:.4f} 累计收益%={(mark['nav']-1)*100:+.2f} MDD%={mark['max_drawdown']*100:.2f} 现金={mark['cash']:.2f} 持仓市值={mark['equity']-mark['cash']:.2f}")
        lines.append('持仓：' + _positions(mark.get('positions', [])))
    return _message(subject, lines, recipients)


class _LocalMailState:
    """Atomic, expiring mail claims without accessing Redis."""
    def __init__(self):
        root = Path(os.environ['CORE_DATA_ROOT']) / 'shadow_mail'
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'delivery.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY, value TEXT, expires REAL)')

    def get(self, key):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT value FROM claims WHERE key=? AND expires>?', (key, time.time())).fetchone()
            return row[0] if row else None

    def claim(self, key, value, ttl_seconds):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM claims WHERE key=? AND expires<=?', (key, time.time()))
            inserted = db.execute('INSERT OR IGNORE INTO claims VALUES (?, ?, ?)', (key, value, time.time()+ttl_seconds)).rowcount == 1
            if not inserted and db.execute('SELECT value FROM claims WHERE key=?', (key,)).fetchone()[0] != 'sent':
                raise RuntimeError('email delivery already in progress; retry later')
            return inserted

    def set(self, key, value, ttl_seconds):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO claims VALUES (?, ?, ?)', (key, value, time.time()+ttl_seconds))

    def delete(self, key):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM claims WHERE key=?', (key,))


def send_shadow_email(msg, *, smtp_config_path=None, dry_run=False):
    headers = [str(msg[k]) for k in ('To', 'Cc', 'Bcc') if msg.get(k)]
    recipients = [addr.lower() for _, addr in getaddresses(headers)]
    if not recipients or any(addr != RECIPIENT for addr in recipients) or msg.get('Cc') or msg.get('Bcc'):
        raise ValueError('shadow email recipient policy rejected message')
    cfg = report_mailer.load_smtp_config(smtp_config_path)
    msg['From'] = cfg.from_addr
    receipt = report_mailer.send_message(cfg, msg, dry_run=dry_run, state=_LocalMailState())
    if not dry_run and receipt.status not in ('sent', 'skipped'):
        raise RuntimeError(f'email delivery incomplete: {receipt.status}')
    return receipt
