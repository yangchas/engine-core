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


def _positions(positions, names=None):
    names = names or {}
    if not positions:
        return '无'
    return '; '.join(f"{p['symbol']} {names.get(p['symbol'], '名称未知')} 数量={p['qty']:.2f} 成本={p['cost_basis']:.2f}" for p in positions)


def _table(headers, rows):
    th = ''.join(f'<th style="padding:10px 8px;text-align:left;border-bottom:1px solid #e5e7eb;white-space:nowrap;color:#6b7280">{_esc(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(f'<td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top">{cell}</td>' for cell in row) + '</tr>' for row in rows)
    return '<div style="overflow-x:auto"><table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>' + th + '</tr></thead><tbody>' + body + '</tbody></table></div>'


def _card(title, content):
    return f'<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin:14px 0"><h2 style="font-size:17px;margin:0 0 14px;color:#111827">{_esc(title)}</h2>{content}</div>'


def _page(content):
    return '<!doctype html><html><body style="margin:0;background:#f5f7fa;color:#374151;font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;line-height:1.6"><div style="max-width:740px;margin:0 auto;padding:20px 12px">' + content + '</div></body></html>'


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
    labels = ('板块池', '主板/创业板非ST', '剔2板+/炸板', '可买', '护栏(承接≥0.7/额≥3000万/不追高)', '精选 Top5')
    funnel_text = ' → '.join(f'{label} {funnel.get(key, "未知")}' for label, key in zip(labels, keys)) + f" (创业板 {funnel.get('gem_count', '未知')}只)"
    badge = {'WARM_SOLO': ('偏暖独秀·满仓', '#d93025'), 'COLD_SOLO': ('偏冷独秀·半仓', '#b45309'), 'CROWDED': ('混战打架·空仓', '#6b7280'), 'RED': ('偏冷防御·空仓', '#188038')}.get(regime['label'], (regime['label'], '#6b7280'))
    gap, temp, top = metrics.get('gap'), metrics.get('temperature'), metrics.get('top100_avg_pct')
    top_cmp = '未知 vs -1.0%' if not isinstance(top, (int, float)) else f"{top:.2f}% {'>' if top > -1 else '≤'} -1.0%"
    explain = f"独秀差 {_comparison(gap, .60, 'pp')}；情绪温度 {_comparison(temp, 45, '°', 0)}；昨日 TOP100 {top_cmp}。"
    if regime.get('reasons'):
        explain += ' 原因：' + ', '.join(regime['reasons'])
    header = f'<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:22px"><h1 style="font-size:22px;margin:0 0 10px">影子策略早盘报告 · {_esc(trade_date)} 09:46</h1><span style="background:{badge[1]};color:#fff;border-radius:20px;padding:5px 10px;font-size:13px">{_esc(regime["label"] + " " + badge[0])}</span><p style="margin:14px 0 0">{_esc(explain)}</p></div>'
    values = [('09:45 情绪温度', _num(temp, '°')), ('昨日 TOP100', _num(top, '%', True)), ('因子10 独秀差 gap', _num(gap, 'pp')), ('昨日 pos20', _num(raw.get('pos20_med'))), ('目标总仓位', _num(regime['target_exposure']*100 if regime.get('target_exposure') is not None else None, '%')), ('单票仓位上限', _num(regime['max_single_weight']*100 if regime.get('max_single_weight') is not None else None, '%'))]
    grid = '<table role="presentation" style="width:100%;table-layout:fixed">' + ''.join('<tr>' + ''.join(f'<td style="padding:8px;vertical-align:top;width:33%"><div style="font-size:12px;color:#6b7280">{_esc(k)}</div><strong>{_esc(v)}</strong></td>' for k, v in values[i:i+3]) + '</tr>' for i in (0, 3)) + '</table>'
    details = selection.get('theme_details') or [{'theme': t, 'p45': None} for t in selection['themes']]
    pills = ' '.join(f'<span style="display:inline-block;background:#f5f7fa;border:1px solid #e5e7eb;border-radius:16px;padding:4px 10px;margin:3px">{_esc(t["theme"])} <strong style="color:{_color(t.get("p45"))}">{_esc(_num(t.get("p45"), "%", True))}</strong></span>' for t in details) or '无'
    funnel_html = '<p style="line-height:2">' + ' → '.join(f'<span style="display:inline-block;background:#f5f7fa;border-radius:12px;padding:3px 8px">{_esc(label)} {_esc(funnel.get(key, "未知"))}</span>' for label, key in zip(labels, keys)) + f'（创业板 {_esc(funnel.get("gem_count", "未知"))}只）</p>'
    rows = []
    for e in selection.get('evidence', []):
        rows.append([_esc(e['rank']), _esc(e['symbol'] + ' ' + e['name']), _esc(e['theme'] + ' / ' + e['board'] + f" {e['limit_pct']:.0f}%"), _esc(_num(e['ret_25'], '%', True) + ' → ' + _num(e['ret_45'], '%', True) + ' (' + _num(e['path_25_45'], 'pp', True) + ')'), _esc(_num(e['norm_accel'], signed=True, digits=3)), _esc(_num(e['intraday_pos_45'])), _esc(_num(e['amt_45_yi'], '亿')), _esc(_num(e['ask1']) + ' / 量 ' + str(e['av1'])), _esc(f"连板 {e['prev_streak']} / 炸板 {e['prev_broken']}")])
    callout = '<div style="background:#fef2f2;color:#b91c1c;padding:12px;border-radius:8px;margin-bottom:12px">今日环境未满足出手条件，坚决空仓观望</div><strong>今日候补观察（未买入）</strong>' if regime['target_exposure'] == 0 else ''
    evidence_table = _table(['#', '标的(代码+名称)', '主线/板别', '09:25→09:45 (加速)', '归一化分', '早盘承接', '09:45成交额', '卖一入场价', '昨日基因'], rows) if rows else '<p>暂无通过护栏的候选</p>'
    compare = []
    lines = [f'影子策略 {trade_date}', '大盘环境与因子 10：', f"temp45={temp} top100={top} gap={gap} pos20={raw.get('pos20_med')} regime={regime['label']} target_exposure={regime['target_exposure']}", explain, 'B1 Top3 主线板块：' + ', '.join(str(t['theme']) + ' ' + _num(t.get('p45'), '%', True) for t in details), '选股漏斗：' + funnel_text, '漏斗明细：' + ' → '.join(f'{key}={funnel.get(key, "未知")}' for key in keys)]
    if callout: lines.append('今日环境未满足出手条件，坚决空仓观望；今日候补观察（未买入）')
    for e in selection.get('evidence', []):
        lines.append('买入证据：' + ' | '.join(f'{k}={v}' for k, v in e.items()))
    for name in NAMES:
        record, execution = records[name], records[name]['execution']
        names = record.get('stock_names', {})
        positions = _positions(record.get('positions', []), names)
        trade_lines = [f"{t['symbol']} {names.get(t['symbol'], '名称未知')} {t['side']} 价格={t['price']:.2f} 数量={t['qty']:.2f} 手续费={t['fee']:.2f}" for t in execution['trades']]
        lines.append(f"\n{name}：现金={execution['cash']:.2f} 持仓={positions}")
        lines.extend(trade_lines or ['无买卖'])
        compare.append([_esc(name), _esc(record['regime']['label']), '<br>'.join(_esc(t) for t in trade_lines) or '无买卖', _esc(_num(execution['cash'])), _esc(positions)])
    sources = main.get('source_metadata') or {}
    provenance = '；'.join(f'{k}={sources.get(k, "未知")}' for k in ('membership', 'kaipan', 'stock_names'))
    lines.append('\n数据来源：' + provenance + '；只读影子策略，不构成交易指令。')
    markup = _page(header + _card('大盘环境与触发证据', grid) + _card('主线板块（B1 Top3）与选股漏斗', pills + funnel_html) + _card('核心买入证据明细（逐只拆解）', callout + evidence_table) + _card('3 条影子策略赛马对比', _table(['策略', '环境', '今日成交明细(代码+名称/方向/价格/股数/手续费)', '剩余现金', '当前持仓'], compare)) + '<p style="color:#6b7280;font-size:12px">数据来源：' + _esc(provenance) + '<br>只读影子策略，不构成交易指令。</p>')
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
        rows.append([_esc(name), _esc(_num(mark['nav'], digits=4)), f'<span style="color:{_color(ret)}">{_esc(_num(ret, "%", True))}</span>', _esc(_num(mark['max_drawdown']*100, '%')), _esc(_num(mark['cash'])), _esc(_num(mark['equity']-mark['cash'])), _esc(positions)])
    markup = _page(_card('影子策略收盘盯市 · ' + trade_date, _table(['策略', 'NAV', '累计收益%', '最大回撤 MDD%', '现金', '持仓市值', '当前持仓明细含名称'], rows)) + '<p style="color:#6b7280;font-size:12px">只读影子策略，不构成交易指令。</p>')
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
