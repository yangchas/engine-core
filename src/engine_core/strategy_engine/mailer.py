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


def _message(subject, lines, recipients, markup):
    addresses = list(recipients)
    if not addresses or any(a.strip().lower() != RECIPIENT for a in addresses):
        raise ValueError('shadow email recipient policy rejected message')
    msg = EmailMessage()
    msg['To'] = ', '.join(addresses)
    msg['Subject'] = subject
    body = '\n'.join(lines)
    msg.set_content(body)
    msg.add_alternative(markup, subtype='html')
    msg['X-Core-Report-SHA256'] = hashlib.sha256((subject + '\n' + body).encode()).hexdigest()
    return msg


def _by_name(records):
    if isinstance(records, dict):
        return records
    return {record['strategy']: record for record in records}


def _esc(value):
    return html.escape(str(value), quote=True)


def _num(value, suffix='', signed=False, digits=2):
    if not isinstance(value, (int, float)):
        return '未知'
    return f"{value:+.{digits}f}{suffix}" if signed else f"{value:.{digits}f}{suffix}"


def _color(value):
    return '#d93025' if isinstance(value, (int, float)) and value > 0 else '#188038' if isinstance(value, (int, float)) and value < 0 else '#374151'


def _wan(value):
    if not isinstance(value, (int, float)):
        return '未知'
    return f'{value/10000:.2f}万'


def _positions(positions, names=None):
    names = names or {}
    if not positions:
        return '空仓'
    return '；'.join(f"{p['symbol']}{names.get(p['symbol'], '')}({p['qty']*p['cost_basis']/10000:.1f}万@{p['cost_basis']:.2f})" for p in positions)


