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


def build_c6_preopen_email(trade_date, payload, *, recipients, subject_prefix='[C6 09:26 冰点实盘信号]'):
    picks = payload['candidates']
    subject = f"{subject_prefix} {trade_date} 温度={payload['prev_eod_temp']:.1f}° | 选出{len(picks)}只"
    headers = ['序号', '代码', '名称', '20日位置(pos20)', '昨日涨跌幅%', '09:25竞价额(万)', '09:25开盘价', '09:25涨幅%']
    rows = [[str(i), p['symbol'], p.get('name') or '未知', _num(p.get('pos20_prev')),
             _num(p.get('prev_pct_chg'), signed=True), _num(p['amt25_yuan'] / 10000),
             _num(p.get('price25')), _num(p.get('p25'), signed=True)] for i, p in enumerate(picks, 1)]
    instruction = '09:30:05 卖一挂单等权买入（50%资金袖），次日 09:45 卖一退出；若一字涨停买不到直接放弃、不替补'
    lines = [subject, ' | '.join(headers), *(' | '.join(row) for row in rows), instruction]
    markup = _page(f'<h2>{_esc(subject)}</h2>' + _table(headers, [[_esc(cell) for cell in row] for row in rows]) + f'<p>{_esc(instruction)}</p>')
    return _message(subject, lines, recipients, markup)


def _comparison(value, threshold, unit, digits=2):
    if not isinstance(value, (int, float)):
        return f'未知 vs {threshold:.{digits}f}{unit}'
    relation = '≥' if value >= threshold else '<'
    return f'{value:.{digits}f}{unit} {relation} {threshold:.{digits}f}{unit}'


def build_intraday_shadow_email(trade_date, decisions, *, recipients, subject_prefix='[影子策略 09:46]'):
    records = _by_name(decisions)
    main = next(iter(records.values()))
    regime, selection = main['regime'], main['selection']
    metrics, raw = regime['metrics'], main.get('regime_raw', {})
    trades = main['execution']['trades']
    buys = sum(t['side'] == 'BUY' for t in trades)
    sells = sum(t['side'] == 'SELL' for t in trades)
    subject = f"{subject_prefix} {trade_date} {regime['label']} | 买{buys}卖{sells}"
    funnel = selection['funnel_counts']
    keys = ('theme_pool', 'after_universe', 'after_landmine', 'after_buyable', 'after_guardrails', 'selected', 'gem_count')
    labels = ('板块池', '主板/创业板非ST', '剔2板+/炸板', '可买', '护栏', '精选 Top5')
    if 'after_landmine' not in funnel:
        keys = ('theme_pool', 'after_universe', 'after_buyable', 'selected', 'gem_count')
        labels = ('板块池', '主板/创业板非ST', '可买', '精选 Top5')
    funnel_text = ' → '.join(f'{label} {funnel.get(key, "未知")}' for label, key in zip(labels, keys)) + f" (创业板 {funnel.get('gem_count', '未知')}只)"
    badge = {'WARM_SOLO': ('偏暖独秀·满仓', '#d93025'), 'COLD_SOLO': ('偏冷独秀·半仓', '#b45309'), 'CROWDED': ('混战打架·空仓', '#6b7280'), 'RED': ('偏冷防御·空仓', '#188038'), 'BEAR_ICE': ('冰点策略', '#2563eb'), 'AUCTION_ON': ('竞价策略', '#b45309')}.get(regime['label'], (regime['label'], '#6b7280'))
    gap, temp, top = metrics.get('gap'), metrics.get('temperature'), metrics.get('top100_avg_pct')
    top_cmp = '未知 vs -1.0%' if not isinstance(top, (int, float)) else f"{top:.2f}% {'>' if top > -1 else '≤'} -1.0%"
    explain = f"独秀差 {_comparison(gap, .60, 'pp')}；情绪温度 {_comparison(temp, 45, '°', 0)}；昨日 TOP100 {top_cmp}。"
    if regime['label'] in ('BULL', 'BULL_EMERGENCY_EXIT', 'BEAR_CASH', 'BEAR_GREEN', 'BASELINE_ON', 'BEAR_ICE', 'AUCTION_ON'):
        explain = f"宏观状态 {raw.get('macro_state', '未知')}（截至 {raw.get('macro_as_of', '未知')}）；温度 {_num(temp, '°')}；TOP100 {_num(top, '%', True)}。"
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
    for name in records:
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


