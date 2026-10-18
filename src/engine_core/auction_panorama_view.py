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
    rule_sentence = (f"竞价额{amount}（{history_text}）；上涨占比{_fmt(up_ratio * 100 if isinstance(up_ratio, (int, float)) else None, 1, '%')}；"
                     f"昨日涨停高开{yesterday.get('numerator', '—')}/{yesterday.get('denominator', '—')}，较市场"
                     f"{_fmt(overview.get('yesterday_vs_market_pp'), 2, 'pp')}（均按竞价价>昨收）；封单合计{_money(seal_sum)}")
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
    feedback_summary = (f"昨日池最弱：{worst_text}；封单已核实 {verified_n if verified_n is not None else '—'} 只、合计 {_money(verified_sum)}")
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
    hot_coverage_note = ("开盘啦热榜口径=热板Top50；258行版本为更宽的列表，当前默认选用50行版本，预览覆盖0/10为实情。"
                         "覆盖分母为本页选中板块，未收录于热榜计为 —。")
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
    return document, text
