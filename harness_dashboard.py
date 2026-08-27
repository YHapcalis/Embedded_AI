"""
harness_dashboard.py — Harness 硬件信号监控面板

启动:
    streamlit run harness_dashboard.py

依赖:
    pip install streamlit plotly pandas
"""

import json
from pathlib import Path
from datetime import datetime

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

# ── 页面配置 ───────────────────────────────────────────────────

st.set_page_config(
    page_title="Harness 硬件监控面板",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 样式 ──────────────────────────────────────────────────────

st.markdown("""
<style>
    /* ── 全局黑夜背景 ── */
    .stApp, .main > div { background: #0e1117 !important; color: #e0e0e0; }
    .main > div { padding: 1rem 1.5rem; }
    .block-container { background: #0e1117; }
    p, li, .stMarkdown, .stMarkdown p { color: #c0c4cc; }

    /* ── 标题栏 ── */
    .app-title {
        font-size: 1.8rem; font-weight: 700;
        color: #e0e0e0; padding: 0.5rem 0;
        border-bottom: 2px solid #4CAF50; margin-bottom: 1.5rem;
    }

    /* ── 指标卡片 ── */
    .metric-card {
        background: #1a1d27; border-radius: 10px;
        padding: 1.2rem 1.5rem;
        border-left: 4px solid #4CAF50;
        margin-bottom: 0.5rem;
    }
    .metric-card.warn { border-left-color: #FF9800; }
    .metric-card.fail { border-left-color: #f44336; }

    .metric-label { font-size: 0.85rem; color: #888; margin-bottom: 0.3rem; }
    .metric-value { font-size: 1.6rem; font-weight: 700; color: #e0e0e0; }
    .metric-sub { font-size: 0.8rem; color: #555; margin-top: 0.2rem; }

    /* ── 区块标题 ── */
    .section-title {
        font-size: 1.2rem; font-weight: 600;
        color: #e0e0e0; margin: 1.5rem 0 1rem 0;
        padding-left: 0.8rem; border-left: 3px solid #4CAF50;
    }

    /* ── 表格 ── */
    .stDataFrame { background: #1a1d27; }
    .stDataFrame td, .stDataFrame th {
        background: #1a1d27 !important; color: #c0c4cc !important;
        border-color: #2a2d37 !important;
    }
    .stDataFrame thead tr th {
        background: #22252f !important; color: #e0e0e0 !important;
    }

    /* ── 侧栏 ── */
    section[data-testid="stSidebar"] {
        background: #0a0c12 !important;
    }
    section[data-testid="stSidebar"] .stMarkdown p {
        color: #c0c4cc;
    }
    section[data-testid="stSidebar"] .stSelectbox label {
        color: #888;
    }
    section[data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] > div {
        background: #1a1d27; border-color: #2a2d37;
    }

    /* ── 展开器 ── */
    .streamlit-expanderHeader {
        background: #1a1d27 !important; color: #c0c4cc !important;
        border-radius: 6px;
    }
    .streamlit-expanderContent {
        background: #1a1d27 !important;
        border-radius: 0 0 6px 6px;
    }

    /* ── info 框 ── */
    .stAlert {
        background: #1a1d27 !important; color: #c0c4cc !important;
        border: 1px solid #2a2d37;
    }

    /* ── 进度条 ── */
    .stProgress > div > div > div > div {
        background: #4CAF50;
    }
</style>
""", unsafe_allow_html=True)

# ── 数据加载 ───────────────────────────────────────────────────

TRACE_DIR = Path(__file__).parent / "traces"


def load_sessions() -> list[dict]:
    """加载 traces/ 下所有会话记录"""
    if not TRACE_DIR.exists():
        return []

    sessions = {}
    for f in sorted(TRACE_DIR.glob("*.jsonl"), reverse=True):
        try:
            lines = f.read_text(encoding="utf-8").strip().split("\n")
            records = [json.loads(l) for l in lines if l.strip()]
            if not records:
                continue

            first, last = records[0], records[-1]

            total, passed = 0, 0
            build_ok = flash_ok = True
            for r in records:
                if r.get("type") == "observation":
                    for c in r.get("checks", []):
                        total += 1
                        if c.get("passed"):
                            passed += 1
                    b = r.get("build", {})
                    if not b.get("success", True):
                        build_ok = False
                    f_data = r.get("flash", {})
                    if not f_data.get("success", True):
                        flash_ok = False

            sid = first.get("session_id", f.stem)
            sessions[sid] = {
                "id": sid,
                "file": f.name,
                "time": datetime.fromtimestamp(
                    first.get("timestamp", 0)
                ).strftime("%m-%d %H:%M:%S"),
                "records": len(records),
                "checks": f"{passed}/{total}",
                "pass_rate": passed / max(total, 1),
                "build_ok": build_ok,
                "flash_ok": flash_ok,
                "scenario": first.get("scenario", ""),
                "data": records,
            }
        except (json.JSONDecodeError, KeyError):
            continue

    return list(sessions.values())


def extract_charts(session: dict) -> dict:
    """从会话记录中提取绘图数据"""
    result = {
        "samples": {},    # {变量: [{round, min, max, count}]}
        "checks": [],     # [{round, var, passed, expected, actual}]
        "rounds": [],     # [{round, goal, reasoning, changes}]
    }

    rn = 0
    for r in session["data"]:
        if r.get("type") == "observation":
            rn += 1
            # 采样
            for var, info in r.get("samples", {}).items():
                result["samples"].setdefault(var, []).append({
                    "round": rn,
                    "min": info.get("min"),
                    "max": info.get("max"),
                    "count": info.get("count", 0),
                })
            # 断言
            for c in r.get("checks", []):
                result["checks"].append({
                    "round": rn,
                    "var": c.get("var", "?"),
                    "passed": c.get("passed", False),
                    "expected": c.get("expected", ""),
                    "actual": c.get("actual", ""),
                })
        elif r.get("type") == "decision":
            result["rounds"].append({
                "round": r.get("round", 0),
                "goal": r.get("goal_achieved", False),
                "reasoning": r.get("reasoning", "")[:200],
                "changes": len(r.get("code_changes", [])),
            })

    return result


def var_display_name(raw: str) -> str:
    """变量名 → 中文显示名"""
    names = {
        "g_can_sensor.valid": "CAN 数据有效",
        "g_can_sensor.temperature": "温度",
        "g_can_sensor.humidity": "湿度",
        "g_can_sensor.knob": "旋钮值",
        "g_can_sensor.tick": "CAN 时间戳",
        "g_can_sensor.key_event": "按键事件",
        "uwTick": "系统时钟",
        "GPIOA_ODR": "GPIOA 输出",
        "hcan1.ErrorCode": "CAN 错误码",
        "hcan1.State": "CAN 状态",
    }
    return names.get(raw, raw)


def var_unit(raw: str) -> str:
    """变量单位"""
    units = {
        "g_can_sensor.temperature": "°C",
        "g_can_sensor.humidity": "%",
        "g_can_sensor.knob": "",
        "g_can_sensor.tick": "tick",
        "uwTick": "tick",
    }
    return units.get(raw, "")


# ── 侧边栏 ────────────────────────────────────────────────────

st.sidebar.markdown("""
<div style="padding:1rem 0; text-align:center;">
    <div style="font-size:2.5rem; margin-bottom:0.3rem;"></div>
    <div style="font-size:1.1rem; font-weight:700; color:white;">硬件监控面板</div>
    <div style="font-size:0.75rem; color:#888;">Harness Dashboard</div>
</div>
""", unsafe_allow_html=True)

sessions = load_sessions()

if not sessions:
    st.warning("没有找到运行记录。")
    st.info(f"请先运行: `embed_harness.py --export-dashboard`")
    st.info(f"数据目录: `{TRACE_DIR}`")
    st.stop()

opts = {
    f"{s['time']} | {s['scenario'][:25]} | {s['checks']} 通过": s["id"]
    for s in sessions
}
sel_label = st.sidebar.selectbox("选择运行记录", list(opts.keys()), index=0)
sel_id = opts[sel_label]
sel = next(s for s in sessions if s["id"] == sel_id)
chart = extract_charts(sel)

st.sidebar.markdown("---")
st.sidebar.markdown(f"<div style='color:#aaa; font-size:0.8rem;'>文件</div>", unsafe_allow_html=True)
st.sidebar.markdown(f"<div style='color:white; font-size:0.85rem; word-break:break-all;'>{sel['file']}</div>", unsafe_allow_html=True)
st.sidebar.markdown(f"<div style='color:#aaa; font-size:0.8rem; margin-top:0.5rem;'>时间</div>", unsafe_allow_html=True)
st.sidebar.markdown(f"<div style='color:white;'>{sel['time']}</div>", unsafe_allow_html=True)

# ── 主面板 ────────────────────────────────────────────────────

st.markdown('<div class="app-title"> Harness 硬件监控面板</div>', unsafe_allow_html=True)

# ── 概览指标行 ──

c1, c2, c3, c4 = st.columns(4)
for col, label, value, sub, css in [
    (c1, "断言通过率", sel["checks"], f"{sel['pass_rate']*100:.0f}%", ""),
    (c2, "编译状态", "通过" if sel["build_ok"] else "失败",
     "", "ok" if sel["build_ok"] else "fail"),
    (c3, "烧录状态", "通过" if sel["flash_ok"] else "失败",
     "", "ok" if sel["flash_ok"] else "fail"),
    (c4, "记录条数", str(sel["records"]), "", ""),
]:
    with col:
        extra = f" {css}" if css else ""
        badge = ("✅" if "通过" in value else "❌") if css else ""
        st.markdown(
            f'<div class="metric-card{extra}">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="metric-value">{badge} {value}</div>'
            f'<div class="metric-sub">{sub}</div>'
            f'</div>', unsafe_allow_html=True)

# ── 场景信息 ──

if sel["scenario"]:
    st.info(f"  **场景**: {sel['scenario']}")

# ── 采样波形 ──

if chart["samples"]:
    st.markdown('<div class="section-title"> 采样数据波形</div>', unsafe_allow_html=True)

    for var, pts in chart["samples"].items():
        fig = go.Figure()

        rounds = [p["round"] for p in pts]
        mins = [p["min"] for p in pts]
        maxs = [p["max"] for p in pts]
        avgs = [(p["min"] + p["max"]) / 2
                for p in pts if p["min"] is not None and p["max"] is not None]

        vr = [r for r, a in zip(rounds, avgs) if a is not None]
        va = [a for a in avgs if a is not None]
        vmin = [m for m, a in zip(mins, avgs) if a is not None]
        vmax = [m for m, a in zip(maxs, avgs) if a is not None]

        if not vr:
            continue

        unit = var_unit(var)
        dname = var_display_name(var)

        fig.add_trace(go.Scatter(
            x=vr, y=va, mode="lines+markers",
            name=f"均值{unit}",
            line=dict(color="#4CAF50", width=2.5),
            marker=dict(size=6, color="#4CAF50"),
        ))
        fig.add_trace(go.Scatter(
            x=vr, y=vmin, mode="lines",
            name="最小值", showlegend=False,
            line=dict(color="#2196F3", width=1, dash="dot"),
        ))
        fig.add_trace(go.Scatter(
            x=vr, y=vmax, mode="lines",
            name="最大值", showlegend=False,
            line=dict(color="#FF5722", width=1, dash="dot"),
        ))

        # 填充区间
        fig.add_trace(go.Scatter(
            x=vr + vr[::-1], y=vmax + vmin[::-1],
            fill="toself", fillcolor="rgba(76,175,80,0.08)",
            line=dict(width=0), showlegend=False,
        ))

        fig.update_layout(
            title=dict(
                text=f"{dname}  {unit}",
                font=dict(size=14, color="#c0c4cc"),
            ),
            xaxis=dict(title="迭代轮次", dtick=1,
                       tickfont=dict(color="#888"),
                       title_font=dict(color="#888")),
            yaxis=dict(title=unit if unit else "数值",
                       tickfont=dict(color="#888"),
                       title_font=dict(color="#888")),
            height=220,
            margin=dict(l=40, r=20, t=40, b=30),
            paper_bgcolor="#0e1117",
            plot_bgcolor="#0e1117",
            hovermode="x unified",
            font=dict(color="#c0c4cc"),
            legend=dict(font=dict(color="#888")),
        )
        fig.update_xaxes(gridcolor="#1a1d27", zerolinecolor="#2a2d37")
        fig.update_yaxes(gridcolor="#1a1d27", zerolinecolor="#2a2d37")

        st.plotly_chart(fig, width='stretch')

# ── 断言检查 ──

if chart["checks"]:
    st.markdown('<div class="section-title"> 断言检查结果</div>', unsafe_allow_html=True)

    df = pd.DataFrame(chart["checks"])
    df["passed"] = df["passed"].map({True: "✅ 通过", False: "❌ 失败"})
    df["var"] = df["var"].map(var_display_name)

    st.dataframe(
        df,
        column_config={
            "round": st.column_config.NumberColumn("轮次", width=60),
            "var": st.column_config.TextColumn("变量", width=180),
            "passed": st.column_config.TextColumn("结果", width=100),
            "expected": st.column_config.TextColumn("期望值", width=150),
            "actual": st.column_config.TextColumn("实际值", width=150),
        },
        width='stretch',
        hide_index=True,
    )

    # 通过率进度条
    passed_count = sum(1 for c in chart["checks"] if c["passed"])
    total_count = len(chart["checks"])
    rate = passed_count / max(total_count, 1)
    st.progress(rate, text=f"{passed_count}/{total_count} 项通过 ({rate*100:.0f}%)")

# ── AI 决策记录 ──

if chart["rounds"]:
    st.markdown('<div class="section-title"> AI 决策记录</div>', unsafe_allow_html=True)
    for r in chart["rounds"]:
        icon = "✅" if r["goal"] else "🔄"
        with st.expander(f"第 {r['round']} 轮 {icon} — {r['reasoning'][:60]}"):
            cols = st.columns([3, 1])
            cols[0].markdown(f"**分析**:\n{r['reasoning']}")
            cols[1].markdown(f"**文件修改**: {r['changes']} 个")

# ── 原始 JSON ──

with st.expander(" 原始数据 (JSON)"):
    st.json(sel["data"])
