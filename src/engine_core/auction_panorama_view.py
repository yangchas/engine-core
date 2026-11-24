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


def _streak_and_hot_conclusions(product: Mapping[str, Any]) -> list[str]:
    groups: dict[str, list[Mapping[str, Any]]] = {"2板": [], "3板": [], "4板及以上": []}
    for layer in product.get("yesterday_ladder", []):
        if not isinstance(layer, Mapping):
            continue
        level = layer.get("yesterday_streak")
        if not isinstance(level, int) or level < 2:
            continue
        key = f"{level}板" if level < 4 else "4板及以上"
        for bucket in layer.get("buckets", []):
            if isinstance(bucket, Mapping):
                groups[key].extend(row for row in bucket.get("stocks", []) if isinstance(row, Mapping))
    def describe(label: str, rows: list[Mapping[str, Any]]) -> str:
        valid = [r for r in rows if r.get("valid_price", True) is True and isinstance(r.get("pct_change"), (int, float))]
        weighted = [r for r in valid if isinstance(r.get("auction_amount_yuan"), (int, float)) and r["auction_amount_yuan"] > 0]
        total = sum(r["auction_amount_yuan"] for r in weighted)
        mean = sum(r["pct_change"] for r in valid) / len(valid) if valid else None
        weight = sum(r["pct_change"] * r["auction_amount_yuan"] for r in weighted) / total if total else None
        missing = [str(r.get("symbol") or "未知代码") for r in rows if r not in valid]
        return (f"{label}：均涨 {_fmt(mean, 2, '%')}，加权 {_fmt(weight, 2, '%')}"
                f"（有效{len(valid)}/{len(rows)}，加权有效{len(weighted)}；缺失 {','.join(missing) or '无'}）")
    all_rows = [row for rows in groups.values() for row in rows]
    lines = ["【昨日连板开盘平均涨幅】" + "；".join(describe(k, v) for k, v in groups.items()) + "；" + describe("总体", all_rows)]
    market_mean = (product.get("overview") or {}).get("market_mean_pct")
    valid_all = [row["pct_change"] for row in all_rows if row.get("valid_price", True) is True
                 and isinstance(row.get("pct_change"), (int, float))]
    if isinstance(market_mean, (int, float)) and valid_all:
        gap = sum(valid_all) / len(valid_all) - market_mean
        lines.append(f"短线赚钱效应：昨日连板均涨较全市场{gap:+.2f}pp（全市场均涨 {_fmt(market_mean, 2, '%')}）")
    else:
        lines.append("短线赚钱效应：UNKNOWN（缺全市场或昨日连板有效竞价涨幅）")
    lines.append("【昨日热板溢价】UNKNOWN（缺昨日热板同版本成份股及全市场竞价平均涨幅）")
    tb = product.get("theme_buckets") or {}
    seals = tb.get("seals") if isinstance(tb, Mapping) else None
    valid = [s for s in (seals or []) if isinstance(s, Mapping)
             and s.get("side") in ("limit_up", "limit_down")
             and (s.get("seal_amount_yuan") or s.get("amount_yuan"))]
    if valid:
        up = [s for s in valid if s["side"] == "limit_up"]
        down = [s for s in valid if s["side"] == "limit_down"]
        total = lambda items: sum(s.get("seal_amount_yuan") or s.get("amount_yuan") for s in items)
        lines.append(f"竞价涨停封单合计：{_money(total(up))}（{len(up)}只）；竞价跌停封单合计：{_money(total(down))}（{len(down)}只）")
    else:
        lines.append("竞价涨停封单合计：UNKNOWN；竞价跌停封单合计：UNKNOWN（缺逐股封单方向及终局盘口）")
    return lines


