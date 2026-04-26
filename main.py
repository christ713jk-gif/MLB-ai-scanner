import re
import os
import uvicorn
from typing import Optional, List, Union
from fastapi import FastAPI, Request
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
            return f * 100.0  # 小數形式 0.74 → 74.0 (完美接住 n8n 的 0.6)
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
# 2. 數據模型 — 欄位名稱完全對齊內部需求
# ──────────────────────────────────────────

class SingleMatch(BaseModel):
    match_str:       str               = Field(...,   alias="Match")

    # ── ML 賠率 ──────────────────────────
    ml_open_str:     str               = Field("N/A", alias="ML_Open")
    ml_close_str:    str               = Field("N/A", alias="ML_Close")

    # ── 跑壘線 (Spread / FG) ──────────────
    # 🌟 直接對齊 Google Sheets 與 n8n 的 Spread_Open / Spread_Close
    rl_open_str:     Union[str, float] = Field("N/A", alias="Spread_Open")
    rl_close_str:    Union[str, float] = Field("N/A", alias="Spread_Close")

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
    raw_tkt_ml = parse_pct(m.tkt_ml)
    raw_mon_ml = parse_pct(m.mon_ml)

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
            "Type":      "ML",
            "Target":    f"{new_rl_fav} ML",
            "Tier":      "Experimental",
            "Rule":      "B2-MLB: RL-Flip-Confirm",
            "Expected_WR": "100.0%"
        })
        fired = True

    # B1: Fav-Steam
    if not fired and fav_move <= FAV_MOVE_B1 and (fav_mon or 0) >= FAV_MON_B1:
        signals.append({
            "Type":      "ML",
            "Target":    f"{fav} ML",
            "Tier":      "Emerging",
            "Rule":      "B1-MLB: Fav-Steam",
            "Expected_WR": "80.0%"
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
        "Match":       m.match_str,
        "Status":      "ACTIVE" if signals else "PASS",
        "signals":     signals,
        "Diagnostics": diagnostics,
    }

# ──────────────────────────────────────────
# 5. FastAPI 路由 (自動正規化攔截器)
# ──────────────────────────────────────────

app = FastAPI(title="MLB AI Scanner V0.2 (Cloud Run Edition)")

def normalize_payload(raw_dict: dict) -> dict:
    """動態攔截並轉換 n8n 傳來的後綴欄位名稱，讓引擎能讀取"""
    new_dict = raw_dict.copy()
    key_mapping = {
        "Spread_Ticket": "Ticket_Spread_G",
        "Spread_Money":  "Money_Spread_G",
        "Total_Ticket":  "Ticket_Total_Over",
        "Total_Money":   "Money_Total_Over",
        "ML_Ticket":     "Ticket_ML_G",
        "ML_Money":      "Money_ML_G"
    }
    for k, v in raw_dict.items():
        for prefix, standard_key in key_mapping.items():
            if k.startswith(prefix):
                new_dict[standard_key] = v
    return new_dict

@app.get("/")
def home():
    return {"status": "Online", "version": "MLB-ML-V0.2 Cloud Run Active"}

@app.post("/scan")
@app.post("/api/v1/scan")
async def scan_endpoint(request: Request):
    payload = await request.json()
    
    # 支援單筆 dict 或多筆 list 陣列
    raw_list = payload.get("matches", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_list, list):
        raw_list = [raw_list]

    results = []
    for raw_match in raw_list:
        normalized_data = normalize_payload(raw_match)
        try:
            m = SingleMatch(**normalized_data)
            results.append(analyze_match(m))
        except Exception as e:
            results.append({
                "Match": raw_match.get("Match", "Unknown"),
                "Status": "ERROR",
                "Diagnostics": f"Data parsing error: {str(e)}"
            })

    # 將輸出格式對齊 n8n 戰報 Code 節點的要求
    return [{
        "version": "MLB-Scanner-V0.2",
        "results": results
    }]

# ──────────────────────────────────────────
# 6. Cloud Run 啟動設定
# ──────────────────────────────────────────

if __name__ == "__main__":
    # 將 Port 修改為 Cloud Run 預設的 8080
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)