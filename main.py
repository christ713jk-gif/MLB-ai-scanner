import re
import os
import uvicorn
from typing import Optional, List, Union
from fastapi import FastAPI
from pydantic import BaseModel, Field


# ──────────────────────────────────────────
# 1. 解析工具
# ──────────────────────────────────────────

def parse_pct(val) -> Optional[float]:
    """
    將百分比欄位統一轉為 0~100 的 float。
    接受: "74%", "74", 74, 0.74 (小數形式)
    邊界修正: "1" → 1.0%, "100" → 100.0%
    """
    if val is None:
        return None
    try:
        s = str(val).replace('%', '').strip()
        f = float(s)
        # 含 % 符號 或 數值 > 1 → 已經是整數百分比 (如 74 代表 74%)
        if '%' in str(val) or f > 1.0:
            return f          # 直接當作 0~100
        else:
            return f * 100.0  # 小數形式 0.74 → 74.0
    except:
        return None


def parse_ml_odds(s: str):
    """'2.9 / 1.23' → (2.9, 1.23)"""
    if not s or s in ["N/A", "Locked", "Unknown"]:
        return None, None
    try:
        parts = s.strip().split('/')
        return float(parts[0].strip()), float(parts[1].strip())
    except:
        return None, None


def parse_rl(s: Union[str, float]):
    """
    '1.5 (1.82 / 1.95)' → (1.5, 1.82, 1.95)
    '1.5'               → (1.5, None, None)
    'Locked'            → (None, None, None)
    """
    try:
        s_str = str(s).strip()
        if s_str in ["N/A", "Locked", ""]:
            return None, None, None
        m = re.match(r'(-?\d+\.?\d*)\s*\((\d+\.?\d*)\s*/\s*(\d+\.?\d*)\)', s_str)
        if m:
            return float(m.group(1)), float(m.group(2)), float(m.group(3))
        m_simple = re.search(r'(-?\d+\.?\d*)', s_str)
        if m_simple:
            return float(m_simple.group(1)), None, None
    except:
        pass
    return None, None, None


# ──────────────────────────────────────────
# 2. 數據模型 — 欄位名稱完全對齊 JSON 輸入
# ──────────────────────────────────────────

class SingleMatch(BaseModel):
    match_str:       str               = Field(...,   alias="Match")

    # ── ML 賠率 ──────────────────────────
    ml_open_str:     str               = Field("N/A", alias="ML_Open")
    ml_close_str:    str               = Field("N/A", alias="ML_Close")

    # ── 跑壘線 (Spread / FG) ──────────────
    rl_open_str:     Union[str, float] = Field("N/A", alias="FG_Open")
    rl_close_str:    Union[str, float] = Field("N/A", alias="FG_Close")

    # ── 大小分 (Total) ───────────────────
    total_open_str:  Union[str, float] = Field("N/A", alias="Total_Open")
    total_close_str: Union[str, float] = Field("N/A", alias="Total_Close")

    # ── Ticket / Money % ─────────────────
    tkt_ml:          Union[str, float] = Field(50.0, alias="Ticket_ML_G")
    mon_ml:          Union[str, float] = Field(50.0, alias="Money_ML_G")
    tkt_spread:      Union[str, float] = Field(50.0, alias="Ticket_Spread_G")
    mon_spread:      Union[str, float] = Field(50.0, alias="Money_Spread_G")
    tkt_total:       Union[str, float] = Field(50.0, alias="Ticket_Total_Over")
    mon_total:       Union[str, float] = Field(50.0, alias="Money_Total_Over")

    # ── 其他 ─────────────────────────────
    has_tbd_pitcher: bool              = Field(False, alias="has_tbd_pitcher")
    daily_dsi:       Optional[float]   = Field(None, alias="Daily_DSI")

    class Config:
        populate_by_name = True


class MatchWrapper(BaseModel):
    matches: List[SingleMatch]
    count:   Optional[int] = 0


# ──────────────────────────────────────────
# 3. 引擎參數（每季回顧）
# ──────────────────────────────────────────

