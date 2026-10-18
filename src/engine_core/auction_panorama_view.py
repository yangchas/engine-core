"""Single page email presentation for the V6 auction panorama product."""
from __future__ import annotations

import html
import re
from typing import Any, Mapping


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{value:,.{digits}f}{suffix}"


def _money(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{value / 1e8:.2f}亿" if abs(value) >= 1e8 else f"{value / 1e4:.2f}万"


def _rank(value: Any) -> str:
	return f"第{value}名" if isinstance(value, int) and not isinstance(value, bool) else "待核实"


def _hot_rank(value: Any) -> str:
	return f"第{value}名" if isinstance(value, int) and not isinstance(value, bool) else "—"


def _stock_text(stock: Mapping[str, Any]) -> str:
    name = stock.get("name") or "名称待核实"
    pct = _fmt(stock.get("pct_change"), 2, "%")
    amount = _money(stock.get("auction_amount_yuan"))
    rank = _rank(stock.get("amount_rank"))
    tags = list(stock.get("extremes") or [])
    top = " · 额Top50" if stock.get("amount_top50") and "额Top50" not in tags else ""
    streak = f" · 昨{stock['yesterday_streak']}板" if stock.get("yesterday_streak") is not None else ""
    promotion = (" · 拟晋级" if stock.get("promotion_status") == "PROVISIONAL" else
                 " · 待核实" if stock.get("promotion_status") == "UNVERIFIED" else
                 " · 未晋级迹象" if stock.get("promotion_status") == "NOT_PROMOTED" else "")
    seal_amount = _money(stock.get("seal_amount_yuan")) if stock.get("seal_amount_yuan") is not None else "待核实"
    seal_rank = _rank(stock.get("seal_rank"))
    is_limit = stock.get("at_official_limit") is True
    seal = (f" · 封单{seal_amount}/{seal_rank}" if stock.get("seal_amount_yuan") is not None
            else (" · 封单金额/名次待核实" if is_limit else ""))
    tags = [tag for tag in tags if tag != "Top1金额占比" or stock.get("top1_amount_share") is not None]
    tags = [f"Top1金额占比{_fmt(stock['top1_amount_share'] * 100, 1, '%')}" if tag == "Top1金额占比" else tag for tag in tags]
    tags = tags[:3]
    suffix = f" · {'/'.join(str(tag) for tag in tags)}" if tags else ""
    cause = stock.get("primary_reason")
    cause_text = f"{cause} · " if cause else ""
    related = stock.get("related_theme")
    related_text = f" · 关联板块：{related}" if related else ""
    membership_text = (" · 主题关联(非当日成员)"
                       if stock.get("membership_status") == "THEME_ASSOCIATION_ONLY" else "")
    return (f"{cause_text}{name}（{pct} · {amount} · 全市额{rank}{top}{streak}{promotion}"
            f"{seal}{suffix}{related_text}{membership_text}）")


def _table_style() -> str:
    return "border-collapse:collapse;width:100%;table-layout:fixed;font-size:12px"


def _th(text: str, width: str = "") -> str:
    return (f'<th style="text-align:left;padding:7px 5px;background:#e9eef5;color:#344054;'
            f'border-bottom:1px solid #d0d5dd;{f"width:{width};" if width else ""}">{_esc(text)}</th>')


def _td(text: str, *, secondary: bool = False) -> str:
    return (f'<td style="vertical-align:top;padding:7px 5px;border-bottom:1px solid #eaecf0;'
            f'word-break:break-word;color:{"#667085" if secondary else "#26364a"}">{text}</td>')


def _pct_cell(value: Any) -> str:
    shown = _fmt(value, 2, "%")
    color = "#c83b3b" if isinstance(value, (int, float)) and value > 0 else (
        "#17804a" if isinstance(value, (int, float)) and value < 0 else "#667085")
    return f'<span style="color:{color};font-weight:700">{_esc(shown)}</span>'


def _rank_cell(value: Any, label: str = "") -> str:
    rank = f"#{value}" if isinstance(value, int) and not isinstance(value, bool) else "—"
    return f"{label}{rank}"


def _digest_stock(stock: Mapping[str, Any]) -> str:
    name = stock.get("name") or stock.get("symbol") or "—"
    amount = _money(stock.get("auction_amount_yuan"))
    amount_html = (f'<span style="color:#98a2b3">{_esc(amount)}</span>'
                   if stock.get("auction_amount_yuan") is None else _esc(amount))
    amount_rank = _rank_cell(stock.get("amount_rank"), "额")
    rank_html = (f'<span style="color:#98a2b3">{_esc(amount_rank)}</span>'
                 if not isinstance(stock.get("amount_rank"), int) or isinstance(stock.get("amount_rank"), bool)
                 else _esc(amount_rank))
    seal = (_money(stock.get("seal_amount_yuan")) if isinstance(stock.get("seal_amount_yuan"), (int, float))
            else "—")
    seal_rank = _rank_cell(stock.get("seal_rank")) if isinstance(stock.get("seal_rank"), int) else "—"
    marks = []
    if stock.get("yesterday_streak") is not None:
        marks.append(f"昨{_esc(stock['yesterday_streak'])}板")
    if stock.get("at_official_limit") is True:
        marks.append("封板")
    suffix = f' <span style="color:#667085">{" · ".join(marks)}</span>' if marks else ""
    return (f'{_esc(name)} {_pct_cell(stock.get("pct_change"))} '
            f'{amount_html} {rank_html} '
            f'<span style="color:#667085">封单{_esc(seal)}·{_esc(seal_rank)}</span>{suffix}')


def render_auction_email_digest(product: Mapping[str, Any], quickbrief: Mapping[str, Any], *,
                                completeness: str = "PARTIAL", degraded_note: str = "") -> tuple[str, str]:
    """Render compact theme comparisons from product-layer statistics."""
    overview = product.get("overview") if isinstance(product.get("overview"), Mapping) else {}
    metrics = quickbrief.get("metrics") if isinstance(quickbrief.get("metrics"), Mapping) else {}
    market = metrics.get("market") if isinstance(metrics.get("market"), Mapping) else {}
    lines = [str(line) for line in quickbrief.get("email_lines", []) if isinstance(line, str)]
    replay = bool((quickbrief.get("rules") or {}).get("replay_banner")) or any("历史回放样本" in line for line in lines)
    date = str(product.get("trade_date") or "日期待核实")
    asof = str(product.get("asof") or "09:26")
    if "T" in asof:
        asof = asof.split("T", 1)[1][:5]
    status = "READY" if completeness == "READY" else "PARTIAL"
    relay_thresholds = overview.get("relay_thresholds_v1") if isinstance(overview.get("relay_thresholds_v1"), Mapping) else {}
    threshold = (f"明显极值：距题材中位≥{_fmt(overview.get('extreme_threshold_pp_v1'), 2, 'pp')}；"
        f"明显分化：P75−P25≥{_fmt(overview.get('dispersion_visible_threshold_pp_v1', 4), 1, 'pp')}；"
        f"接力意愿V1：样本≥{relay_thresholds.get('min_n', '—')}；偏弱=高开率低于市场≥{_fmt(relay_thresholds.get('strong_pp'), 0, 'pp')}或涨幅中位低于市场≥1pp；"
        "偏强=高开率不低于市场且涨幅中位高于市场≥1pp；V1冻结工程阈值，非市场规律，未回测")
    pool_n = overview.get("yesterday_pool_n")
    pool_counts = overview.get("yesterday_pool_open_counts")
    pool_valid = overview.get("yesterday_pool_open_valid_n")
    pool_counts = pool_counts if isinstance(pool_counts, Mapping) else {}
    high_rate = (pool_counts.get("high") / pool_valid * 100
                 if isinstance(pool_valid, int) and pool_valid > 0 and isinstance(pool_counts.get("high"), int) else None)
    def pool_bucket(label: str) -> str:
        value = pool_counts.get(label)
        ratio = value / pool_valid * 100 if isinstance(value, int) and isinstance(pool_valid, int) and pool_valid else None
        return f"{value if value is not None else '—'}({_fmt(ratio, 1, '%') if ratio is not None else '—'})"
    down = overview.get("yesterday_limit_down") if isinstance(overview.get("yesterday_limit_down"), Mapping) else {}
    pool_amount_share = overview.get("yesterday_pool_market_share")
    down_text = (f"昨日跌停 {down.get('n')}只 · 今日高/平/低 {down.get('open_counts', {}).get('high', '—')}/"
                 f"{down.get('open_counts', {}).get('flat', '—')}/{down.get('open_counts', {}).get('low', '—')}"
                 if down.get("status") == "VERIFIED" else "昨日跌停:待核实")
    whole_batch = (f"昨日涨停池 {pool_n if pool_n is not None else '—'}只→今日高/平/低开 "
        f"{pool_bucket('high')}/{pool_bucket('flat')}/{pool_bucket('low')}"
        f"（{_fmt(high_rate, 1, '%') if high_rate is not None else '—'}；较全市场 {_fmt(overview.get('yesterday_pool_high_open_gap_pp'), 1, 'pp')}）；"
        f"竞价额占全市场 {_fmt(pool_amount_share * 100, 1, '%') if isinstance(pool_amount_share, (int, float)) else '—'}；"
        f"涨幅中位 {_fmt(overview.get('yesterday_pool_median_pct'), 2, '%')}；{down_text}")
    extremes = overview.get("market_extremes") if isinstance(overview.get("market_extremes"), Mapping) else {}
    def extreme_text(label: str, item: Any, amount_key: str = "auction_amount_yuan") -> str:
        if not isinstance(item, Mapping):
            return f"{label}待核实"
        amount_rank = _rank_cell(item.get("amount_rank"), "额")
        return (f"{label}{item.get('name') or item.get('symbol') or '—'} "
                f"{_fmt(item.get('pct_change'), 2, '%')} {_money(item.get(amount_key))} {amount_rank} "
                f"{item.get('limit_mark') or ''}").strip()
    seal_extreme = extremes.get("verified_seal_max")
    seal_text = (f"已核实封单最大 {seal_extreme.get('name') or seal_extreme.get('symbol')} "
        f"{_money(seal_extreme.get('seal_amount_yuan'))} 封单#{seal_extreme.get('seal_rank') or '—'} "
        f"额#{seal_extreme.get('amount_rank') or '—'}" if isinstance(seal_extreme, Mapping) else "已核实封单最大待核实")
    market_extreme_text = "；".join((extreme_text("全市场最高 ", extremes.get("highest")),
        extreme_text("全市场最低 ", extremes.get("lowest")), seal_text))

    plate_rows = [row for row in product.get("plates", []) if isinstance(row, Mapping)]
    plate_html = []
    plate_text = []
    for plate in plate_rows[:10]:
        name = _esc(plate.get("plate_name") or plate.get("plate_id") or "—")
        reasons = plate.get("selection_reasons") if isinstance(plate.get("selection_reasons"), list) else []
        badges = " ".join(f"[{_esc(label)}]" for label in reasons)
        valid_n = plate.get("valid_price_count", "—")
        total_n = plate.get("count", plate.get("member_count", "—"))
        share = plate.get("market_share")
        amount_share = _fmt(share * 100, 1, "%") if isinstance(share, (int, float)) else "—"
        dist = plate.get("distribution") if isinstance(plate.get("distribution"), Mapping) else {}
        dist_n = [dist.get("ge_3_n"), dist.get("zero_to_3_n"), dist.get("lt_0_n")]
        denom = dist.get("valid_n")
        dist_share = [f"{n}/{denom} ({_fmt(n / denom * 100, 0, '%')})" if isinstance(n, int) and isinstance(denom, int) and denom else "—" for n in dist_n]
        bar = "▰" * min(10, max(0, round((plate.get("up_ratio") or 0) * 10))) + "▱" * (10 - min(10, max(0, round((plate.get("up_ratio") or 0) * 10))))
        split = plate.get("extremes") if isinstance(plate.get("extremes"), Mapping) else {}
        standout = []
        standout_plain = []
        for key, label in (("strongest", "最强"), ("weakest", "最弱")):
            item = split.get(key)
            if isinstance(item, Mapping):
                standout.append(f"{label}{_esc(item.get('name') or item.get('symbol'))} {_pct_cell(item.get('pct_change'))} {_esc(_rank_cell(item.get('amount_rank'), '额'))}")
                standout_plain.append(f"{label}{item.get('name') or item.get('symbol')} {_fmt(item.get('pct_change'), 2, '%')} {_rank_cell(item.get('amount_rank'), '额')}")
        extremes_cell = "；".join(standout) if standout else "无明显极值"
        relay = plate.get("yesterday_relay") if isinstance(plate.get("yesterday_relay"), Mapping) else {}
        relay_counts = relay.get("open_counts") if isinstance(relay.get("open_counts"), Mapping) else {}
        relay_text = (f"昨涨停{relay.get('n', '—')}只 高/平/低 {relay_counts.get('high', '—')}/"
            f"{relay_counts.get('flat', '—')}/{relay_counts.get('low', '—')}；拟晋级{relay.get('provisional_n', '—')} "
            f"未晋级迹象{relay.get('not_promoted_n', '—')} 待核实{relay.get('unverified_n', '—')}；{relay.get('willingness', '待核实')}")
        seal = plate.get("seal_extreme") if isinstance(plate.get("seal_extreme"), Mapping) else None
        seal_cell = (f"{plate.get('verified_seal_n', 0)}只·{_money(plate.get('verified_seal_amount_yuan'))}"
            + (f"；{_esc(seal.get('name') or seal.get('symbol'))} {_money(seal.get('seal_amount_yuan'))} 封单#{seal.get('seal_rank') or '—'} 额#{seal.get('amount_rank') or '—'}" if seal else ""))
        dispersion = plate.get("dispersion_p75_p25_pp")
        dispersion_text = (_fmt(dispersion, 2, "pp") if isinstance(dispersion, (int, float)) and
                           dispersion >= plate.get("dispersion_visible_threshold_pp", 4.0) else "—")
        cells = [f"{name}<br><small>{badges}</small>", f"{valid_n}/{total_n}",
            f"{_money(plate.get('amount_yuan'))}<br>{amount_share}", _pct_cell(plate.get("member_median_pct")),
            f"{plate.get('up_count', '—')}/{valid_n} {bar}",
            f"≥+3% {dist_share[0]}；0≤x&lt;+3% {dist_share[1]}；&lt;0 {dist_share[2]}<br>多数行为:{_esc(dist.get('majority') or '待核实')}",
            dispersion_text, extremes_cell,
            relay_text, seal_cell]
        plate_html.append("<tr>" + "".join(f'<td style="padding:5px;border-bottom:1px solid #eaecf0;vertical-align:top">{cell}</td>' for cell in cells) + "</tr>")
        distribution_text = f"≥+3% {dist_share[0]}；0≤x<+3% {dist_share[1]}；<0 {dist_share[2]}"
        plate_text.append(f"{plate.get('plate_name') or plate.get('plate_id')} [{'/'.join(reasons)}] 成员{valid_n}/{total_n}；额占比{amount_share}；中位{_fmt(plate.get('member_median_pct'),2,'%')}；上涨{plate.get('up_count','—')}/{valid_n}；分档{distribution_text} 多数行为{dist.get('majority') or '待核实'}；离散度{dispersion_text}；极值{';'.join(standout_plain) or '无明显极值'}；接力{relay_text}；封单{seal_cell}")
    plate_head = ("题材[来源徽标]", "成员有效/总数", "竞价额·占比", "中位涨幅", "上涨占比", "内部分化分档·多数行为", "离散度P75−P25", "极值", "昨日接力", "封单")
    plate_table = '<table role="table" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;table-layout:fixed;font-size:10px"><thead><tr>' + ''.join(_th(label) for label in plate_head) + '</tr></thead><tbody>' + ''.join(plate_html) + '</tbody></table>'

    buckets = overview.get("yesterday_reason_buckets", [])
    bucket_html, bucket_text = [], []
    for bucket in buckets if isinstance(buckets, list) else []:
        if not isinstance(bucket, Mapping):
            continue
        open_counts = bucket.get("open_counts") if isinstance(bucket.get("open_counts"), Mapping) else {}
        extreme_values = bucket.get("extremes") if isinstance(bucket.get("extremes"), Mapping) else {}
        extreme_parts = []
        extreme_plain = []
        for key, label in (("strongest", "最强"), ("weakest", "最弱")):
            item = extreme_values.get(key)
            if isinstance(item, Mapping):
                extreme_parts.append(f"{label}{_esc(item.get('name') or item.get('symbol'))} {_pct_cell(item.get('pct_change'))} {_esc(_rank_cell(item.get('amount_rank'), '额'))}")
                extreme_plain.append(f"{label}{item.get('name') or item.get('symbol')} {_fmt(item.get('pct_change'), 2, '%')} {_rank_cell(item.get('amount_rank'), '额')}")
        extremes_cell = "；".join(extreme_parts) if extreme_parts else "无明显极值"
        share = bucket.get("market_amount_share")
        share_text = _fmt(share * 100, 1, "%") if isinstance(share, (int, float)) else "—"
        if str(bucket.get("reason", "")).startswith("其他单只题材("):
            cells = [f"{_esc(bucket.get('reason'))}<br>非共同题材", str(bucket.get("count", "—")),
                "—", "—", "—", "—", _esc(bucket.get("willingness") or "不适用·非共同题材"), "—",
                f"{_money(bucket.get('auction_amount_yuan'))} · {share_text}"]
            bucket_html.append("<tr>" + "".join(f'<td style="padding:5px;border-bottom:1px solid #eaecf0;vertical-align:top">{cell}</td>' for cell in cells) + "</tr>")
            bucket_text.append(f"{bucket.get('reason')}：{bucket.get('count')}只；竞价额{_money(bucket.get('auction_amount_yuan'))}，占比{share_text}；不适用·非共同题材")
            continue
        cells = [f"{_esc(bucket.get('reason') or '原因待核实')}<br>高标昨≥2板", str(bucket.get("count", "—")),
            str(bucket.get("highest_streak") or "—"), f"{open_counts.get('high','—')}/{open_counts.get('flat','—')}/{open_counts.get('low','—')}",
            _pct_cell(bucket.get("median_pct")), f"拟晋级{bucket.get('provisional_n','—')} / 未晋级迹象{bucket.get('not_promoted_n','—')} / 待核实{bucket.get('unverified_n','—')}",
            _esc(bucket.get("willingness") or "待核实"), extremes_cell,
            f"{_money(bucket.get('auction_amount_yuan'))} · {share_text}"]
        bucket_html.append("<tr>" + "".join(f'<td style="padding:5px;border-bottom:1px solid #eaecf0;vertical-align:top">{cell}</td>' for cell in cells) + "</tr>")
        bucket_text.append(f"{bucket.get('reason')} 昨涨停{bucket.get('count')}只最高昨{bucket.get('highest_streak')}板；高/平/低{open_counts.get('high')}/{open_counts.get('flat')}/{open_counts.get('low')}；中位{_fmt(bucket.get('median_pct'),2,'%')}；拟晋级{bucket.get('provisional_n')} 未晋级迹象{bucket.get('not_promoted_n')} 待核实{bucket.get('unverified_n')}；{bucket.get('willingness')}；极值{';'.join(extreme_plain) or '无明显极值'}；额占比{share_text}")
    bucket_head = ("题材", "昨涨停家数", "最高板", "今日高/平/低", "中位涨幅", "晋级迹象", "接力意愿", "极值股", "竞价额占比")
    bucket_table = '<table role="table" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;table-layout:fixed;font-size:10px"><thead><tr>' + ''.join(_th(label) for label in bucket_head) + '</tr></thead><tbody>' + ''.join(bucket_html) + '</tbody></table>'
    high_boards = overview.get("yesterday_pool_high_boards", [])
    highboard_lines = [f"{item.get('name') or item.get('symbol')} · 昨{item.get('yesterday_streak')}板 · {item.get('primary_reason') or '原因待核实'} · {_fmt(item.get('pct_change'),2,'%')} · {_money(item.get('auction_amount_yuan'))} {_rank_cell(item.get('amount_rank'),'额')}" for item in high_boards if isinstance(item, Mapping)]
    highboard_text = "；".join(highboard_lines) if highboard_lines else "暂无可核实高标"
    highboard_html = "<br>".join(_esc(line) for line in highboard_lines) if highboard_lines else "暂无可核实高标"
    history = overview.get("amount_multiple_64d") if isinstance(overview.get("amount_multiple_64d"), Mapping) else {}
    market_up = overview.get("up_ratio") if isinstance(overview.get("up_ratio"), Mapping) else {}
    seal_metric = metrics.get("seal") if isinstance(metrics.get("seal"), Mapping) else {}
    cards = [("竞价额", f"{_fmt(overview.get('auction_amount_yi', market.get('auction_amount_yuan') / 1e8 if isinstance(market.get('auction_amount_yuan'), (int,float)) else None), 2, '亿')} · 基线样本{history.get('sample_n','—')}/{history.get('window_n',64)}"),
        ("上涨占比·中位", f"{_fmt(market_up.get('ratio') * 100 if isinstance(market_up.get('ratio'), (int,float)) else None, 1, '%')} · {_fmt(overview.get('market_median_pct'),2,'%')}"),
        ("昨板高开", f"{pool_counts.get('high','—')}/{pool_valid if pool_valid is not None else '—'} · {_fmt(overview.get('yesterday_pool_high_open_gap_pp'),1,'pp')}"),
        ("已核实封单", f"{seal_metric.get('verified_n', overview.get('yesterday_pool_verified_seal_n','—'))}只 · {_money(seal_metric.get('total_yuan', overview.get('verified_seal_sum_yuan')))}")]
    cards_html = '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>' + ''.join(
        f'<td style="padding:3px;width:25%"><div style="background:#f6f8fb;padding:7px"><small>{_esc(label)}</small><br><b>{_esc(value)}</b></div></td>' for label, value in cards) + '</tr></table>'
    market_extremes_html = _esc(market_extreme_text)
    replay_pill = '<span style="background:#fff4e5;color:#7a4b00;padding:3px 7px;border-radius:10px">历史回放样本·非实时信号</span>' if replay else ""
    degraded_html = f'<div style="color:#7c2d12;font-size:10px">{_esc(degraded_note)}</div>' if status != "READY" and degraded_note else ""
    aggregate_html = _esc(whole_batch)
    html = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>@media(max-width:700px){table{font-size:9px!important}td{padding:3px!important}}</style></head>'
        '<body style="margin:0;padding:8px;color:#26364a;background:#eef2f6;font-family:Arial,sans-serif"><table role="presentation" width="100%"><tr><td align="center">'
        '<table role="presentation" width="100%" style="max-width:1100px;background:#fff;border-collapse:collapse"><tr><td style="padding:12px">'
        f'<h1 style="font-size:16px;margin:0 0 5px">竞价速览卡 · {_esc(date)} · {_esc(asof)} · {status}</h1>{replay_pill}{degraded_html}{cards_html}'
        f'<div style="padding:7px;background:#f4f7fb;margin:8px 0"><b>昨日整批统计 / 市场整体</b><br>{aggregate_html}</div>'
        f'<div style="margin:5px 0">{_esc(next((line.split(":",1)[1].strip() for line in lines if line.startswith("结论:")), "结论待核实"))}</div>'
        f'<div style="font-size:10px;color:#667085">{_esc(threshold)}</div><h2>① 题材横向对比</h2>{plate_table}'
        f'<h2>② 昨日涨停×题材 接力意愿</h2>{bucket_table}'
        f'<h2>高标与封单极值</h2><p style="font-size:10px">高标（昨≥2板）</p><div>{highboard_html}</div><p>{market_extremes_html}</p>'
        '<p style="font-size:10px;color:#667085">完整明细已存档于 Core 报告目录；个股点名仅用于极值、封单和高标。缺失不补0。</p>'
        '</td></tr></table></td></tr></table></body></html>')
    plain_blocks = [f"09:26竞价速览 | {date} {asof} {('历史回放样本,非当时实时信号' if replay else '')}",
        f"昨日整批统计 / 市场整体：{whole_batch}",
        "①题材横向对比：" + ("\n".join(plate_text) or "无可展示题材"),
        "②昨日涨停×题材接力意愿：" + ("\n".join(bucket_text) or "无可展示题材"),
        "高标与封单极值：" + highboard_text + "；" + market_extreme_text + f"；{threshold}；完整明细已存档于 Core 报告目录"]
    return html, "\n\n".join(plain_blocks)