def _theme_behavior_tag(t: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return (label, badge_html, plain_text) indicating primary driver behavior."""
    w_bp = t.get("weighted_mean_change_bp")
    m_bp = t.get("mean_change_bp")
    s_cnt = t.get("strong_count") or 0
    w_cnt = t.get("weak_count") or 0
    up_cnt = t.get("limit_up_count") or 0
    down_cnt = t.get("limit_down_count") or 0
    gap_bp = (w_bp - m_bp) if (isinstance(w_bp, (int, float)) and isinstance(m_bp, (int, float))) else 0

    n = t.get("auction_count") or 0
    up_ratio = t.get("up_ratio") if isinstance(t.get("up_ratio"), (int, float)) else 0.0
    if (up_cnt > 0 and down_cnt > 0) or (s_cnt >= 2 and w_cnt >= 5):
        label = "剧烈分化"
    elif isinstance(m_bp, (int, float)) and m_bp >= 250 and up_ratio >= .75 and (up_cnt >= 2 or (n > 0 and s_cnt / n >= .40)):
        label = "共振主升"
    elif up_cnt >= 4 and ((isinstance(m_bp, (int, float)) and m_bp >= 150) or up_ratio >= .65):
        label = "涨停潮"
    elif isinstance(w_bp, (int, float)) and w_bp > 0 and gap_bp >= 150:
        label = "大票领涨"
    elif isinstance(m_bp, (int, float)) and m_bp >= 150 and up_ratio >= .70:
        label = "普涨活跃"
    elif isinstance(w_bp, (int, float)) and w_bp <= 0 and gap_bp >= 100:
        label = "大票抗跌"
    elif gap_bp <= -100:
        label = "大票拖累"
    elif isinstance(w_bp, (int, float)) and isinstance(m_bp, (int, float)) and w_bp < -100 and m_bp < -100:
        label = "承压走弱"
    else:
        label = "平稳分化"
    badge = f'<span style="display:inline-block;padding:1px 3px;font-size:9px;background:#f5f5f5;color:#595959;border-radius:2px">{label}</span>'
    return label, badge, f"[{label}]"


def _render_theme_buckets_section(tb: Mapping[str, Any] | None, *, for_email: bool = False,
                                   table1_plates: set[str] | list[str] | None = None) -> tuple[str, list[str]]:
    """Render Table ③ Theme Buckets block for email digest or full panorama report."""
    if not isinstance(tb, Mapping):
        return "", []
    tb_html_block = ""
    tb_text_lines = []
    h2_style = "" if for_email else ' style="font-size:16px;margin:18px 0 7px"'
    subtitle_size = "10px" if for_email else "11px"
    subtitle_span = f' <span style="font-size:{subtitle_size};font-weight:normal;color:#667085">[热榜成份股×盘前映射]</span>'

    if tb.get("status") in ("available", "OK"):
        from .auction_theme_cards import non_theme_reason
        curated = tb.get("curated_themes")
        if curated:
            curated = [t for t in curated if not non_theme_reason(t.get("name") or "", extended=True)]
        else:
            raw = tb.get("top_amount") or tb.get("themes") or []
            curated = [
                t for t in raw
                if not non_theme_reason(t.get("name") or "", extended=True)
                and (t.get("amount_yuan") or 0) >= 30_000_000
                and (t.get("auction_count") or 0) >= 5
                and (t.get("top1_share") or 0) < 0.80
            ]
        top_themes = curated[:10]
        tb_rows = []
        cell_pad = "padding:5px 3px;" if for_email else "padding:6px 5px;"
        td_style = f"vertical-align:top;{cell_pad}border-bottom:1px solid #eaecf0;word-break:break-word;"

        for t in top_themes:
            # 1. 题材名与热度排名 (Rank 徽章 + 名称 + 全景共振标 + 主力行为定性 + 热度分值)
            rank_val = t.get("rank")
            rank_badge = ""
            rank_plain = ""
            if isinstance(rank_val, int) and not isinstance(rank_val, bool):
                rank_plain = f"#{rank_val}"
                if rank_val == 1:
                    rank_badge = '<span style="display:inline-block;padding:0 3px;font-size:9px;font-weight:700;color:#d48806;background:#fffbe6;border:1px solid #ffe58f;border-radius:2px;margin-right:2px">#1</span>'
                elif rank_val == 2:
                    rank_badge = '<span style="display:inline-block;padding:0 3px;font-size:9px;font-weight:700;color:#1d39c4;background:#f0f5ff;border:1px solid #adc6ff;border-radius:2px;margin-right:2px">#2</span>'
                elif rank_val == 3:
                    rank_badge = '<span style="display:inline-block;padding:0 3px;font-size:9px;font-weight:700;color:#d4380d;background:#fff2e8;border:1px solid #ffbb96;border-radius:2px;margin-right:2px">#3</span>'
                else:
                    rank_badge = f'<span style="display:inline-block;padding:0 3px;font-size:9px;color:#595959;background:#f5f5f5;border-radius:2px;margin-right:2px">#{rank_val}</span>'

            t_name_raw = str(t.get("name") or t.get("plate_code") or "—")
            t_name = _esc(t_name_raw)
            is_resonance = t_name_raw in table1_plates if table1_plates else False
            res_badge = ' <span style="display:inline-block;padding:0 3px;font-size:9px;background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f;border-radius:2px;font-weight:700">①共振</span>' if is_resonance else ""
            res_plain = " [①全景共振]" if is_resonance else ""

            tag_name, tag_badge, tag_plain = _theme_behavior_tag(t)
            strength_val = t.get("strength")
            strength_html = f' <span style="font-size:9px;color:#8c8c8c">强度{_esc(strength_val)}</span>' if strength_val is not None else ""
            strength_plain = f"(强度{strength_val})" if strength_val is not None else ""
            cell_theme = f'{rank_badge}<b>{t_name}</b>{res_badge}<br>{tag_badge}{strength_html}'

            # 2. 成员数与红盘率 (成份股 + 红盘率)
            up_ratio_val = t.get("up_ratio")
            auction_cnt = t.get('auction_count', '—')
            member_cnt = t.get('member_count', '—')
            if isinstance(up_ratio_val, (int, float)):
                up_pct = up_ratio_val * 100
                up_color = "#cf1322" if up_pct >= 50 else ("#389e0d" if up_pct < 20 else "#595959")
                up_html = f'<br><span style="font-size:9px;font-weight:600;color:{up_color}">红盘{_fmt(up_pct, 1, "%")}</span>'
                up_plain_str = f"·红盘{_fmt(up_pct, 1, '%')}"
            else:
                up_html = ""
                up_plain_str = ""
            cell_members = f"{auction_cnt}/{member_cnt}{up_html}"
            members_plain = f"{auction_cnt}/{member_cnt}{up_plain_str}"

            # 3. 竞价额·占比 (大额金额加粗，大额占比高亮)
            amount_val = t.get("amount_yuan")
            t_amount = _money(amount_val)
            amount_is_big = isinstance(amount_val, (int, float)) and amount_val >= 1e8
            amount_html = f"<b>{t_amount}</b>" if amount_is_big else t_amount
            share_val = t.get("market_amount_share")
            t_share = _fmt(share_val * 100, 1, "%") if isinstance(share_val, (int, float)) else "—"
            share_is_big = isinstance(share_val, (int, float)) and share_val >= 0.05
            share_html = f'<span style="color:{"#1d39c4;font-weight:600" if share_is_big else "#667085"}">{t_share}</span>'
            cell_amount = f"{amount_html}<br>{share_html}"

            # 4. 金额加权涨幅 vs 算术均涨 (A股红涨绿跌，加权涨幅加粗反映主力资金方向)
            w_bp = t.get("weighted_mean_change_bp")
            m_bp = t.get("mean_change_bp")
            w_pct = w_bp / 100 if isinstance(w_bp, (int, float)) else None
            m_pct = m_bp / 100 if isinstance(m_bp, (int, float)) else None
            w_color = "#cf1322" if (w_pct or 0) > 0 else ("#389e0d" if (w_pct or 0) < 0 else "#667085")
            m_color = "#cf1322" if (m_pct or 0) > 0 else ("#389e0d" if (m_pct or 0) < 0 else "#667085")
            w_html = f'<span style="color:{w_color};font-weight:700">{_fmt(w_pct, 2, "%")}</span>' if w_pct is not None else "—"
            m_html = f'<span style="color:{m_color}">{_fmt(m_pct, 2, "%")}</span>' if m_pct is not None else "—"
            cell_mean = f'<span style="font-size:9px;color:#8c8c8c">加权</span> {w_html}<br><span style="font-size:9px;color:#8c8c8c">算术</span> {m_html}'
            mean_plain = f"加权{_fmt(w_pct, 2, '%')}·算术{_fmt(m_pct, 2, '%')}" if w_pct is not None else f"均涨{_fmt(m_pct, 2, '%')}"

            # 5. 多空强弱分布 (强>=+3% 红, 平 灰, 弱<=-3% 绿)
            strong_cnt = t.get("strong_count")
            mid_cnt = t.get("middle_count")
            weak_cnt = t.get("weak_count")
            if strong_cnt is not None or mid_cnt is not None or weak_cnt is not None:
                s_num = strong_cnt if strong_cnt is not None else 0
                m_num = mid_cnt if mid_cnt is not None else 0
                w_num = weak_cnt if weak_cnt is not None else 0
                cell_structure = (f'<span style="color:#cf1322;font-weight:600">{s_num}强</span> / '
                                  f'<span style="color:#8c8c8c">{m_num}平</span> / '
                                  f'<span style="color:#389e0d;font-weight:600">{w_num}弱</span>')
                struct_plain = f"{s_num}强/{m_num}平/{w_num}弱"
            else:
                cell_structure = '<span style="color:#bfbfbf">—</span>'
                struct_plain = "—"

            # 6. Top1 龙头集中度与涨跌停
            top1_sym = t.get("top1_symbol")
            top1_nm = t.get("top1_name") or top1_sym
            top1_sh = t.get("top1_share")
            top1_chg = t.get("top1_change_pct")
            up_cnt = t.get("limit_up_count", 0)
            down_cnt = t.get("limit_down_count", 0)
            up_badge = f'<span style="display:inline-block;padding:1px 3px;font-size:9px;background:#fff1f0;color:#cf1322;border:1px solid #ffa39e;border-radius:2px;font-weight:700">涨停{up_cnt}</span>' if up_cnt > 0 else ''
            down_badge = f' <span style="display:inline-block;padding:1px 3px;font-size:9px;background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f;border-radius:2px;font-weight:700">跌停{down_cnt}</span>' if down_cnt > 0 else ''
            limit_prefix = f"{up_badge}{down_badge}<br>" if (up_badge or down_badge) else ""

            if top1_nm:
                top1_chg_color = "#cf1322" if (top1_chg or 0) > 0 else ("#389e0d" if (top1_chg or 0) < 0 else "#667085")
                top1_chg_html = f' <span style="color:{top1_chg_color};font-size:9px">{_fmt(top1_chg, 2, "%")}</span>' if top1_chg is not None else ""
                top1_sh_str = _fmt(top1_sh * 100, 1, "%") if isinstance(top1_sh, (int, float)) else ""
                top1_sh_html = f'<br><span style="font-size:9px;color:#8c8c8c">Top1 {top1_sh_str}</span>' if top1_sh_str else ""
                cell_limits = f"{limit_prefix}<b>{_esc(top1_nm)}</b>{top1_chg_html}{top1_sh_html}"
                limits_plain = f"Top1:{top1_nm}({top1_sh_str})" if top1_sh_str else f"Top1:{top1_nm}"
            else:
                top3_val = t.get("amount_top3_share")
                top3_html = f'<br><span style="font-size:9px;color:#8c8c8c">Top3 {_fmt(top3_val * 100, 1, "%")}</span>' if isinstance(top3_val, (int, float)) else ""
                top3_plain = f"·Top3 {_fmt(top3_val * 100, 1, '%')}" if isinstance(top3_val, (int, float)) else ""
                cell_limits = f"{limit_prefix or '<span style=\"color:#bfbfbf\">-</span>'}{top3_html}"
                limits_plain = f"涨停{up_cnt}/跌停{down_cnt}{top3_plain}"

            cells = [cell_theme, cell_members, cell_amount, cell_mean, cell_structure, cell_limits]
            row_tds = "".join(f'<td style="{td_style}">{cell}</td>' for cell in cells)
            tb_rows.append(f"<tr>{row_tds}</tr>")

            rank_prefix = f"[{rank_plain}] " if rank_plain else ""
            tb_text_lines.append(f"{rank_prefix}{t_name_raw}{res_plain} {tag_plain}{strength_plain} 成员{members_plain}；额{t_amount}·占比{t_share}；{mean_plain}；分布:{struct_plain}；{limits_plain}")

        tb_head = ("热点题材(开盘竞价)", "成份股·红盘率", "竞价额·占比", "加权/算术均涨", "强/平/弱分布", "涨跌停·龙头集中度")
        font_sz = "10px" if for_email else "11px"
        tb_table = f'<table role="table" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;table-layout:fixed;font-size:{font_sz}"><thead><tr>' + ''.join(_th(label) for label in tb_head) + '</tr></thead><tbody>' + ''.join(tb_rows) + '</tbody></table>'

        # 挂载竞价极值封单多空双侧对照
        seals = tb.get("seals") or []
        seal_valid = [s for s in seals if isinstance(s, Mapping) and (s.get("seal_amount_yuan") or s.get("amount_yuan"))]
        up_seals = [s for s in seal_valid if s.get("side") == "limit_up"]
        down_seals = [s for s in seal_valid if s.get("side") == "limit_down"]
        up_seals.sort(key=lambda s: -(s.get("seal_amount_yuan") or s.get("amount_yuan") or 0))
        down_seals.sort(key=lambda s: -(s.get("seal_amount_yuan") or s.get("amount_yuan") or 0))
        top_up = up_seals[:3]
        top_down = down_seals[:3]

        seals_html = ""
        if top_up or top_down:
            def _format_seal_item(s: Mapping[str, Any]) -> tuple[str, str]:
                sym = s.get("symbol") or "—"
                s_name = s.get("name") or sym
                s_amt = _money(s.get("seal_amount_yuan") or s.get("amount_yuan"))
                assignment = s.get("assignment") if isinstance(s.get("assignment"), Mapping) else {}
                primary_theme = assignment.get("primary_theme") if isinstance(assignment.get("primary_theme"), Mapping) else None
                theme_name = primary_theme.get("name") if primary_theme else None
                if not theme_name and s.get("hot_plates") and isinstance(s["hot_plates"], list):
                    theme_name = s["hot_plates"][0].get("name")
                theme_tag = f' <span style="background:#e6f4ff;color:#0958d9;padding:0 3px;border-radius:2px;font-size:9px">{_esc(theme_name)}</span>' if theme_name else ""
                name_disp = f'<b>{_esc(s_name)}</b>' if s_name == sym else f'<b>{_esc(s_name)}</b><small style="color:#667085">({_esc(sym)})</small>'
                html_snippet = f'{name_disp} <b>{_esc(s_amt)}</b>{theme_tag}'
                theme_str = f"[{theme_name}]" if theme_name else ""
                plain_snippet = f"{s_name}({sym}) {s_amt}{theme_str}"
                return html_snippet, plain_snippet

            up_html_parts = [_format_seal_item(s)[0] for s in top_up]
            down_html_parts = [_format_seal_item(s)[0] for s in top_down]
            up_plain_parts = [_format_seal_item(s)[1] for s in top_up]
            down_plain_parts = [_format_seal_item(s)[1] for s in top_down]

            up_line_html = ('<span style="display:inline-block;padding:1px 4px;font-size:9px;background:#fff1f0;color:#cf1322;border:1px solid #ffa39e;border-radius:2px;font-weight:700">🔴买方涨停极值</span> '
                            + " ； ".join(up_html_parts)) if top_up else ""
            down_line_html = ('<span style="display:inline-block;padding:1px 4px;font-size:9px;background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f;border-radius:2px;font-weight:700">🟢卖方跌停核按钮</span> '
                              + " ； ".join(down_html_parts)) if top_down else ""

            seals_content = "<br>".join(p for p in (up_line_html, down_line_html) if p)
            seals_html = (f'<div style="margin-top:7px;padding:6px 8px;background:#f9fafb;border:1px solid #eaecf0;border-radius:4px;font-size:{font_sz};line-height:1.6">'
                          f'{seals_content}</div>')

            up_plain_str = "🔴买方涨停极值: " + "；".join(up_plain_parts) if top_up else ""
            down_plain_str = "🟢卖方跌停核按钮: " + "；".join(down_plain_parts) if top_down else ""
            plain_seals = " ｜ ".join(p for p in (up_plain_str, down_plain_str) if p)
            tb_text_lines.append(f"竞价多空极值封单对照：{plain_seals}")

        tb_html_block = f'<h2{h2_style}>③ 热点题材竞价分桶{subtitle_span}</h2>{tb_table}{seals_html}'
    elif tb.get("status"):
        msg_style = 'style="font-size:10px;color:#7c2d12"' if for_email else 'style="font-size:11px;color:#7c2d12"'
        reason_text = f" [{tb.get('reason')}]" if tb.get("reason") else ""
        tb_html_block = f'<h2{h2_style}>③ 热点题材竞价分桶{subtitle_span}</h2><p {msg_style}>热点题材分桶未就绪: {_esc(tb.get("status"))}{_esc(reason_text)}</p>'
        tb_text_lines.append(f"热点题材分桶未就绪({tb.get('status')}{reason_text})")
    return tb_html_block, tb_text_lines


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
    prh = product.get("plate_reference_health")
    if isinstance(prh, Mapping):
        prh_status = prh.get("status", "—")
        prh_cov = prh.get("coverage")
        cov_text = f"{prh_cov * 100:.1f}%" if isinstance(prh_cov, (int, float)) else "—"
        prh_n = prh.get("plate_count", "—")
        health_summary = f"板块映射:{prh_status}({prh_n}板块/覆盖{cov_text})"
        whole_batch += f"；{health_summary}"
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
    table1_names = {p.get("plate_name") for p in plate_rows if isinstance(p, Mapping) and p.get("plate_name")}
    tb_html_block, tb_text_lines = _render_theme_buckets_section(product.get("theme_buckets"), for_email=True, table1_plates=table1_names)

    html = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>@media(max-width:700px){table{font-size:9px!important}td{padding:3px!important}}</style></head>'
        '<body style="margin:0;padding:8px;color:#26364a;background:#eef2f6;font-family:Arial,sans-serif"><table role="presentation" width="100%"><tr><td align="center">'
        '<table role="presentation" width="100%" style="max-width:1100px;background:#fff;border-collapse:collapse"><tr><td style="padding:12px">'
        f'<h1 style="font-size:16px;margin:0 0 5px">竞价速览卡 · {_esc(date)} · {_esc(asof)} · {status}</h1>{replay_pill}{degraded_html}{cards_html}'
        f'<div style="padding:7px;background:#f4f7fb;margin:8px 0"><b>昨日整批统计 / 市场整体</b><br>{aggregate_html}</div>'
        f'<div style="margin:5px 0">{_esc(next((line.split(":",1)[1].strip() for line in lines if line.startswith("结论:")), "结论待核实"))}</div>'
        f'<div style="font-size:10px;color:#667085">{_esc(threshold)}</div><h2>① 题材横向对比 <span style="font-size:10px;font-weight:normal;color:#667085">[开盘啦全景板块]</span></h2>{plate_table}'
        f'<h2>② 昨日涨停×题材 接力意愿</h2>{bucket_table}{tb_html_block}'
        f'<h2>高标与封单极值</h2><p style="font-size:10px">高标（昨≥2板）</p><div>{highboard_html}</div><p>{market_extremes_html}</p>'
        '<p style="font-size:10px;color:#667085">完整明细已存档于 Core 报告目录；个股点名仅用于极值、封单和高标。缺失不补0。</p>'
        '</td></tr></table></td></tr></table></body></html>')
    extra = _streak_and_hot_conclusions(product)
    html = html.replace('</body>', '<div>' + '<br>'.join(_esc(line) for line in extra) + '</div></body>')
    plain_blocks = [f"09:26竞价速览 | {date} {asof} {('历史回放样本,非当时实时信号' if replay else '')}\n" + "\n".join(extra),
        f"昨日整批统计 / 市场整体：{whole_batch}",
        "①题材横向对比[开盘啦全景板块]：" + ("\n".join(plate_text) or "无可展示题材"),
        "②昨日涨停×题材接力意愿：" + ("\n".join(bucket_text) or "无可展示题材")]
    if tb_text_lines:
        plain_blocks.append("③热点题材竞价分桶[热榜成份股×盘前映射]：" + ("\n".join(tb_text_lines) or "无可展示题材"))
    plain_blocks.append("高标与封单极值：" + highboard_text + "；" + market_extreme_text + f"；{threshold}；完整明细已存档于 Core 报告目录")
    return html, "\n\n".join(plain_blocks)

def _render_ladder_theme_matrix(product: Mapping[str, Any], *, for_email: bool = False) -> tuple[str, list[str]]:
    from statistics import median
    from .auction_theme_buckets import normalize_theme_label

    rows = []
    for layer in product.get("yesterday_ladder", []):
        if not isinstance(layer, Mapping):
            continue
        level = layer.get("yesterday_streak")
        if not isinstance(level, int) or isinstance(level, bool) or level < 1:
            continue
        for bucket in layer.get("buckets", []):
            if not isinstance(bucket, Mapping):
                continue
            for stock in bucket.get("stocks", []):
                if isinstance(stock, Mapping):
                    rows.append((level, stock))
    symbols = {s.get("symbol") for _, s in rows if s.get("symbol")}
    tb = product.get("theme_buckets") or {}
    seals = [s for s in tb.get("seals", []) if isinstance(s, Mapping) and s.get("side") == "limit_up" and s.get("symbol") not in symbols] if isinstance(tb, Mapping) else []
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    def total(items, key):
        vals = [s.get(key) for _, s in items]
        return sum(vals) if vals and all(number(v) for v in vals) else None
    def known_total(items, key):
        vals = [s.get(key) for _, s in items if number(s.get(key))]
        return sum(vals) if vals else None
    def seal_total(items):
        relevant = [i for i in items if promoted(i)]
        value = known_total(relevant, 'seal_amount_yuan')
        missing = sum(not number(s.get('seal_amount_yuan')) for _, s in relevant)
        return _money(value) + (f'（{missing}只待核实）' if missing else '')
    def med(items):
        vals = [s['pct_change'] for _, s in items if s.get('valid_price', True) is True and number(s.get('pct_change'))]
        return median(vals) if vals else None
    def promoted(item):
        return item[0] == 0 or item[1].get('promotion_status') == 'PROVISIONAL'
    all_rows = rows + [(0, s) for s in seals]
    groups = {}
    for level, stock in all_rows:
        if level:
            label = stock.get('primary_reason') or ''
        else:
            assignment = stock.get('assignment') or {}
            primary = assignment.get('primary_theme') if isinstance(assignment, Mapping) else None
            plates = stock.get('hot_plates') or []
            label = (primary.get('name') if isinstance(primary, Mapping) else None) or (plates[0].get('name') if plates and isinstance(plates[0], Mapping) else '') or ''
        label = normalize_theme_label(label) or '未归类'
        groups.setdefault(label, []).append((level, stock))
    main, other = [], []
    for name, items in groups.items():
        (main if len(items) >= 2 or any(l >= 2 or promoted((l, s)) for l, s in items) else other).append((name, items))
    def sort_key(group):
        name, items = group
        return (-sum(promoted(i) for i in items), -max(l for l, _ in items), -(known_total(items, 'seal_amount_yuan') or 0), -(known_total(items, 'auction_amount_yuan') or 0), name)
    main.sort(key=sort_key)
    if other:
        main.append(('其他独立题材', [item for _, items in sorted(other) for item in items]))
    labels = ['更高(昨≥4板)', '拟4板(昨3板)', '拟3板(昨2板)', '拟2板(昨1板)', '拟首板(今日新涨停)']
    levels = [[i for i in rows if i[0] >= 4], [i for i in rows if i[0] == 3], [i for i in rows if i[0] == 2], [i for i in rows if i[0] == 1], [(0, s) for s in seals]]
    blocks, text = [], []
    for idx, (label, items) in enumerate(zip(labels, levels)):
        n = sum(promoted(i) for i in items)
        ratio = f'{n / len(items) * 100:.1f}%' if items else '—'
        progress = f'{n}/{len(items)} · 晋级率{ratio}' if idx < 4 else f'{n}只'
        gap = ' · 缺昨4板(断层)' if idx == 0 and any(l >= 5 for l, _ in rows) and not any(l == 4 for l, _ in rows) else ''
        if 0 < idx < 4 and not items and any(l > 4 - idx for l, _ in rows):
            gap = ' · 断层'
        line = f'{label} {progress}{gap} · 中位 {_fmt(med(items), 2, "%")} · 真实封单额 {seal_total(items)}'
        text.append(line)
        blocks.append(f'<div style="display:inline-block;vertical-align:top;width:19%;min-width:120px;box-sizing:border-box;padding:4px;overflow-wrap:anywhere"><b>{_esc(label)}</b><br><span style="display:inline-block;border-radius:50%;padding:7px;background:{"#c83b3b" if n else "#98a2b3"};color:white">{n}</span><br>{_esc(progress)}<br><span style="color:#c65d00">{_esc(gap)}</span><br>中位 {_pct_cell(med(items))}<br>真实封单额 {seal_total(items)}</div>')
    cards = []
    for name, items in main:
        n = sum(promoted(i) for i in items)
        highest = max(l for l, _ in items)
        badge = f'含昨{highest}板' if highest else '拟首板'
        header = f'{name} · {badge} · 拟晋级 {n}/{len(items)} ({n / len(items) * 100:.1f}%) · 中位涨幅 {_fmt(med(items), 2, "%")} · 竞价额 {_money(total(items, "auction_amount_yuan"))} · 封单额 {seal_total(items)}'
        text.append(header)
        card = [f'<section data-theme="{_esc(name)}" style="border:1px solid #d0d5dd;border-radius:6px;margin:8px 0;padding:8px;overflow-wrap:anywhere"><b>{_esc(name)}</b> · {_esc(badge)} · 拟晋级 {n}/{len(items)} ({n / len(items) * 100:.1f}%) · 中位涨幅 {_pct_cell(med(items))} · 竞价额 {_money(total(items, "auction_amount_yuan"))} · 封单额 {seal_total(items)}']
        for level in sorted({l for l, _ in items}, reverse=True):
            subset = [i for i in items if i[0] == level]
            title = f'昨{level}板' if level else '拟首板（今日新涨停）'
            card.append(f'<div style="margin-top:6px"><b>{_esc(title)}</b>')
            text.append(title)
            for flag, label in (('yes', '🔴 拟晋级' if level else '🔴 拟首板'), ('no', '⚪ 未封板/待核实')):
                selected = [i for i in subset if ('yes' if promoted(i) else 'no') == flag]
                if not selected:
                    continue
                selected.sort(key=lambda i: (-(i[1].get('auction_amount_yuan') if number(i[1].get('auction_amount_yuan')) else -1), str(i[1].get('symbol') or '')))
                card.append(f'<div>{label}：')
                def render_stock(item):
                    stock = item[1]
                    status = ' · 待核实' if stock.get('promotion_status') == 'UNVERIFIED' else ''
                    return f'{_esc(stock.get("name") or stock.get("symbol") or "—")}{status} {_pct_cell(stock.get("pct_change"))} · 封单{_esc(_money(stock.get("seal_amount_yuan")))} · 竞价额{_esc(_money(stock.get("auction_amount_yuan")))} · 额{_esc(_rank(stock.get("amount_rank")))}'
                visible = selected[:3] if level == 1 and flag != 'yes' else selected
                hidden = selected[3:] if level == 1 and flag != 'yes' else []
                card.append('<br>'.join(render_stock(i) for i in visible))
                if hidden:
                    m = med(hidden)
                    mid = f'{m:+.2f}%' if m is not None else '—'
                    card.append(f'<details><summary>展开其余 {len(hidden)} 只（中位 {mid}）</summary>' + '<br>'.join(render_stock(i) for i in hidden) + '</details>')
                card.append('</div>')
                text.append(label + '：' + '；'.join(_stock_text(s) for _, s in selected))
            card.append('</div>')
        card.append('</section>')
        cards.append(''.join(card))
    return '<table role="table" style="width:100%;table-layout:fixed"><tr><td>' + ''.join(blocks) + ''.join(cards) + '</td></tr></table>', text


def _render_style_and_capital_flow(product: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    flow = product.get("capital_flow")
    historical = not isinstance(flow, Mapping)
    if historical:
        stocks: dict[str, dict[str, Any]] = {}
        pool: set[str] = set()
        def add(row: Mapping[str, Any], **extra: Any) -> None:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                return
            dest = stocks.setdefault(symbol, {})
            for key, value in {**extra, **row}.items():
                if value is not None and (key not in dest or dest[key] is None):
                    dest[key] = value
        for plate in product.get("plates", []):
            for row in plate.get("stocks", []):
                add(row, primary_theme=plate.get("plate_name"))
        for layer in product.get("yesterday_ladder", []):
            for bucket in layer.get("buckets", []):
                for row in bucket.get("stocks", []):
                    add(row, primary_theme=row.get("primary_reason") or bucket.get("reason"), yesterday_streak=layer.get("yesterday_streak"))
                    pool.add(str(row.get("symbol") or ""))
        for row in (product.get("theme_buckets") or {}).get("seals", []):
            assignment = row.get("assignment") or {}
            theme = assignment.get("primary_theme") or {}
            add(row, primary_theme=theme.get("name"), seal_side="up" if row.get("side") == "limit_up" else None)
        ordered = sorted((r for r in stocks.values() if isinstance(r.get("auction_amount_yuan"), (int, float)) and r["auction_amount_yuan"] > 0),
                         key=lambda r: (-r["auction_amount_yuan"], r["symbol"]))
        rush = [r for r in ordered if isinstance(r.get("pct_change"), (int, float)) and r["pct_change"] >= 5
                and (r.get("at_official_limit") is False or r.get("seal_status") == "NOT_LIMIT") and r.get("seal_side") != "up"
                and r.get("promotion_status") != "PROVISIONAL" and r.get("seal_status") not in ("VERIFIED", "VERIFIED_REPLAY")]
        nuclear = sorted((r for r in stocks.values() if r.get("symbol") in pool and isinstance(r.get("pct_change"), (int, float)) and r["pct_change"] < 0), key=lambda r: (r["pct_change"], r["symbol"]))
        flow = {"top_amount": ordered[:10], "rush_buy": rush[:8], "pool_nuclear": nuclear[:5], "style_preference": {}}
    style = flow.get("style_preference") or {}
    share = style.get("top20_amount_share")
    ladder_changes = [s.get("pct_change") for layer in product.get("yesterday_ladder", []) if (layer.get("yesterday_streak") or 0) >= 2
                      for bucket in layer.get("buckets", []) for s in bucket.get("stocks", [])
                      if s.get("valid_price") is True and isinstance(s.get("pct_change"), (int, float))]
    market_mean = (product.get("overview") or {}).get("market_mean_pct")
    premium = sum(ladder_changes) / len(ladder_changes) - market_mean if ladder_changes and isinstance(market_mean, (int, float)) else None
    premium_text = f"{premium:+.2f}pp" if premium is not None else "—"
    bar = (f"大票吸金 Top20占比 {_fmt(share * 100 if isinstance(share, (int, float)) else None, 1, '%')} · "
           f"亿元大单 {style.get('big_ticket_1e_n') if style.get('big_ticket_1e_n') is not None else '—'}只 · "
           f"20cm高开≥5% {style.get('chinext_star_strong_n') if style.get('chinext_star_strong_n') is not None else '—'}只 · 连板溢价 {premium_text}")
    bar_html = f'<div style="margin:5px 4px;padding:8px;background:#eef5ff;color:#20466c;font-size:12px">{_esc(bar)} <span style="color:#667085">（昨≥2板均幅减市场均幅）</span></div>'
    lines = [bar + "（昨≥2板均幅减市场均幅）", "④ 资金去向雷达" + ("（历史报告已收录个股，非全市场）" if historical else "")]
    sections = []
    for key, title in (("top_amount", "🔥 大资金主战场（竞价额 Top10）"), ("rush_buy", "🚀 竞价强抢筹（高开≥5%非一字 Top8）"), ("pool_nuclear", "⚠️ 昨涨停负反馈 / 核按钮 Top5")):
        lines.append(title)
        rows_html = []
        for row in flow.get(key, []):
            name = row.get("name") or row.get("symbol") or "—"
            theme = row.get("primary_theme") or row.get("primary_reason") or "—"
            streak = row.get("yesterday_streak")
            tags = (f"昨{streak:g}板" if isinstance(streak, (int, float)) else "—")
            if key == "rush_buy" and str(row.get("symbol", "")).startswith(("30", "688")):
                tags = "20cm · " + tags
            if key == "top_amount" and row.get("seal_amount_yuan") is not None:
                tags = "封" + _money(row.get("seal_amount_yuan")) + " · " + tags
            rank = _rank_cell(row.get("amount_rank")) + " " if key == "top_amount" else ""
            line = f"{rank}{name} [{theme}] {_fmt(row.get('pct_change'), 2, '%')} {_money(row.get('auction_amount_yuan'))} {tags}"
            lines.append(line)
            rows_html.append(f'<tr><td style="padding:3px;border-bottom:1px solid #eee">{_esc(rank + str(name))}<br><small>{_esc(theme)} · {_esc(tags)}</small></td><td>{_pct_cell(row.get("pct_change"))}</td><td>{_esc(_money(row.get("auction_amount_yuan")))}</td></tr>')
        sections.append(f'<h3 style="font-size:13px;margin:12px 0 4px">{_esc(title)}</h3><table style="width:100%;font-size:12px;border-collapse:collapse">{"".join(rows_html) or "<tr><td>—</td></tr>"}</table>')
    note = "（历史报告已收录个股，非全市场）" if historical else ""
    radar = f'<h2 style="font-size:16px;margin:18px 0 7px">④ 资金去向雷达 <small>{note}</small></h2>' + "".join(sections)
    return bar_html, radar, lines


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
             ("昨日涨停高开", high_open + "（按竞价价>昨收）"), ("竞价双侧封单合计", seal_text)]
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
    summaries = [pool_summary, bias_summary, feedback_summary, *_streak_and_hot_conclusions(product)]
    prh = product.get("plate_reference_health")
    if isinstance(prh, Mapping):
        prh_status = prh.get("status", "—")
        prh_cov = prh.get("coverage")
        cov_text = f"{prh_cov * 100:.1f}%" if isinstance(prh_cov, (int, float)) else "—"
        prh_n = prh.get("plate_count", "—")
        summaries.append(f"板块映射：{prh_status} · 板块 {prh_n} 个 · 覆盖率 {cov_text}")
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

    ladder_table, ladder_text = _render_ladder_theme_matrix(product)
    table1_names = {p.get("plate_name") for p in plates if isinstance(p, Mapping) and p.get("plate_name")}
    tb_html_block, tb_text_lines = _render_theme_buckets_section(product.get("theme_buckets"), for_email=False, table1_plates=table1_names)
    style_bar, radar_html, capital_text = _render_style_and_capital_flow(product)
    cards_html += style_bar
    tb_text_section = ("\n\n③ 热点题材竞价分桶 [热榜成份股×盘前映射]\n" + "\n".join(tb_text_lines)) if tb_text_lines else ""
    text = (f"竞价全景 V6 | {product.get('trade_date', '日期待核实')} | {product.get('asof', '时点待核实')}\n"
        + " | ".join(f"{label}：{value}" for label, value in cards) + f"\n规则一句话：{rule_sentence}\n" + "\n".join(summaries) + f"\n{hot_note}\n{hot_coverage_note}\n\n① 板块竞价全景 [开盘啦全景板块]\n"
        + "\n\n".join(plate_text) + "\n\n② 昨日连板梯队×涨停原因\n" + "\n\n".join(ladder_text)
        + tb_text_section + "\n\n" + "\n".join(capital_text)
        + "\n\n缺失字段留白或待核实；名次为全市场口径。")
    document = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>竞价全景 V6</title></head><body style="margin:0;padding:10px;background:#eef2f6;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#26364a">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center"><table role="presentation" width="100%" style="max-width:760px;background:#fff">'
        f'<tr><td style="padding:12px 14px;background:#172b43;color:#fff"><div style="font-size:16px;font-weight:700">竞价全景 V6　·　{_esc(product.get("trade_date", "日期待核实"))}　·　{_esc(product.get("asof", "时点待核实"))}　·　{_esc(overview.get("status", "PARTIAL"))}</div></td></tr>'
        f'<tr><td style="padding:10px">{cards_html}<div style="margin:6px 0;font-size:11px">{"<br>".join(_esc(line) for line in summaries)}</div><div style="margin:6px 0;color:#7a4b00;font-size:11px">{_esc(hot_note)}</div>'
        f'<h2 style="font-size:16px;margin:18px 0 7px">① 板块竞价全景 <span style="font-size:11px;font-weight:normal;color:#667085">[开盘啦全景板块]</span></h2>{plate_table}'
        f'<h2 style="font-size:16px;margin:18px 0 7px">② 昨日连板梯队×涨停原因</h2>{ladder_table}'
        f'{tb_html_block}{radar_html}'
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
