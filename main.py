import re
from typing import Optional, List, Union
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ──────────────────────────────────────────
# 1. 定義 API 請求格式 (Pydantic Model)
# ──────────────────────────────────────────
# 使用 alias 讓 API 可以直接接收您的 JSON 欄位名稱
class MatchData(BaseModel):
    match: str = Field(..., alias="Match")
    
    # 賠率部分：如果沒有傳，預設為 "N/A"
    ml_open_str: Optional[str] = Field("N/A", alias="ML_Open")
    ml_close_str: Optional[str] = Field("N/A", alias="ML_Close")
    
    # 讓分部分：對應您的 FG_Open/FG_Close
    rl_open_str: Optional[Union[str, float]] = Field("N/A", alias="FG_Open")
    rl_close_str: Optional[Union[str, float]] = Field("N/A", alias="FG_Close")
    
    # 資金比例：對應您的 Ticket_ML_G / Money_ML_G
    ml_ticket_pct: Optional[float] = Field(0.0, alias="Ticket_ML_G")
    ml_money_pct: Optional[float] = Field(0.0, alias="Money_ML_G")
    
    # 預防 TBD 投手
    has_tbd_pitcher: bool = Field(False, alias="has_tbd_pitcher")

    class Config:
        populate_by_name = True  # 允許同時使用原始名稱和 alias

# ──────────────────────────────────────────
# 2. 解析工具
# ──────────────────────────────────────────
def parse_ml_odds(s: str):
    if not s or s == "N/A" or s == "Locked":
        return None, None
    try:
        parts = s.strip().split('/')
        return float(parts[0].strip()), float(parts[1].strip())
    except:
        return None, None

def parse_rl(s: Union[str, float]):
    if isinstance(s, (int, float)):
        return float(s), None, None
    try:
        # 匹配 "-1.5 (1.68 / 1.82)" 格式
        m = re.match(r'(-?\d+\.?\d*)\s*\((\d+\.?\d*)\s*/\s*(\d+\.?\d*)\)', str(s).strip())
        if m:
            return float(m.group(1)), float(m.group(2)), float(m.group(3))
        # 僅匹配純數字 "1.5"
        return float(s), None, None
    except:
        return None, None, None

# ──────────────────────────────────────────
# 3. 核心引擎
# ──────────────────────────────────────────
class MLB_ML_Engine_V01:
    VERSION = "MLB-ML-V0.1"
    FAV_MOVE_THRESHOLD_B1   = -0.10   
    FAV_MON_THRESHOLD_B1    =  55.0   
    FAV_MOVE_THRESHOLD_B2   = -0.05   

    def scan(self, data: MatchData) -> dict:
        if data.has_tbd_pitcher:
            return {
                "match": data.match,
                "status": "PASS",
                "signals": [],
                "diagnostics": "⚠️ TBD投手：需人工核實",
            }

        # 解析賠率
        gmo, hmo = parse_ml_odds(data.ml_open_str)
        gmc, hmc = parse_ml_odds(data.ml_close_str)
        
        # 如果解析失敗，回傳診斷訊息
        if not all([gmo, hmo, gmc, hmc]):
            return {
                "match": data.match,
                "status": "PASS",
                "signals": [],
                "diagnostics": f"🚨 賠率格式錯誤或缺失: {data.ml_open_str} / {data.ml_close_str}",
            }

        sp_ov, _, _ = parse_rl(data.rl_open_str)
        sp_cv, _, _ = parse_rl(data.rl_close_str)

        # 判定盤主 (Favorite)
        if gmc <= hmc:
            fav_side, fav_ml_c, fav_ml_o = 'guest', gmc, gmo
            fav_mon, fav_tkt = data.ml_money_pct, data.ml_ticket_pct
        else:
            fav_side, fav_ml_c, fav_ml_o = 'home', hmc, hmo
            fav_mon, fav_tkt = 100.0 - data.ml_money_pct, 100.0 - data.ml_ticket_pct

        fav_move = fav_ml_c - fav_ml_o
        rl_flip = 1 if (sp_ov is not None and sp_cv is not None and (sp_ov >= 0) != (sp_cv >= 0)) else 0

        signals = []
        # B2-MLB: RL-Flip-Confirm
        if rl_flip == 1 and fav_move <= self.FAV_MOVE_THRESHOLD_B2:
            signals.append({
                "signal": "B2-MLB: RL-Flip-Confirm",
                "direction": f"{fav_side} ML",
                "logic": "跑壘線翻轉 + ML賠率確認",
            })

        # B1-MLB: Fav-Steam
        if not signals and fav_move <= self.FAV_MOVE_THRESHOLD_B1 and fav_mon >= self.FAV_MON_THRESHOLD_B1:
            signals.append({
                "signal": "B1-MLB: Fav-Steam",
                "direction": f"{fav_side} ML",
                "logic": f"盤主大幅降賠({fav_move:+.2f}) + 聰明錢({fav_mon:.0f}%)",
            })

        return {
            "match": data.match,
            "status": "ACTIVE" if signals else "PASS",
            "signals": signals,
            "diagnostics": f"fav={fav_side} | move={fav_move:+.2f} | mon={fav_mon:.0f}% | flip={rl_flip}",
        }

# ──────────────────────────────────────────
# 4. FastAPI 應用與路由
# ──────────────────────────────────────────
app = FastAPI(title="MLB AI Scanner API", version="0.2")
engine = MLB_ML_Engine_V01()

# 喚醒與健康檢查介面 (GET)
@app.get("/")
def home():
    return {
        "status": "I am awake!",
        "engine": engine.VERSION,
        "usage": "POST to /scan with match data"
    }

# 分析介面 (POST) - 支援您目前的兩種路徑
@app.post("/api/v1/scan")
@app.post("/scan")
def scan_match(data: MatchData):
    try:
        result = engine.scan(data)
        return result
    except Exception as e:
        # 詳細錯誤捕捉，方便在 Render Logs 查看
        raise HTTPException(status_code=500, detail=f"Engine Error: {str(e)}")

# 若直接執行此檔案 (本地測試用)
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)