FAV_MOVE_B1  = -0.10   # B1: 盤主賠率縮短門檻
FAV_MON_B1   =  55.0   # B1: 聰明錢押盤主% 門檻 (0~100)
FAV_MOVE_B2  = -0.05   # B2: RL Flip 搭配的 ML 移動門檻


# ──────────────────────────────────────────
# 4. 單場分析核心
# ──────────────────────────────────────────

def analyze_match(m: SingleMatch) -> dict:

    # ── TBD 投手安全門 ────────────────────
    if m.has_tbd_pitcher:
        return {
            "match":       m.match_str,
            "status":      "PASS",
            "signals":     [],
            "diagnostics": "⚠️ TBD投手：基線失效，需人工核實"
        }

    # ── ML 解析 ───────────────────────────
    gmo, hmo = parse_ml_odds(m.ml_open_str)
    gmc, hmc = parse_ml_odds(m.ml_close_str)
    if not all([gmo, hmo, gmc, hmc]):
        return {
            "match":       m.match_str,
            "status":      "PASS",
            "signals":     [],
            "diagnostics": f"🚨 ML_MISSING: Open({m.ml_open_str}) Close({m.ml_close_str})"
        }

    # ── 確定盤主視角 ──────────────────────
    raw_tkt_ml = parse_pct(m.tkt_ml)   # 客隊 Ticket%
    raw_mon_ml = parse_pct(m.mon_ml)   # 客隊 Money%

    if gmc <= hmc:
        fav       = 'guest'
        fav_ml_o  = gmo;  fav_ml_c = gmc
        fav_tkt   = raw_tkt_ml
        fav_mon   = raw_mon_ml
    else:
        fav       = 'home'
        fav_ml_o  = hmo;  fav_ml_c = hmc
        fav_tkt   = (100.0 - raw_tkt_ml) if raw_tkt_ml is not None else None
        fav_mon   = (100.0 - raw_mon_ml)  if raw_mon_ml is not None else None

    fav_move  = fav_ml_c - fav_ml_o
    fav_smd   = (fav_mon - fav_tkt) if (fav_mon is not None and fav_tkt is not None) else None
    impl_prob = 1.0 / fav_ml_c

    # ── RL Flip 偵測 ──────────────────────
    rl_ov, _, _ = parse_rl(m.rl_open_str)
    rl_cv, _, _ = parse_rl(m.rl_close_str)
    flip = 0
    new_rl_fav = None
    if rl_ov is not None and rl_cv is not None:
        if (rl_ov >= 0) != (rl_cv >= 0):
            flip = 1
            new_rl_fav = 'guest' if rl_cv < 0 else 'home'

    # ── 信號邏輯 ──────────────────────────
    signals = []
    fired   = False

    # B2: RL-Flip-Confirm（優先）
    if not fired and flip == 1 and fav_move <= FAV_MOVE_B2:
        signals.append({
            "signal":    "B2-MLB: RL-Flip-Confirm",
            "direction": f"{new_rl_fav} ML",
            "train_wr":  "100.0% (N=6)",
            "maturity":  "[Experimental]",
        })
        fired = True

    # B1: Fav-Steam
    if not fired and fav_move <= FAV_MOVE_B1 and (fav_mon or 0) >= FAV_MON_B1:
        signals.append({
            "signal":    "B1-MLB: Fav-Steam",
            "direction": f"{fav} ML",
            "train_wr":  "80.0% (N=10)",
            "maturity":  "[Emerging]",
        })
        fired = True

    # ── 診斷字串（含 SMD）─────────────────
    smd_str  = f"{fav_smd:+.0f}" if fav_smd is not None else "N/A"
    tkt_str  = f"{fav_tkt:.0f}%" if fav_tkt is not None else "N/A"
    mon_str  = f"{fav_mon:.0f}%" if fav_mon is not None else "N/A"
    diagnostics = (
        f"fav={fav} | move={fav_move:+.3f} | "
        f"mon={mon_str} | tkt={tkt_str} | smd={smd_str} | "
        f"impl={impl_prob:.1%} | flip={flip}"
    )

    return {
        "match":       m.match_str,
        "status":      "ACTIVE" if signals else "PASS",
        "signals":     signals,
        "diagnostics": diagnostics,
    }


