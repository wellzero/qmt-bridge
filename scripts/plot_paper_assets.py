#!/usr/bin/env python3
"""绘制模拟盘各账户全交易日总资产走势（自包含 HTML 小倍数图）。

用法::

    python scripts/plot_paper_assets.py [--data-dir data/paper_trading] [--out output/paper_assets.html]

数据口径
--------
- 读取 ``<data_dir>/<account>/order/orders_YYYYMMDD.csv``；
- 跨日重写的委托按 ``(order_id, order_time, stock_code)`` 去重、保留最早文件中的行
  （与 dashboard ``data_loader.load_all_orders`` 口径一致）；
- 每个交易日取最后一笔成交委托（``traded_volume > 0``，无成交则取最后一行）的
  ``account_cash + account_market_value`` 作为当日期末总资产；
- 无委托日沿用前值（持仓不重新估值），曲线起点为 ``config.json`` 的 ``initial_cash``；
- 无任何委托的账户不绘制曲线，仅在表格中列出。

仅依赖标准库，可独立运行。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from html import escape
from pathlib import Path

# ── 画布几何（viewBox 单位） ──
W, H = 240.0, 132.0
PAD_L, PAD_R, PAD_T, PAD_B = 5.0, 5.0, 8.0, 18.0
PLOT_W, PLOT_H = W - PAD_L - PAD_R, H - PAD_T - PAD_B


# ── 数据提取 ────────────────────────────────────────────────

def load_account(data_dir: Path, account_id: str) -> dict | None:
    """读取某账户全部委托 CSV，返回期末总资产序列等信息。"""
    orders_dir = data_dir / account_id / "order"
    if not orders_dir.exists():
        return None
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for path in sorted(orders_dir.glob("orders_*.csv")):
        date_str = path.stem.removeprefix("orders_")
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = (row.get("order_id", ""), row.get("order_time", ""),
                           row.get("stock_code", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    row["trade_date"] = date_str
                    rows.append(row)
        except OSError:
            print(f"警告: 读取失败 {path}", file=sys.stderr)
    if not rows:
        return None

    eod: dict[str, float] = {}
    n_filled = 0
    cur_date: str | None = None
    last_filled: dict | None = None
    last_row: dict | None = None
    for row in rows:  # rows 已按文件日期 + 追加顺序排列
        date = row["trade_date"]
        if date != cur_date:  # 进入新交易日，落盘上一日
            if cur_date is not None:
                src = last_filled or last_row
                eod[cur_date] = _asset(src)
            cur_date, last_filled = date, None
        try:
            traded = float(row.get("traded_volume") or 0)
        except ValueError:
            traded = 0.0
        if traded > 0:
            n_filled += 1
            last_filled = row
        last_row = row
    if cur_date is not None:
        src = last_filled or last_row
        eod[cur_date] = _asset(src)
    if not eod:
        return None
    return {"account_id": account_id, "eod": eod, "n_filled": n_filled}


def _asset(row: dict) -> float:
    try:
        cash = float(row.get("account_cash") or 0)
    except ValueError:
        cash = 0.0
    try:
        mv = float(row.get("account_market_value") or 0)
    except ValueError:
        mv = 0.0
    return cash + mv


def load_initial_cash(data_dir: Path, account_id: str) -> float:
    """初始资金: config.json -> summary.json -> 默认 10 万。"""
    for loader in (_from_config, _from_summary):
        value = loader(data_dir, account_id)
        if value:
            return value
    return 100000.0


def _from_config(data_dir: Path, account_id: str) -> float:
    path = data_dir / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return float(cfg.get(account_id, {}).get("initial_cash") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0.0


def _from_summary(data_dir: Path, account_id: str) -> float:
    path = data_dir / account_id / "summary" / "summary.json"
    try:
        return float(json.loads(path.read_text(encoding="utf-8")).get("initial_cash") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0.0


def summary_total(data_dir: Path, account_id: str) -> float | None:
    path = data_dir / account_id / "summary" / "summary.json"
    try:
        return float(json.loads(path.read_text(encoding="utf-8")).get("total_asset"))
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return None


# ── 序列构建 ────────────────────────────────────────────────

def build_series(accounts: list[dict], dates: list[str]) -> list[dict]:
    """在全体交易日轴上前向填充，得到每账户 v(i) 序列与渲染元数据。"""
    n = len(dates)
    for acct in accounts:
        eod = acct["eod"]
        first = min(dates.index(d) for d in eod)
        last = max(dates.index(d) for d in eod)
        vals: list[float | None] = [None] * n
        carry = acct["initial"]
        for i in range(first, n):
            d = dates[i]
            if d in eod:
                carry = eod[d]
            vals[i] = carry
        final = vals[n - 1] or acct["initial"]
        lo = min(acct["initial"], *[v for v in vals if v is not None])
        hi = max(acct["initial"], *[v for v in vals if v is not None])
        if hi == lo:
            hi, lo = lo + 1.0, lo - 1.0
        pad = (hi - lo) * 0.08
        acct.update(
            first=first, last=last, vals=vals, final=final,
            ret=(final - acct["initial"]) / acct["initial"],
            ylo=lo - pad, yhi=hi + pad,
        )
    return sorted(accounts, key=lambda a: a["ret"], reverse=True)


# ── SVG 渲染 ────────────────────────────────────────────────

def x_at(i: int, n: int) -> float:
    return PAD_L + PLOT_W * i / (n - 1) if n > 1 else PAD_L + PLOT_W / 2


def y_at(v: float, acct: dict) -> float:
    return PAD_T + PLOT_H * (1 - (v - acct["ylo"]) / (acct["yhi"] - acct["ylo"]))


def render_spark(acct: dict, dates: list[str]) -> str:
    """单账户面板 SVG：初始资金参考线 + 总资产折线 + 端点圆点 + 首末日期。"""
    n = len(dates)
    first = acct["first"]
    pts = [(x_at(first, n), y_at(acct["initial"], acct))]
    for i in range(first, n):
        pts.append((x_at(i, n), y_at(acct["vals"][i], acct)))
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    y_init = y_at(acct["initial"], acct)
    ex, ey = pts[-1]
    lab_l, lab_r = _mdy(dates[0]), _mdy(dates[-1])
    return (
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none" '
        f'aria-hidden="true">'
        f'<line class="refline" x1="{PAD_L}" y1="{y_init:.1f}" '
        f'x2="{W - PAD_R}" y2="{y_init:.1f}"/>'
        f'<path class="spark" d="{path}"/>'
        f'<circle class="enddot" cx="{ex:.1f}" cy="{ey:.1f}" r="3"/>'
        f'<line class="xhair" x1="0" y1="{PAD_T}" x2="0" '
        f'y2="{PAD_T + PLOT_H}" visibility="hidden"/>'
        f'<circle class="hoverdot" r="3.5" visibility="hidden"/>'
        f'<text class="xlab" x="{PAD_L}" y="{H - 5}">{lab_l}</text>'
        f'<text class="xlab" x="{W - PAD_R}" y="{H - 5}" text-anchor="end">{lab_r}</text>'
        f"</svg>"
    )


def _mdy(yyyymmdd: str) -> str:
    return f"{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


# ── 格式化 ──────────────────────────────────────────────────

def fmt_wan(v: float) -> str:
    return f"{v / 10000:.2f}万"


def fmt_yuan(v: float) -> str:
    return f"¥{v:,.0f}"


def fmt_pct(r: float) -> str:
    sign = "+" if r >= 0 else "−"
    return f"{sign}{abs(r) * 100:.2f}%"


def short_name(account_id: str) -> str:
    return account_id.removesuffix("_paper") or account_id


# ── HTML 组装 ───────────────────────────────────────────────

CSS = """
:root{color-scheme:light;
  --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#898781;
  --grid:#e1e0d9;--baseline:#c3c2b7;--border:rgba(11,11,11,.10);
  --pos:#2a78d6;--neg:#e34948;}
@media (prefers-color-scheme:dark){
:root{color-scheme:dark;
  --page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink-2:#c3c2b7;--muted:#898781;
  --grid:#2c2c2a;--baseline:#383835;--border:rgba(255,255,255,.10);
  --pos:#3987e5;--neg:#e66767;}}
*{box-sizing:border-box}
body{margin:0;padding:24px 20px 40px;font:14px/1.5 system-ui,-apple-system,
  "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--ink);
  background:var(--page)}
.wrap{max-width:1440px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--ink-2);font-size:13px;margin:0 0 4px;max-width:900px}
.sub .gen{color:var(--muted)}
.legend{display:flex;gap:18px;margin:10px 0 18px;font-size:12.5px;color:var(--ink-2)}
.legend .sw{display:inline-block;width:16px;height:2px;border-radius:1px;
  vertical-align:middle;margin-right:6px}
.legend .sw.pos{background:var(--pos)}.legend .sw.neg{background:var(--neg)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:12px;margin:0 0 22px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:12px 16px}
.t-label{font-size:12px;color:var(--muted);margin-bottom:4px}
.t-value{font-size:22px;font-weight:650}
.t-sub{font-size:12px;color:var(--ink-2);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(238px,1fr));
  gap:12px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:10px 12px 8px;outline:none}
.panel:focus-visible{box-shadow:0 0 0 2px var(--pos)}
.p-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.p-name{font-size:12.5px;font-weight:600;color:var(--ink-2);overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  word-break:break-all}
.p-ret{font-size:14px;font-weight:650;white-space:nowrap;font-variant-numeric:tabular-nums}
.panel svg{display:block;width:100%;height:auto;margin-top:4px;overflow:visible}
.refline{stroke:var(--baseline);stroke-width:1}
.panel[data-tone="pos"] .spark{stroke:var(--pos)}
.panel[data-tone="neg"] .spark{stroke:var(--neg)}
.spark{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.panel[data-tone="pos"] .enddot,.panel[data-tone="pos"] .hoverdot{fill:var(--pos)}
.panel[data-tone="neg"] .enddot,.panel[data-tone="neg"] .hoverdot{fill:var(--neg)}
.enddot{stroke:var(--surface);stroke-width:2}
.xhair{stroke:var(--muted);stroke-width:1}
.hoverdot{stroke:var(--surface);stroke-width:2}
.xlab{fill:var(--muted);font-size:8.5px;font-family:inherit}
.p-foot{display:flex;justify-content:space-between;font-size:11.5px;
  color:var(--muted);font-variant-numeric:tabular-nums}
.p-foot .final{color:var(--ink-2);font-weight:600}
.bar{display:flex;justify-content:flex-end;margin:18px 0 10px}
button{font:inherit;font-size:13px;color:var(--ink-2);background:var(--surface);
  border:1px solid var(--border);border-radius:6px;padding:5px 14px;cursor:pointer}
button:hover{color:var(--ink)}
.tablewrap{overflow-x:auto;background:var(--surface);
  border:1px solid var(--border);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 14px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{color:var(--muted);font-weight:600;border-bottom:1px solid var(--grid);
  font-size:12px}
tbody tr+tr td{border-top:1px solid var(--grid)}
td.num{font-variant-numeric:tabular-nums}
td.ret{font-weight:650}
.hidden{display:none}
#tip{position:fixed;z-index:10;pointer-events:none;background:var(--surface);
  border:1px solid var(--border);border-radius:6px;padding:8px 11px;
  box-shadow:0 4px 14px rgba(0,0,0,.14);min-width:120px}
#tip .d{font-size:11px;color:var(--muted)}
#tip .v{font-size:16px;font-weight:650;margin:2px 0}
#tip .r{font-size:12px;color:var(--ink-2)}
"""

JS = """
(function(){
"use strict";
var DATA=JSON.parse(document.getElementById("viz-data").textContent);
var dates=DATA.dates,N=dates.length;
var PAD_L=%%PAD_L%%,PLOT_W=%%PLOT_W%%,W=%%W%%,H=%%H%%,PAD_T=%%PAD_T%%,PLOT_H=%%PLOT_H%%;
var tip=document.getElementById("tip");
function show(panel,i,cx,cy){
  var id=panel.dataset.acct,d=DATA.panels[id];
  var v=d.vals[i];if(v===null){hide();return;}
  while(tip.firstChild)tip.removeChild(tip.firstChild);
  var e1=document.createElement("div");e1.className="d";
  e1.textContent=dates[i].slice(0,4)+"-"+dates[i].slice(4,6)+"-"+dates[i].slice(6);
  var e2=document.createElement("div");e2.className="v";
  e2.textContent="\\u00a5"+v.toLocaleString("zh-CN",{maximumFractionDigits:0});
  var e3=document.createElement("div");e3.className="r";
  var r=(v-d.initial)/d.initial*100;
  e3.textContent="\\u8f83\\u521d\\u59cb "+(r>=0?"+":"\\u2212")+
    Math.abs(r).toFixed(2)+"%";
  tip.appendChild(e1);tip.appendChild(e2);tip.appendChild(e3);
  tip.style.display="block";
  var tw=tip.offsetWidth,th=tip.offsetHeight;
  var x=cx+14,y=cy-th/2;
  if(x+tw>innerWidth-8)x=cx-tw-12;
  y=Math.max(8,Math.min(innerHeight-th-8,y));
  tip.style.left=x+"px";tip.style.top=y+"px";
  // 十字线与悬浮点
  var svg=panel.querySelector("svg");
  var rect=svg.getBoundingClientRect();
  var sx=W/rect.width,sy=H/rect.height;
  var vx=PAD_L+PLOT_W*i/(N-1);
  var vy=PAD_T+PLOT_H*(1-(v-d.ylo)/(d.yhi-d.ylo));
  var xh=svg.querySelector(".xhair"),hd=svg.querySelector(".hoverdot");
  xh.setAttribute("x1",vx);xh.setAttribute("x2",vx);xh.removeAttribute("visibility");
  hd.setAttribute("cx",vx);hd.setAttribute("cy",vy);hd.removeAttribute("visibility");
}
function hide(){
  tip.style.display="none";
  var p=hide.panel;
  if(p){p.querySelector(".xhair").setAttribute("visibility","hidden");
    p.querySelector(".hoverdot").setAttribute("visibility","hidden");hide.panel=null;}
}
function idx(e,panel){
  var svg=panel.querySelector("svg"),rect=svg.getBoundingClientRect();
  var vx=(e.clientX-rect.left)*W/rect.width;
  var i=Math.round((vx-PAD_L)/PLOT_W*(N-1));
  return Math.max(0,Math.min(N-1,i));
}
document.querySelectorAll(".panel").forEach(function(panel){
  var cur=-1;
  panel.addEventListener("pointermove",function(e){
    var i=idx(e,panel);cur=i;hide.panel=panel;show(panel,i,e.clientX,e.clientY);});
  panel.addEventListener("pointerleave",hide);
  panel.addEventListener("focus",function(){
    var d=DATA.panels[panel.dataset.acct];
    var i=cur>=0?cur:(d.vals[N-1]!==null?N-1:d.first);
    cur=i;hide.panel=panel;
    var r=panel.getBoundingClientRect();
    show(panel,i,r.left+r.width*0.7,r.top+r.height/2);});
  panel.addEventListener("blur",function(){hide();});
  panel.addEventListener("keydown",function(e){
    if(e.key!=="ArrowLeft"&&e.key!=="ArrowRight")return;
    e.preventDefault();
    var d=DATA.panels[panel.dataset.acct];
    var i=Math.max(d.first,Math.min(N-1,(cur<0?N-1:cur)+(e.key==="ArrowLeft"?-1:1)));
    cur=i;hide.panel=panel;
    var r=panel.getBoundingClientRect();
    show(panel,i,r.left+r.width*0.7,r.top+r.height/2);});
});
var btn=document.getElementById("tabletoggle"),tw=document.getElementById("tablewrap");
btn.addEventListener("click",function(){
  var on=tw.classList.toggle("hidden");
  btn.textContent=on?"\\u663e\\u793a\\u8868\\u683c":"\\u9690\\u85cf\\u8868\\u683c";});
})();
""".replace("%%PAD_L%%", str(PAD_L)).replace("%%PAD_R%%", str(PAD_R)) \
   .replace("%%PLOT_W%%", str(PLOT_W)).replace("%%W%%", str(W)) \
   .replace("%%H%%", str(H)).replace("%%PAD_T%%", str(PAD_T)) \
   .replace("%%PLOT_H%%", str(PLOT_H))


def build_html(accts: list[dict], idle: list[dict], dates: list[str]) -> str:
    n = len(dates)
    total = sum(a["final"] for a in accts)
    avg_ret = sum(a["ret"] for a in accts) / len(accts) if accts else 0.0
    best = accts[0] if accts else None

    panels_html = []
    viz_panels = {}
    for a in accts:
        tone = "pos" if a["ret"] >= 0 else "neg"
        name = escape(short_name(a["account_id"]))
        panels_html.append(
            f'<div class="panel" data-tone="{tone}" data-acct="{escape(a["account_id"])}" '
            f'tabindex="0" role="img" aria-label="{name} 期末总资产 '
            f'{a["final"]:,.0f} 元，收益率 {fmt_pct(a["ret"])}">'
            f'<div class="p-head"><span class="p-name" '
            f'title="{escape(a["account_id"])}">{name}</span>'
            f'<span class="p-ret">{fmt_pct(a["ret"])}</span></div>'
            f"{render_spark(a, dates)}"
            f'<div class="p-foot"><span class="final">{fmt_yuan(a["final"])}</span>'
            f'<span>成交 {a["n_filled"]} 笔</span></div></div>'
        )
        viz_panels[a["account_id"]] = {
            "initial": a["initial"], "vals": [round(v, 2) if v is not None else None
                                              for v in a["vals"]],
            "ylo": round(a["ylo"], 2), "yhi": round(a["yhi"], 2), "first": a["first"],
        }

    rows = []
    for a in accts + idle:
        ret_cell = (f'<td class="num ret">{fmt_pct(a["ret"])}</td>' if "ret" in a
                    else '<td class="num">—</td>')
        final_cell = (f'<td class="num">{fmt_yuan(a["final"])}</td>' if "final" in a
                      else f'<td class="num">{fmt_yuan(a["initial"])}</td>')
        dates_cells = (f'<td class="num">{_mdy(dates[a["first"]])}~{_mdy(dates[a["last"]])}</td>'
                       if "first" in a else '<td class="num">未交易</td>')
        rows.append(
            f'<tr><td>{escape(short_name(a["account_id"]))}</td>'
            f'<td class="num">{a.get("n_filled", 0)}</td>{dates_cells}{final_cell}{ret_cell}</tr>'
        )

    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    span = f"{dates[0][:4]}-{_mdy(dates[0])} ~ {dates[-1][:4]}-{_mdy(dates[-1])}（{n} 个交易日）"
    viz_data = json.dumps({"dates": dates, "panels": viz_panels}, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>模拟盘账户总资产 · 全交易日</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<h1>模拟盘账户总资产 · 全交易日走势</h1>
<p class="sub">{escape(span)}。期末值取当日最后一笔成交委托的
账户现金 + 市值；无委托日沿用前值（持仓不重新估值）。{len(idle)} 个账户无委托记录，仅在表格中列出。
<span class="gen">生成于 {gen} · scripts/plot_paper_assets.py</span></p>
<div class="legend"><span><i class="sw pos"></i>期末收益 ≥ 0</span>
<span><i class="sw neg"></i>期末收益 &lt; 0</span></div>
<div class="tiles">
<div class="tile"><div class="t-label">交易账户</div>
<div class="t-value">{len(accts)}<span class="t-sub"> / {len(accts) + len(idle)}</span></div></div>
<div class="tile"><div class="t-label">期末总资产合计</div>
<div class="t-value">{fmt_wan(total)}</div></div>
<div class="tile"><div class="t-label">平均收益率</div>
<div class="t-value">{fmt_pct(avg_ret)}</div></div>
<div class="tile"><div class="t-label">最佳账户</div>
<div class="t-value">{fmt_pct(best["ret"]) if best else "—"}</div>
<div class="t-sub">{escape(short_name(best["account_id"])) if best else ""}</div></div>
</div>
<div class="grid">{"".join(panels_html)}</div>
<div class="bar"><button id="tabletoggle">显示表格</button></div>
<div class="tablewrap hidden" id="tablewrap">
<table><thead><tr><th>账户</th><th>成交笔数</th><th>交易区间</th>
<th>期末总资产</th><th>收益率</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
</div>
</div>
<div id="tip" style="display:none"></div>
<script type="application/json" id="viz-data">{viz_data}</script>
<script>{JS}</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data/paper_trading",
                    help="模拟交易数据目录 (默认: data/paper_trading)")
    ap.add_argument("--out", default="output/paper_assets.html",
                    help="输出 HTML 路径 (默认: output/paper_assets.html)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        ap.error(f"数据目录不存在: {data_dir}")

    config_accts = set()
    try:
        config_accts = set(json.loads(
            (data_dir / "config.json").read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    dir_accts = sorted(
        d.name for d in data_dir.iterdir()
        if d.is_dir() and d.name != "prices" and _ACCOUNT_ID_RE.match(d.name))
    all_ids = sorted(config_accts & set(dir_accts) or set(dir_accts))

    accounts: list[dict] = []
    idle: list[dict] = []
    for account_id in all_ids:
        info = load_account(data_dir, account_id)
        if info is None:
            idle.append({"account_id": account_id,
                         "initial": load_initial_cash(data_dir, account_id)})
            continue
        info["initial"] = load_initial_cash(data_dir, account_id)
        accounts.append(info)

    dates = sorted({d for a in accounts for d in a["eod"]})
    if not dates:
        ap.error("未找到任何委托记录")
    accounts = build_series(accounts, dates)

    # 与引擎 summary.json 交叉核对（差异通常来自引擎对持仓的实时重估）
    for a in accounts:
        st = summary_total(data_dir, a["account_id"])
        if st and abs(st - a["final"]) / a["initial"] > 0.02:
            print(f"提示: {a['account_id']} 曲线期末 {a['final']:,.0f} 与 "
                  f"summary.json {st:,.0f} 差异 >2%（持仓重估所致）", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(accounts, idle, dates), encoding="utf-8")
    print(f"已生成 {out}（{len(accounts)} 个交易账户，{len(idle)} 个未交易，"
          f"{dates[0]}~{dates[-1]} 共 {len(dates)} 个交易日）")


_ACCOUNT_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_-]+$")

if __name__ == "__main__":
    main()