def render_auction_panorama(product: Mapping[str, Any]) -> tuple[str, str]:
    """Render a complete two-table V6 page and its matching plain-text version."""
    overview = product.get("overview") if isinstance(product.get("overview"), Mapping) else {}
    history = overview.get("amount_multiple_64d") if isinstance(overview.get("amount_multiple_64d"), Mapping) else {}
    up = overview.get("up_ratio") if isinstance(overview.get("up_ratio"), Mapping) else {}
    yesterday = overview.get("yesterday_limit_high_open") if isinstance(overview.get("yesterday_limit_high_open"), Mapping) else {}
    amount = _fmt(overview.get("auction_amount_yi"), 2, "亿")
    multiple_value, percentile = history.get("value"), history.get("percentile")
    history_ready = history.get("status") == "READY" and history.get("sample_n", 0) >= 20
    if history_ready:
        history_text = (f"{_fmt(multiple_value, 2)}倍 / "
                        f"{_fmt(percentile * 100 if isinstance(percentile, (int, float)) else None, 1)}分位")
    else:
        history_text = (f"基线样本{history.get('sample_n', 0)}/{history.get('window_n', 64)}"
                        "（样本不足，仅参考）")
    if history_ready and history.get("basis") == "REPLAY_EVENT_TIME":
        history_text += "（回放·按事件时间）"
    up_ratio = up.get("ratio")
    up_text = (f"{_fmt(up_ratio * 100, 1, '%')} · 中位{_fmt(overview.get('market_median_pct'), 2, '%')}"
               if isinstance(up_ratio, (int, float)) else "待核实")
    high_open = (f"{yesterday.get('numerator', '—')}/{yesterday.get('denominator', '—')} · 较市场"
                 f" {_fmt(overview.get('yesterday_vs_market_pp'), 2, 'pp')}" )
    seal_scope = overview.get("verified_seal_scope") or "待核实"
    seal_sum = overview.get("verified_seal_sum_yuan")
    seal_text = (f"{_fmt(seal_sum / 1e8, 2, '亿')}（核实 {seal_scope}，{overview.get('verified_seal_label') or '待核实'}）"
                 if isinstance(seal_sum, (int, float)) else f"待核实（{seal_scope}）")
    cards = [("竞价匹配额", amount + " · " + history_text), ("上涨 / 中位涨幅", up_text),
             ("昨日涨停高开", high_open + "（按竞价价>昨收）"), ("竞价涨停封单合计", seal_text)]
    conclusions = []
    if not history_ready:
        conclusions.append("竞价额基线不足，仅参考")
    yn, yd = yesterday.get("numerator"), yesterday.get("denominator")
    market_ratio = up.get("ratio")
    if (isinstance(yn, (int, float)) and isinstance(yd, (int, float)) and yd > 0
            and isinstance(market_ratio, (int, float))):
        yesterday_ratio = yn / yd
        if yesterday_ratio < market_ratio:
            conclusions.append(f"昨日涨停高开率{_fmt(yesterday_ratio * 100, 1, '%')}低于市场{_fmt(market_ratio * 100, 1, '%')}")
        else:
            conclusions.append(f"昨日涨停高开率{_fmt(yesterday_ratio * 100, 1, '%')}不低于市场{_fmt(market_ratio * 100, 1, '%')}")
    rule_sentence = "；".join(conclusions) or "暂无可核实的结论"
    pool_amount = overview.get("yesterday_pool_auction_amount_yuan")
    pool_share = overview.get("yesterday_pool_market_share")
    open_counts = overview.get("yesterday_pool_open_counts", {})
    pool_summary = (f"昨日涨停池竞价额 {_money(pool_amount)}（占已知市场额 {_fmt(pool_share * 100 if isinstance(pool_share, (int, float)) else None, 1, '%')}）；"
                    f"高/平/低开 {open_counts.get('high', '—')}/{open_counts.get('flat', '—')}/{open_counts.get('low', '—')}（{overview.get('yesterday_pool_open_valid_n', '—')}只有效价）"
                    if isinstance(open_counts, Mapping) else "昨日涨停池汇总待核实")
    multi_mean, first_mean = overview.get("multi_board_mean_auction_amount_yuan"), overview.get("first_board_mean_auction_amount_yuan")
    mean_multiple = overview.get("multi_to_first_board_mean_multiple")
    bias_summary = (f"昨日≥2板单股均额 {_money(multi_mean)}，首板 {_money(first_mean)}，约 {_fmt(mean_multiple, 2, '倍')}；未控制流动性"
                    if multi_mean is not None or first_mean is not None else "昨日≥2板与首板单股均额待核实")
    worst = overview.get("yesterday_pool_worst_two", [])
    worst_text = "、".join(f"{row.get('name') or row.get('symbol')} {_fmt(row.get('pct_change'), 2, '%')}"
                           for row in worst if isinstance(row, Mapping)) or "待核实"
    verified_n = overview.get("yesterday_pool_verified_seal_n")
    verified_sum = overview.get("yesterday_pool_verified_seal_sum_yuan")
    global_verified_n = None
    if isinstance(seal_scope, str) and "/" in seal_scope:
        try:
            global_verified_n = int(seal_scope.split("/", 1)[0])
        except ValueError:
            pass
    pool_seal_share = (verified_sum / seal_sum if isinstance(verified_sum, (int, float))
                       and isinstance(seal_sum, (int, float)) and seal_sum > 0 else None)
    feedback_summary = (f"昨日池最弱：{worst_text}；全市场已核实封单 {global_verified_n if global_verified_n is not None else '—'} 只、合计 {_money(seal_sum)}；"
                        f"昨日涨停池内已核实封单 {verified_n if verified_n is not None else '—'} 只、合计 {_money(verified_sum)}，占全市场 {_fmt(pool_seal_share * 100 if pool_seal_share is not None else None, 1, '%')}")
    summaries = [pool_summary, bias_summary, feedback_summary]
    cards_html = '<table role="presentation" width="100%"><tr>' + ''.join(
        f'<td style="width:50%;padding:4px;vertical-align:top"><div style="background:#f6f8fb;border:1px solid #e4e7ec;padding:9px">'
        f'<div style="font-size:10px;color:#667085">{_esc(label)}</div><strong style="display:block;padding-top:3px;font-size:13px">{_esc(value)}</strong></div></td>'
        for label, value in cards[:2]) + '</tr><tr>' + ''.join(
        f'<td style="width:50%;padding:4px;vertical-align:top"><div style="background:#f6f8fb;border:1px solid #e4e7ec;padding:9px">'
        f'<div style="font-size:10px;color:#667085">{_esc(label)}</div><strong style="display:block;padding-top:3px;font-size:13px">{_esc(value)}</strong></div></td>'
        for label, value in cards[2:4]) + '</tr></table>'
    cards_html += (f'<div style="margin:5px 4px;padding:8px 9px;background:#f6f8fb;border:1px solid #e4e7ec">'
                   f'<span style="font-size:10px;color:#667085">规则一句话</span>'
                   f'<strong style="display:block;padding-top:3px;font-size:12px">{_esc(rule_sentence)}</strong></div>')
    if not history_ready:
        cards_html = cards_html.replace("（样本不足，仅参考）", '<span style="color:#98a2b3">（样本不足，仅参考）</span>')

    plates = [row for row in product.get("plates", []) if isinstance(row, Mapping)]
    hot_n = sum(1 for row in plates if isinstance(row.get("hot_rank"), int))
    lineage = product.get("lineage") if isinstance(product.get("lineage"), Mapping) else {}
    hot_basis = lineage.get("hot_rank_basis", "UNKNOWN")
    hot_note = ("收盘后回溯，非09:26快照，仅供开发评估" if hot_basis == "POSTHOC_DEV"
                else "热榜名次按报告前真实抓取快照展示。" if hot_basis == "PIT_LIVE"
                else "热榜名次暂无可用快照。")
    hot_note = f"{hot_basis}：{hot_note}；热榜覆盖 {hot_n}/{len(plates)}"
    hot_coverage_note = (f"热榜覆盖 {hot_n}/{len(plates)}；覆盖分母为本页选中板块，未收录于热榜计为 —。")
    plate_rows, plate_text = [], []
    for plate in plates:
        name = plate.get("plate_name") or plate.get("plate_id") or "板块待核实"
        size = plate.get("count", plate.get("member_count"))
        scale = plate.get("scale") or "待核实"
        count = f"{scale}({size if size is not None else '—'}只)" 
        member_median = plate.get("member_median_pct", plate.get("equal_weight_pct"))
        stats = (f"板块额 {_money(plate.get('amount_yuan'))} · 占市场 {_fmt((plate.get('market_share') or 0) * 100 if plate.get('market_share') is not None else None, 1, '%')}"
                 f"<br>成员涨幅中位数 {_fmt(member_median, 2, '%')} · 上涨 {plate.get('up_count', '—')}/{plate.get('valid_price_count', '—')}"
                 f" · 价格有效 {plate.get('valid_price_count', '—')}/{size if size is not None else '—'}"
                 f" · 竞价额有效 {plate.get('valid_amount_count', '—')}/{size if size is not None else '—'}"
                 f"<br>Top1占比 {_fmt((plate.get('top1_share') or 0) * 100 if plate.get('top1_share') is not None else None, 1, '%')} · 开盘啦热板名次：{_esc(_hot_rank(plate.get('hot_rank')))}")
        stock_list = plate.get("stocks") if isinstance(plate.get("stocks"), list) else []
        stocks = "<br>".join(_esc(_stock_text(stock)) for stock in stock_list if isinstance(stock, Mapping)) or "个股待核实"
        plate_rows.append("<tr>" + _td(f"{_esc(name)}<br><span style=\"color:#667085\">{_esc(count)}</span>") + _td(stats + "<br>" + stocks) + "</tr>")
        reasons = plate.get("selection_reasons", plate.get("selection_reason", []))
        if isinstance(reasons, str): reasons = [reasons]
        reason_text = "/".join(str(reason) for reason in reasons or [])
        plate_text.append(f"{name}（{count}；选择：{reason_text or '待核实'}）：板块额 {_money(plate.get('amount_yuan'))}；占市场 {_fmt((plate.get('market_share') or 0) * 100 if plate.get('market_share') is not None else None, 1, '%')}；成员涨幅中位数 {_fmt(member_median, 2, '%')}；上涨 {plate.get('up_count', '—')}/{plate.get('valid_price_count', '—')}；价格有效 {plate.get('valid_price_count', '—')}/{size if size is not None else '—'}；竞价额有效 {plate.get('valid_amount_count', '—')}/{size if size is not None else '—'}；Top1占比 {_fmt((plate.get('top1_share') or 0) * 100 if plate.get('top1_share') is not None else None, 1, '%')}；开盘啦热板名次：{_hot_rank(plate.get('hot_rank'))}\n" + "\n".join(_stock_text(stock) for stock in stock_list if isinstance(stock, Mapping)))
    plate_table = (f'<table style="{_table_style()}" role="table"><thead><tr>{_th("板块（规模·只数）", "25%")}{_th("竞价数据与内部个股")}</tr></thead><tbody>{"".join(plate_rows)}</tbody></table>')

    ladder = [row for row in product.get("yesterday_ladder", []) if isinstance(row, Mapping)]
    ladder_rows, ladder_text = [], []
    for layer in sorted(ladder, key=lambda row: -(row.get("yesterday_streak") or 0)):
        level = layer.get("yesterday_streak", "—")
        for bucket in layer.get("buckets", []) if isinstance(layer.get("buckets"), list) else []:
            if not isinstance(bucket, Mapping):
                continue
            single_bucket = bucket.get("count") == 1
            share_html = ("" if single_bucket else
                          f" · 本层占比 {_fmt((bucket.get('layer_amount_share') or 0) * 100 if bucket.get('layer_amount_share') is not None else None, 1, '%')}")
            promotion_text = (f"拟晋级 {bucket.get('provisional_promotion_count', 0)} · 待核实 {bucket.get('unverified_promotion_count', 0)}"
                              f" · 未晋级迹象 {bucket.get('not_promoted_count', 0)}"
                              if "unverified_promotion_count" in bucket else
                              f"拟晋级 {bucket.get('provisional_promotion_count', '—')}")
            bucket_meta = (f"昨{level}板 · {_esc(bucket.get('reason') or '原因待核实')} · {bucket.get('count', '—')}只"
                f"<br>竞价额 {_money(bucket.get('auction_amount_yuan'))}{share_html}"
                f" · 高开/低开 {_fmt((bucket.get('high_open_ratio') or 0) * 100 if bucket.get('high_open_ratio') is not None else None, 1, '%')}/{_fmt((bucket.get('low_open_ratio') or 0) * 100 if bucket.get('low_open_ratio') is not None else None, 1, '%')}"
                f" · 中位 {_fmt(bucket.get('median_pct'), 2, '%')} · {promotion_text} · 真实封单额 {_money(bucket.get('verified_seal_amount_yuan'))}")
            stocks = bucket.get("stocks") if isinstance(bucket.get("stocks"), list) else []
            stock_html = "<br>".join(_esc(_stock_text(stock)) for stock in stocks if isinstance(stock, Mapping)) or "个股待核实"
            ladder_rows.append("<tr>" + _td(bucket_meta) + _td(stock_html) + "</tr>")
            share_text = ("" if single_bucket else
                          f"；本层占比 {_fmt((bucket.get('layer_amount_share') or 0) * 100 if bucket.get('layer_amount_share') is not None else None, 1, '%')}")
            ladder_text.append(f"昨{level}板 · {bucket.get('reason') or '原因待核实'} · {bucket.get('count', '—')}只；竞价额 {_money(bucket.get('auction_amount_yuan'))}{share_text}；中位 {_fmt(bucket.get('median_pct'), 2, '%')}；{promotion_text}；真实封单额 {_money(bucket.get('verified_seal_amount_yuan'))}\n" + "\n".join(_stock_text(stock) for stock in stocks if isinstance(stock, Mapping)))
    ladder_table = f'<table style="{_table_style()}" role="table"><thead><tr>{_th("昨日板高·原因桶", "42%")}{_th("个股明细（全部昨日涨停股）")}</tr></thead><tbody>{"".join(ladder_rows)}</tbody></table>'
    text = (f"竞价全景 V6 | {product.get('trade_date', '日期待核实')} | {product.get('asof', '时点待核实')}\n"
        + " | ".join(f"{label}：{value}" for label, value in cards) + f"\n规则一句话：{rule_sentence}\n" + "\n".join(summaries) + f"\n{hot_note}\n{hot_coverage_note}\n\n① 板块竞价全景\n"
        + "\n\n".join(plate_text) + "\n\n② 昨日连板梯队×涨停原因\n" + "\n\n".join(ladder_text)
        + "\n\n缺失字段留白或待核实；名次为全市场口径。")
    document = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>竞价全景 V6</title></head><body style="margin:0;padding:10px;background:#eef2f6;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#26364a">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center"><table role="presentation" width="100%" style="max-width:760px;background:#fff">'
        f'<tr><td style="padding:12px 14px;background:#172b43;color:#fff"><div style="font-size:16px;font-weight:700">竞价全景 V6　·　{_esc(product.get("trade_date", "日期待核实"))}　·　{_esc(product.get("asof", "时点待核实"))}　·　{_esc(overview.get("status", "PARTIAL"))}</div></td></tr>'
        f'<tr><td style="padding:10px">{cards_html}<div style="margin:6px 0;font-size:11px">{"<br>".join(_esc(line) for line in summaries)}</div><div style="margin:6px 0;color:#7a4b00;font-size:11px">{_esc(hot_note)}</div>'
        f'<h2 style="font-size:16px;margin:18px 0 7px">① 板块竞价全景</h2>{plate_table}'
        f'<h2 style="font-size:16px;margin:18px 0 7px">② 昨日连板梯队×涨停原因</h2>{ladder_table}'
        f'<div style="font-size:10px;color:#667085;margin-top:12px">{_esc(hot_coverage_note)} 缺失字段留白或待核实；名次为全市场口径。</div></td></tr></table></td></tr></table></body></html>')
    quickbrief = product.get("quickbrief")
    if isinstance(quickbrief, Mapping) and isinstance(quickbrief.get("lines"), list):
        quick_lines = [str(line) for line in quickbrief.get("email_lines", quickbrief["lines"])]
        diagnostics = [str(line) for line in quickbrief["lines"]
                       if str(line).startswith(("备看排序", "流动性门槛排除"))]
        quick_plain = "\n".join(quick_lines) + "\n\n"
        quick_html = '<div style="padding:10px 12px;margin:8px 0;background:#f2f6fb;border-left:4px solid #2471a3;font-size:13px;line-height:1.7">' + "<br>".join(_esc(line) for line in quick_lines) + "</div>"
        if diagnostics:
            quick_html += ('<details style="margin:4px 0 10px;color:#667085;font-size:11px"><summary>速览计算诊断</summary>'
                           + '<br>'.join(_esc(line) for line in diagnostics) + '</details>')
            quick_plain += "速览计算诊断：\n" + "\n".join(diagnostics) + "\n\n"
        text = text.replace("竞价全景 V6 |", quick_plain + "竞价全景 V6 |", 1)
        document = document.replace('<tr><td style="padding:10px">', '<tr><td style="padding:10px">' + quick_html, 1)
    archive_note = "完整明细已存档于 Core 报告目录"
    document = document.replace("</body>", f'<p style="font-size:10px;color:#667085">{archive_note}</p></body>', 1)
    return document, text + "\n\n" + archive_note