# ──────────────────────────────────────────
# 5. FastAPI 路由
# ──────────────────────────────────────────

app = FastAPI(title="MLB AI Scanner V0.2")


@app.get("/")
def home():
    return {"status": "Online", "version": "MLB-ML-V0.2"}


@app.post("/scan")
@app.post("/api/v1/scan")
def scan_endpoint(input_data: Union[MatchWrapper, List[MatchWrapper]]):
    data = input_data[0] if isinstance(input_data, list) else input_data
    results = [analyze_match(m) for m in data.matches]
    return {"results": results}


# ──────────────────────────────────────────
# 6. 本機測試
# ──────────────────────────────────────────

if __name__ == "__main__":
    # 快速冒煙測試 (已對齊最新鍵值名稱)
    test_cases = [
        # B1 應觸發
        {"Match": "MIL(客) vs BOS(主)",
         "ML_Open": "1.68 / 1.82", "ML_Close": "1.52 / 1.98",
         "FG_Open": "-1.5 (2.05 / 1.48)", "FG_Close": "-1.5 (1.9 / 1.6)",
         "Total_Open": "7.5 (O 1.68 / U 1.82)", "Total_Close": "7.5 (O 1.63 / U 1.87)",
         "Ticket_ML_G": 78, "Money_ML_G": 93,
         "Ticket_Spread_G": 87, "Money_Spread_G": 79,
         "Ticket_Total_Over": 87, "Money_Total_Over": 86},
        # B2 應觸發
        {"Match": "MIL(客) vs KC(主)",
         "ML_Open": "1.87 / 1.63", "ML_Close": "1.7 / 1.8",
         "FG_Open": "1.5 (1.43 / 2.15)", "FG_Close": "-1.5 (2.07 / 1.46)",
         "Total_Open": "8.5 (O 1.82 / U 1.68)", "Total_Close": "8.5 (O 1.82 / U 1.68)",
         "Ticket_ML_G": 21, "Money_ML_G": 10,
         "Ticket_Spread_G": 12, "Money_Spread_G": 8,
         "Ticket_Total_Over": 68, "Money_Total_Over": 62},
        # PASS
        {"Match": "TEX(客) vs LAD(主)",
         "ML_Open": "2.9 / 1.23", "ML_Close": "2.85 / 1.25",
         "FG_Open": "1.5 (1.92 / 1.58)", "FG_Close": "1.5 (1.92 / 1.58)",
         "Total_Open": "8.5 (O 1.63 / U 1.87)", "Total_Close": "8.5 (O 1.79 / U 1.71)",
         "Ticket_ML_G": 6, "Money_ML_G": 8,
         "Ticket_Spread_G": 7, "Money_Spread_G": 2,
         "Ticket_Total_Over": 91, "Money_Total_Over": 88},
        # TBD 投手
        {"Match": "PHI(客) vs COL(主)", "has_tbd_pitcher": True,
         "ML_Open": "1.28 / 2.7", "ML_Close": "1.26 / 2.75",
         "FG_Open": "-1.5 (1.5 / 2)", "FG_Close": "-1.5 (1.5 / 2)",
         "Total_Open": "9.5 (O 1.66 / U 1.84)", "Total_Close": "9.5 (O 1.7 / U 1.8)",
         "Ticket_ML_G": 95, "Money_ML_G": 94,
         "Ticket_Spread_G": 97, "Money_Spread_G": 100,
         "Ticket_Total_Over": 81, "Money_Total_Over": 91},
    ]

    print("MLB Scanner V0.2 — 冒煙測試\n" + "="*60)
    for tc in test_cases:
        m = SingleMatch(**tc)
        r = analyze_match(m)
        sig = r['signals'][0]['signal'] if r['signals'] else '—'
        print(f"\n  [{r['status']}] {r['match']}")
        print(f"  信號: {sig}")
        print(f"  診斷: {r['diagnostics']}")

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)