def _trace_summary(record):
    regime = record.get('regime', {})
    metrics = regime.get('metrics', {})
    reasons = ', '.join(regime.get('reasons') or ()) or '条件满足'
    exp = regime.get('target_exposure', 0.0) * 100.0
    themes = ', '.join(record.get('selection', {}).get('themes') or ()) or '无'
    names = record.get('stock_names', {})
    trades = record.get('execution', {}).get('trades', [])
    trade_str = '；'.join(f"{t['side']} {t['symbol']}{names.get(t['symbol'], '')}@{t['price']:.2f}({_wan(t['qty']*t['price'])})" for t in trades) or '无买卖'
    parts = [f"判定={regime.get('label', '未知')}", f"目标仓位={exp:.0f}%"]
    if metrics.get('macro_state'):
        parts.append(f"宏观={metrics['macro_state']}")
    if isinstance(metrics.get('temperature'), (int, float)):
        parts.append(f"09:45温度={metrics['temperature']:.1f}°")
    parts.append(f"理由={reasons}")
    return {'label': regime.get('label', '未知'), 'exp': f'{exp:.0f}%', 'chain': ' → '.join(parts), 'themes': themes, 'trades': trade_str}


def build_daily_summary_payload(trade_date, intraday_decisions=None, eod_marks=None, macro_record=None):
    decisions = _by_name(intraday_decisions or {})
    marks = _by_name(eod_marks or {})
    names = list(dict.fromkeys([*decisions, *marks]))
    macro_keys = ('macro_state', 'mkt_ew_ret', 'mkt_ew_idx', 'mkt_ma20', 'above_ma20_pct', 'n_stocks')
    macro = {key: macro_record.get(key) for key in macro_keys} if macro_record else None
    strategies = []
    for name in names:
        rec = decisions.get(name, {})
        regime = rec.get('regime', {})
        exposure = regime.get('target_exposure')
        pct = round(exposure * 100) if isinstance(exposure, (int, float)) else None
        label = regime.get('label', '未知')
        reasons = regime.get('reasons') or []
        if pct == 0:
            chain = f"{reasons[0] if reasons else label} → 空仓观望"
        elif pct is None:
            chain = '盘中决策未知'
        else:
            metrics = regime.get('metrics') or {}
            if label == 'BASELINE_ON':
                prefix = '无门控基线'
            elif label == 'BEAR_ICE':
                temp = metrics.get('prev_eod_temp')
                prefix = 'BEAR_ICE' + (f'（前日 {temp:.1f}°）' if isinstance(temp, (int, float)) else '')
            elif label == 'AUCTION_ON':
                prefix = 'AUCTION_ON 竞价信号'
            elif label == 'BEAR_GREEN':
                temp = metrics.get('temperature')
                prefix = 'BEAR + 09:45 GREEN' + (f'({temp:.1f}°)' if isinstance(temp, (int, float)) else '')
            else:
                prefix = reasons[0] if reasons else label
            selection = rec.get('selection') or {}
            themes = '/'.join(str(t) for t in (selection.get('themes') or [])[:3]) or ('竞价候选' if label in ('BEAR_ICE', 'AUCTION_ON') and name.startswith(('c6_', 'v1_')) else '板块未知')
            picks = selection.get('picks')
            count = f'({len(picks)}股)' if isinstance(picks, (list, tuple)) else ''
            chain = f'{prefix} → {themes}{count}'
        import re
        chain = re.sub(r'(?<!\d)\d{6}(?!\d)', '', chain)
        nav = marks.get(name, {}).get('nav')
        nav = float(nav) if isinstance(nav, (int, float)) else None
        strategies.append({'name': name, 'exposure_pct': pct, 'derivation': chain,
                           'nav': nav, 'ret_pct': (nav - 1) * 100 if nav is not None else None})
    return {'trade_date': str(trade_date), 'macro': macro, 'strategies': strategies}


def _summary_lines(payload):
    lines = []
    macro = payload['macro']
    if macro:
        ret = macro.get('mkt_ew_ret')
        ret = ret * 100 if isinstance(ret, (int, float)) else None
        lines.append(f"明日宏观 B1：{macro.get('macro_state') or '未知'}（全市场 {_num(ret, '%', True)}｜指数 {_num(macro.get('mkt_ew_idx'), digits=4)} vs MA20 {_num(macro.get('mkt_ma20'), digits=4)}｜站上MA20 {_num(macro.get('above_ma20_pct'), '%')}）")
    for i, item in enumerate(payload['strategies']):
        branch = '└──' if i == len(payload['strategies']) - 1 else '├──'
        exposure = f"{item['exposure_pct']}%" if item['exposure_pct'] is not None else '未知'
        nav = _num(item['nav'], digits=4)
        ret = _num(item['ret_pct'], '%', True)
        lines.append(f"{branch} {item['name']} ｜ 仓位 {exposure} ｜ 推导：{item['derivation']} ｜ NAV {nav} ({ret})")
    return lines


def render_daily_summary_md(trade_date, intraday_decisions=None, eod_marks=None, macro_record=None):
    payload = build_daily_summary_payload(trade_date, intraday_decisions, eod_marks, macro_record)
    return '\n'.join([f'# {trade_date} 多策略推导线与仓位', '', *_summary_lines(payload), ''])


