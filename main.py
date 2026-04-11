import re
from typing import Optional, List, Union
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ──────────────────────────────────────────
# 1. 解析工具 (完全保留您的原始邏輯)
# ──────────────────────────────────────────

def parse_ml_odds(s: str):
    try:
        parts = s.strip().split('/')
        return float(parts[0].strip()), float(parts[1].strip())
    except:
        return None, None

def parse_rl(s: Union[str, float]):
    # 支援純數字 1.5 或字串 "1.5 (1.62 / 1.88)"
    if isinstance(s, (int, float)):
        return float(s), None, None
    try:
        m = re.match(r'(-?\d+\.?\d*)\s*\((\d+\.?\d*)\s*/\s*(\d+\.?\d*)\)', str(s).strip())
        if m:
            return float(m.group(1)), float(m.group(2)), float(m.group(3))
        return float(s), None, None
    except:
        return None, None, None

# ──────────────────────────────────────────
# 2. 數據模型 (精準對接您的 JSON 欄位)
# ──────────────────────────────────────────

class SingleMatch(BaseModel):
    # 使用 alias 將 JSON 欄位映射到引擎參數
    match: str = Field(..., alias="Match")
    
    # 您的 JSON 中目前缺少的 ML 賠率欄位 (給予預設值 N/A 觸發引擎的 DATA_MISSING)
    ml_open_str: str = Field("N/A", alias="ML_Open")
    ml_close_str: str = Field("N/A", alias="ML_Close")
    
    # 讓分欄位 (對應您的 FG_Open / FG_Close)
    rl_open_str: Union[str, float] = Field("N/A", alias="FG_Open")
    rl_close_str: Union[str, float] = Field("N/A", alias="FG_Close")
    
    # 資金比例 (對應您的 Ticket_ML_G / Money_ML_G)
    ml_ticket_pct: float = Field(0.0, alias="Ticket_ML_G")
    ml_money_pct: float = Field(0.0, alias="Money_ML_G")
    
    has_tbd_pitcher: bool = Field(False, alias="has_tbd_pitcher")

# 對應您傳入的 {"matches": [...], "count": 1}
class MatchWrapper(BaseModel):
    matches: List[SingleMatch]
    count: Optional[int] = 0

# ──────────────────────────────────────────
# 3. 核心引擎 (完全移植您的 V0.1 邏輯)
# ──────────────────────────────────────────

class MLB_ML_Engine_V01:
    VERSION = "MLB-ML-V0.1"
    FAV_MOVE_THRESHOLD_B1   = -0.10
    FAV_MON_THRESHOLD_B1    =  55.0
    FAV_MOVE_THRESHOLD_B2   = -0.05

    def parse_inputs(self, mlo, mlc, rlo, rlc, tkt, mon):
        gmo, hmo = parse_ml_odds(mlo)
        gmc, hmc = parse_ml_odds(mlc)
        if not all([gmo, hmo, gmc, hmc]):
            return {"error": "ML_PARSE_FAIL"}

        sp_ov, _, _ = parse_rl(rlo)
        sp_cv, _, _ = parse_rl(rlc)

        if gmc <= hmc:
            fav_side, fav_ml_c, fav_ml_o, fav_mon, fav_tkt = 'guest', gmc, gmo, mon, tkt
        else:
            fav_side, fav_ml_c, fav_ml_o, fav_mon, fav_tkt = 'home', hmc, hmo, 100.0 - mon, 100.0 - tkt

        fav_move = fav_ml_c - fav_ml_o
        rl_flip = 1 if (sp_ov is not None and sp_cv is not None and (sp_ov >= 0) != (sp_cv >= 0)) else 0

        return {
            "fav_side": fav_side,
            "fav_move": round(fav_move, 3),
            "fav_mon": fav_mon,
            "fav_tkt": fav_tkt,
            "fav_smd": fav_mon - fav_tkt,
            "fav_impl_prob": round(1 / fav_ml_c, 4),
            "rl_flip": rl_flip
        }

    def scan(self, data: SingleMatch) -> dict:
        if data.has_tbd_pitcher:
            return {"match": data.match, "status": "PASS", "signals": [], "diagnostics": "⚠️ TBD投手"}

        feats = self.parse_inputs(
            data.ml_open_str, data.ml_close_str,
            data.rl_open_str, data.rl_close_str,
            data.ml_ticket_pct, data.ml_money_pct
        )
        
        if "error" in feats:
            return {"match": data.match, "status": "PASS", "signals": [], "diagnostics": f"🚨 DATA_MISSING: {feats['error']}"}

        fav, move, mon, flip = feats["fav_side"], feats["fav_move"], feats["fav_mon"], feats["rl_flip"]
        signals = []
        
        # B2: RL-Flip-Confirm
        if flip == 1 and move <= self.FAV_MOVE_THRESHOLD_B2:
            signals.append({
                "signal": "B2-MLB: RL-Flip-Confirm",
                "direction": f"{fav} ML",
                "logic": "跑壘線翻轉 + ML賠率確認"
            })
        
        # B1: Fav-Steam
        if not signals and move <= self.FAV_MOVE_THRESHOLD_B1 and mon >= self.FAV_MON_THRESHOLD_B1:
            signals.append({
                "signal": "B1-MLB: Fav-Steam",
                "direction": f"{fav} ML",
                "logic": "盤主降賠 + 聰明錢確認"
            })

        return {
            "match": data.match,
            "status": "ACTIVE" if signals else "PASS",
            "signals": signals,
            "diagnostics": f"fav={fav} | move={move:+.3f} | mon={mon:.0f}% | flip={flip}"
        }

# ──────────────────────────────────────────
# 4. FastAPI 路由 (處理 [ { "matches": ... } ] )
# ──────────────────────────────────────────

app = FastAPI()
engine = MLB_ML_Engine_V01()

@app.get("/")
def home():
    return {"status": "I am awake!"}

@app.post("/scan")
def scan_endpoint(input_data: Union[MatchWrapper, List[MatchWrapper]]):
    # 處理外層的 [ ]
    data = input_data[0] if isinstance(input_data, list) else input_data
    
    final_results = []
    for m in data.matches:
        res = engine.scan(m)
        final_results.append(res)
        
    return {"results": final_results}

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)