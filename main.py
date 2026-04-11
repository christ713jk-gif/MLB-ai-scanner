import re
import os
import uvicorn
from typing import Optional, List, Union
from fastapi import FastAPI
from pydantic import BaseModel, Field

# ──────────────────────────────────────────
# 1. 解析工具 (支援水位格式解析)
# ──────────────────────────────────────────

def parse_numeric(val):
    if val is None: return 0.0
    try:
        f = float(str(val).replace('%', '').strip())
        return f / 100.0 if (f > 1.0 or '%' in str(val)) else f
    except: return 0.5

def parse_ml_odds(s: str):
    if not s or s in ["N/A", "Locked"]: return None, None
    try:
        parts = s.strip().split('/')
        return float(parts[0].strip()), float(parts[1].strip())
    except: return None, None

def parse_rl(s: Union[str, float]):
    """解析 '1.5 (1.82 / 1.95)' 格式"""
    try:
        s_str = str(s).strip()
        if s_str in ["N/A", "Locked"]: return None, None, None
        # 匹配讓分值與括號水位
        m = re.match(r'(-?\d+\.?\d*)\s*\((\d+\.?\d*)\s*/\s*(\d+\.?\d*)\)', s_str)
        if m:
            return float(m.group(1)), float(m.group(2)), float(m.group(3))
        # 若只有純數字
        m_simple = re.match(r'(-?\d+\.?\d*)', s_str)
        if m_simple:
            return float(m_simple.group(1)), None, None
    except: pass
    return None, None, None

# ──────────────────────────────────────────
# 2. 數據模型與路由
# ──────────────────────────────────────────

class SingleMatch(BaseModel):
    match_str: str = Field(..., alias="Match")
    ml_open_str: str = Field("N/A", alias="ML_Open")
    ml_close_str: str = Field("N/A", alias="ML_Close")
    rl_open_str: str = Field("N/A", alias="FG_Open")
    rl_close_str: str = Field("N/A", alias="FG_Close")
    ml_money_g: float = Field(50.0, alias="Money_ML_G")
    has_tbd_pitcher: bool = Field(False, alias="has_tbd_pitcher")

class MatchWrapper(BaseModel):
    matches: List[SingleMatch]

app = FastAPI()

@app.post("/scan")
def scan_endpoint(input_data: Union[MatchWrapper, List[MatchWrapper]]):
    data = input_data[0] if isinstance(input_data, list) else input_data
    results = []
    
    for m in data.matches:
        if m.has_tbd_pitcher:
            results.append({"match": m.match_str, "status": "PASS", "diagnostics": "⚠️ TBD"})
            continue
            
        # 解析
        gmo, hmo = parse_ml_odds(m.ml_open_str)
        gmc, hmc = parse_ml_odds(m.ml_close_str)
        sp_ov, _, _ = parse_rl(m.rl_open_str)
        sp_cv, _, _ = parse_rl(m.rl_close_str)
        mon = parse_numeric(m.ml_money_g)

        if not all([gmo, hmo, gmc, hmc]):
            results.append({"match": m.match_str, "status": "PASS", "diagnostics": "🚨 ML MISSING"})
            continue

        # 判定盤主
        if gmc <= hmc:
            fav, move, fav_mon = 'guest', (gmc - gmo), mon
        else:
            fav, move, fav_mon = 'home', (hmc - hmo), (1.0 - mon)

        # 判定翻轉
        flip = 1 if (sp_ov is not None and sp_cv is not None and (sp_ov >= 0) != (sp_cv >= 0)) else 0
        
        signals = []
        if flip == 1 and move <= -0.05:
            signals.append({"signal": "B2-MLB: RL-Flip-Confirm", "direction": f"{fav} ML"})
        elif move <= -0.10 and fav_mon >= 0.55:
            signals.append({"signal": "B1-MLB: Fav-Steam", "direction": f"{fav} ML"})

        results.append({
            "match": m.match_str,
            "status": "ACTIVE" if signals else "PASS",
            "signals": signals,
            "diagnostics": f"fav={fav} | move={move:+.2f} | mon={fav_mon:.1%} | flip={flip}"
        })
        
    return {"results": results}

@app.get("/")
def home(): return {"status": "Online"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)