def _table(headers, rows):
    th = ''.join(f'<th style="padding:5px 6px;text-align:left;border-bottom:1px solid #d1d5db;white-space:nowrap;color:#4b5563;background:#f9fafb;font-weight:600">{_esc(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(f'<td style="padding:5px 6px;border-bottom:1px solid #eef2f7;vertical-align:middle;white-space:nowrap">{cell}</td>' for cell in row) + '</tr>' for row in rows)
    return '<div style="overflow-x:auto"><table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:12px;line-height:1.35"><thead><tr>' + th + '</tr></thead><tbody>' + body + '</tbody></table></div>'


def _section(title, content):
    return f'<div style="padding:8px 0;border-top:1px solid #e5e7eb"><div style="font-size:13px;font-weight:700;color:#111827;margin:0 0 5px">{_esc(title)}</div>{content}</div>'


def _page(content):
    return '<!doctype html><html><body style="margin:0;background:#f3f4f6;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;font-size:12px;line-height:1.45"><div style="max-width:760px;margin:0 auto;padding:8px"><div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px">' + content + '</div></div></body></html>'


def _comparison(value, threshold, unit, digits=2):
    if not isinstance(value, (int, float)):
        return f'未知 vs {threshold:.{digits}f}{unit}'
    relation = '≥' if value >= threshold else '<'
    return f'{value:.{digits}f}{unit} {relation} {threshold:.{digits}f}{unit}'


def build_intraday_shadow_email(trade_date, decisions, *, recipients, subject_prefix='[影子策略 09:46]'):
    records = _by_name(decisions)
    main = records['main_a_p2']
    regime, selection = main['regime'], main['selection']
    metrics, raw = regime['metrics'], main.get('regime_raw', {})
    trades = main['execution']['trades']
    buys = sum(t['side'] == 'BUY' for t in trades)
    sells = sum(t['side'] == 'SELL' for t in trades)
    subject = f"{subject_prefix} {trade_date} {regime['label']} | 买{buys}卖{sells}"
    funnel = selection['funnel_counts']
    keys = ('theme_pool', 'after_universe', 'after_landmine', 'after_buyable', 'after_guardrails', 'selected', 'gem_count')
    labels = ('板块池', '主板/创业板非ST', '剔2板+/炸板', '可买', '护栏', '精选 Top5')
    funnel_text = ' → '.join(f'{label} {funnel.get(key, "未知")}' for label, key in zip(labels, keys)) + f" (创业板 {funnel.get('gem_count', '未知')}只)"
    badge = {'WARM_SOLO': ('偏暖独秀·满仓', '#d93025'), 'COLD_SOLO': ('偏冷独秀·半仓', '#b45309'), 'CROWDED': ('混战打架·空仓', '#6b7280'), 'RED': ('偏冷防御·空仓', '#188038')}.get(regime['label'], (regime['label'], '#6b7280'))
    gap, temp, top = metrics.get('gap'), metrics.get('temperature'), metrics.get('top100_avg_pct')
    top_cmp = '未知 vs -1.0%' if not isinstance(top, (int, float)) else f"{top:.2f}% {'>' if top > -1 else '≤'} -1.0%"
    explain = f"独秀差 {_comparison(gap, .60, 'pp')}；情绪温度 {_comparison(temp, 45, '°', 0)}；昨日 TOP100 {top_cmp}。"
    if regime.get('reasons'):
        explain += ' 原因：' + ', '.join(regime['reasons'])
    exp_pct = regime['target_exposure'] * 100 if regime.get('target_exposure') is not None else None
    max_w_pct = regime['max_single_weight'] * 100 if regime.get('max_single_weight') is not None else None
    header = f'<div style="padding-bottom:8px"><span style="font-size:15px;font-weight:700;color:#111827">影子策略 · {_esc(trade_date)} 09:46</span> <span style="background:{badge[1]};color:#fff;border-radius:10px;padding:2px 8px;font-size:11px;margin-left:6px">{_esc(regime["label"] + " " + badge[0])}</span> <span style="color:#6b7280;margin-left:6px">买{buys} / 卖{sells}</span></div>'
    env_html = f'<div style="background:#f9fafb;border:1px solid #eef2f7;border-radius:6px;padding:6px 8px">温度 <strong>{_esc(_num(temp, "°"))}</strong> ｜ TOP100 <strong style="color:{_color(top)}">{_esc(_num(top, "%", True))}</strong> ｜ 独秀差gap <strong>{_esc(_num(gap, "pp"))}</strong> ｜ pos20 <strong>{_esc(_num(raw.get("pos20_med")))}</strong> ｜ 目标仓位 <strong>{_esc(_num(exp_pct, "%", digits=0))}</strong>（单票上限 {_esc(_num(max_w_pct, "%", digits=0))}）</div>'
    details = selection.get('theme_details') or [{'theme': t, 'p45': None} for t in selection['themes']]
    pills = ' '.join(f'<span style="display:inline-block;background:#f3f4f6;border-radius:4px;padding:2px 6px;margin-right:4px">{_esc(t["theme"])} <strong style="color:{_color(t.get("p45"))}">{_esc(_num(t.get("p45"), "%", True))}</strong></span>' for t in details) or '无'
    funnel_html = f'<div style="color:#4b5563;margin-top:4px;font-size:11px">漏斗：{_esc(funnel_text)}</div>'
    equity_ref = main['execution'].get('equity_before_buys') or main['execution'].get('cash') or 1_000_000.0
    buy_by_sym = {t['symbol']: t for t in trades if t['side'] == 'BUY'}
    rows = []
    for e in selection.get('evidence', []):
        bt = buy_by_sym.get(e['symbol'])
        if bt:
            amt = bt['qty'] * bt['price']
            pos_cell = f"<strong style='color:#d93025'>{amt/equity_ref*100:.1f}% / {_wan(amt)}</strong>"
        else:
            pos_cell = '<span style="color:#9ca3af">0% / 未买</span>'
        rows.append([
            _esc(e['rank']),
            f"<strong>{_esc(e['symbol'])}</strong> {_esc(e['name'])}",
            _esc(e['theme'] + '·' + e['board']),
            f"{_esc(_num(e['ret_25'], '%', True))}→<strong style='color:{_color(e['ret_45'])}'>{_esc(_num(e['ret_45'], '%', True))}</strong> ({_esc(_num(e['path_25_45'], 'pp', True))})",
            _esc(_num(e['norm_accel'], signed=True, digits=3)),
            _esc(_num(e['intraday_pos_45'])),
            _esc(_num(e['amt_45_yi'], '亿')),
            _esc(_num(e['ask1'])),
            pos_cell,
            _esc(f"{e['prev_streak']}板/炸{e['prev_broken']}"),
        ])
    trigger_line = f'<div style="margin-bottom:5px;color:#374151">{_esc(explain)}</div>'
    if regime['target_exposure'] == 0:
        trigger_line += '<div style="background:#fef2f2;color:#b91c1c;padding:4px 8px;border-radius:4px;margin-bottom:5px">未触发买点，今日空仓观望 · 今日候补观察（未买入）</div>'
    evidence_table = _table(['#', '标的', '板块/板', '09:25→09:45(加速)', '归一化分', '承接', '09:45成交额', '卖一价', '买入仓位/金额', '昨日基因'], rows) if rows else '<div style="color:#6b7280">暂无通过护栏的候选</div>'
    compare = []
    lines = [f'影子策略 {trade_date}', '大盘环境与因子 10：', f"temp45={temp} top100={top} gap={gap} pos20={raw.get('pos20_med')} regime={regime['label']} target_exposure={regime['target_exposure']}", explain, 'B1 Top3 主线板块：' + ', '.join(str(t['theme']) + ' ' + _num(t.get('p45'), '%', True) for t in details), '选股漏斗：' + funnel_text, '漏斗明细：' + ' → '.join(f'{key}={funnel.get(key, "未知")}' for key in keys)]
    if regime['target_exposure'] == 0:
        lines.append('今日环境未满足出手条件，坚决空仓观望；今日候补观察（未买入）')
    for e in selection.get('evidence', []):
        lines.append('买入证据：' + ' | '.join(f'{k}={v}' for k, v in e.items()))
    for name in NAMES:
        record, execution = records[name], records[name]['execution']
        names = record.get('stock_names', {})
        positions = _positions(record.get('positions', []), names)
        trade_lines = [f"{t['symbol']} {names.get(t['symbol'], '名称未知')} {t['side']} 价格={t['price']:.2f} 数量={t['qty']:.2f} 金额={_wan(t['qty']*t['price'])} 手续费={t['fee']:.2f}" for t in execution['trades']]
        compact_trades = [f"{t['side']} {t['symbol']}{names.get(t['symbol'], '')} @{t['price']:.2f} ({_wan(t['qty']*t['price'])})" for t in execution['trades']]
        lines.append(f"\n{name}：现金={execution['cash']:.2f} 持仓={positions}")
        lines.extend(trade_lines or ['无买卖'])
        compare.append([f"<strong>{_esc(name)}</strong>", _esc(record['regime']['label']), '<br>'.join(_esc(t) for t in compact_trades) or '无', _esc(_wan(execution['cash'])), _esc(positions)])
    sources = main.get('source_metadata') or {}
    provenance = '；'.join(f'{k}={sources.get(k, "未知")}' for k in ('membership', 'kaipan', 'stock_names'))
    lines.append('\n数据来源：' + provenance + '；只读影子策略，不构成交易指令。')
    markup = _page(
        header
        + _section('1. 大盘环境', env_html)
        + _section('2. 板块分析（B1 Top3 + 漏斗）', f'<div>{pills}</div>{funnel_html}')
        + _section('3. 买点触发 · 买入仓位与金额', trigger_line + evidence_table)
        + _section('4. 持仓与 3 策略对照', _table(['策略', '判定', '今日买卖(金额)', '现金', '当前持仓'], compare))
        + f'<div style="color:#9ca3af;font-size:11px;padding-top:6px;border-top:1px solid #eef2f7">来源：{_esc(provenance)} ｜ 只读影子验证，不构成交易指令</div>'
    )
    return _message(subject, lines, recipients, markup)


def build_eod_shadow_email(trade_date, marks, *, recipients, subject_prefix='[影子策略 15:25盯市]'):
    records = _by_name(marks)
    nav = records['main_a_p2']['nav']
    subject = f'{subject_prefix} {trade_date} | main_a_p2 NAV={nav:.4f} ({(nav-1)*100:+.2f}%)'
    lines, rows = [f'影子策略盯市 {trade_date}'], []
    for name in NAMES:
        mark = records[name]
        positions = _positions(mark.get('positions', []), mark.get('stock_names', {}))
        ret = (mark['nav']-1)*100
        lines.append(f"\n{name}: NAV={mark['nav']:.4f} 累计收益%={ret:+.2f} MDD%={mark['max_drawdown']*100:.2f} 现金={mark['cash']:.2f} 持仓市值={mark['equity']-mark['cash']:.2f}")
        lines.append('持仓：' + positions)
        rows.append([f"<strong>{_esc(name)}</strong>", _esc(_num(mark['nav'], digits=4)), f'<strong style="color:{_color(ret)}">{_esc(_num(ret, "%", True))}</strong>', _esc(_num(mark['max_drawdown']*100, '%')), _esc(_wan(mark['cash'])), _esc(_wan(mark['equity']-mark['cash'])), _esc(positions)])
    markup = _page(f'<div style="font-size:15px;font-weight:700;color:#111827;padding-bottom:6px">影子策略收盘盯市 · {_esc(trade_date)}</div>' + _table(['策略', 'NAV', '累计收益%', '最大回撤 MDD%', '现金', '持仓市值', '当前持仓'], rows) + '<div style="color:#9ca3af;font-size:11px;padding-top:6px">只读影子验证，不构成交易指令</div>')
    return _message(subject, lines, recipients, markup)


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