def _legacy_eod_shadow_email(trade_date, marks, *, recipients, subject_prefix='[影子策略 18:10复盘]', intraday_decisions=None, macro_record=None):
    records = _by_name(marks)
    first = next(iter(records))
    nav = records[first]['nav']
    macro_tag = f" | 明日宏观:{macro_record['macro_state']}" if macro_record and macro_record.get('macro_state') else ''
    subject = f'{subject_prefix} {trade_date}{macro_tag} | {first} NAV={nav:.4f} ({(nav-1)*100:+.2f}%)'
    lines, rows = [f'影子策略复盘 {trade_date}'], []
    sections = [f'<div style="font-size:15px;font-weight:700;color:#111827;padding-bottom:6px">影子策略每日复盘汇总 · {_esc(trade_date)}</div>']
    if macro_record:
        m_state = macro_record.get('macro_state', '未知')
        m_ret = macro_record.get('mkt_ew_ret') * 100 if isinstance(macro_record.get('mkt_ew_ret'), (int, float)) else None
        m_line = f"明日宏观状态={m_state} | 全市场等权={_num(m_ret, '%', True)} | 指数={_num(macro_record.get('mkt_ew_idx'), digits=4)} vs MA20={_num(macro_record.get('mkt_ma20'), digits=4)} | 站上MA20={_num(macro_record.get('above_ma20_pct'), '%')}"
        lines.append(m_line)
        m_color = '#d93025' if m_state == 'BULL' else '#188038'
        sections.append(_section('1. 明日宏观牛熊底座（截至今日收盘）', f'<div style="background:#f9fafb;border:1px solid #eef2f7;border-radius:6px;padding:6px 8px">状态 <strong style="color:{m_color}">{_esc(m_state)}</strong> ｜ 全市场等权 <strong style="color:{_color(m_ret)}">{_esc(_num(m_ret, "%", True))}</strong> ｜ 指数 <strong>{_esc(_num(macro_record.get("mkt_ew_idx"), digits=4))}</strong> vs MA20 <strong>{_esc(_num(macro_record.get("mkt_ma20"), digits=4))}</strong> ｜ 站上 MA20 <strong>{_esc(_num(macro_record.get("above_ma20_pct"), "%"))}</strong></div>'))
    for name in records:
        mark = records[name]
        positions = _positions(mark.get('positions', []), mark.get('stock_names', {}))
        ret = (mark['nav']-1)*100
        lines.append(f"\n{name}: NAV={mark['nav']:.4f} 累计收益%={ret:+.2f} MDD%={mark['max_drawdown']*100:.2f} 现金={mark['cash']:.2f} 持仓市值={mark['equity']-mark['cash']:.2f}")
        lines.append('持仓：' + positions)
        rows.append([f"<strong>{_esc(name)}</strong>", _esc(_num(mark['nav'], digits=4)), f'<strong style="color:{_color(ret)}">{_esc(_num(ret, "%", True))}</strong>', _esc(_num(mark['max_drawdown']*100, '%')), _esc(_wan(mark['cash'])), _esc(_wan(mark['equity']-mark['cash'])), _esc(positions)])
    sections.append(_section('2. 多策略收盘盯市与净值' if macro_record else '收盘盯市与净值', _table(['策略', 'NAV', '累计收益%', '最大回撤 MDD%', '现金', '持仓市值', '当前持仓'], rows)))
    if intraday_decisions:
        trace_rows = []
        lines.append('\n今日多策略触发信号与理由链路：')
        for name, rec in _by_name(intraday_decisions).items():
            t = _trace_summary(rec)
            lines.append(f"- {name}: {t['chain']} | 板块={t['themes']} | 买卖={t['trades']}")
            trace_rows.append([f"<strong>{_esc(name)}</strong>", _esc(t['label']), _esc(t['exp']), _esc(t['chain']), _esc(t['themes']), _esc(t['trades'])])
        sections.append(_section('3. 今日触发信号与理由路径复盘', _table(['策略', '判定', '目标仓位', '触发理由链路', '选中板块', '今日买卖'], trace_rows)))
    sections.append('<div style="color:#9ca3af;font-size:11px;padding-top:6px">只读影子验证，不构成交易指令</div>')
    return _message(subject, lines, recipients, _page(''.join(sections)))


def build_eod_shadow_email(trade_date, marks, *, recipients, subject_prefix='[影子策略 18:10复盘]', intraday_decisions=None, macro_record=None):
    # Preserve the established mark-only email contract for existing callers.
    if not intraday_decisions:
        return _legacy_eod_shadow_email(trade_date, marks, recipients=recipients, subject_prefix=subject_prefix, macro_record=macro_record)
    payload = build_daily_summary_payload(trade_date, intraday_decisions, marks, macro_record)
    lines = _summary_lines(payload)
    first = payload['strategies'][0]
    tag = f" | 明日宏观:{macro_record['macro_state']}" if macro_record and macro_record.get('macro_state') else ''
    subject = f"{subject_prefix} {trade_date}{tag} | {first['name']} NAV={_num(first['nav'], digits=4)} ({_num(first['ret_pct'], '%', True)})"
    markup = _page('<h2>多策略推导线与仓位</h2>' + ''.join(f'<div>{_esc(line)}</div>' for line in lines))
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
