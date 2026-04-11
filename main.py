import re
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ──────────────────────────────────────────
# 1. 定義 API 請求格式 (Pydantic Model)
# ──────────────────────────────────────────
class MatchData(BaseModel):
    match: str = Field(..., description="比賽描述", example="MIL vs KC (4/5)")
    ml_open_str: str = Field(..., description="開盤ML賠率", example="1.87 / 1.63")
    ml_close_str: str = Field(..., description="收盤ML賠率", example="1.7 / 1.8")
    rl_open_str: str = Field(..., description="開盤讓分盤", example="1.5 (1.43 / 2.15)")
    rl_close_str: str = Field(..., description="收盤讓分盤", example="-1.5 (2.07 / 1.46)")
    ml_ticket_pct: float = Field(..., description="客隊ML票數%", example=12.0)
    ml_money_pct: float = Field(..., description="客隊ML金額%", example=10.0)
    has_tbd_pitcher: bool = Field(False, description="是否有 TBD 投手")

# ──────────────────────────────────────────
# 2. 原始解析工具
# ──────────────────────────────────────────
def parse_ml_odds(s: str):
    try:
        parts = s.strip().split('/')
        return float(parts[0].strip()), float(parts[1].strip())
    except:
        return None, None

def parse_rl(s: str):
    try:
        m = re.match(r'(-?\d+\.?\d*)\s*\((\d+\.?\d*)\s*/\s*(\d+\.?\d*)\)', s.strip())
        if m:
            return float(m.group(1)), float(m.group(2)), float(m.group(3))
    except:
        pass
    return None, None, None

# ──────────────────────────────────────────
# 3. 核心引擎
# ──────────────────────────────────────────
class MLB_ML_Engine_V01:
    VERSION = "MLB-ML-V0.1"

    FAV_MOVE_THRESHOLD_B1   = -0.10   
    FAV_MON_THRESHOLD_B1    =  55.0   
    FAV_MOVE_THRESHOLD_B2   = -0.05   

    def parse_inputs(
        self,
        ml_open_str: str,
        ml_close_str: str,
        rl_open_str: str,
        rl_close_str: str,
        ml_ticket_pct: float,
        ml_money_pct: float,
    ) -> dict:
        gmo, hmo = parse_ml_odds(ml_open_str)
        gmc, hmc = parse_ml_odds(ml_close_str)
        if not all([gmo, hmo, gmc, hmc]):
            return {"error": "ML_PARSE_FAIL"}

        sp_ov, _, _ = parse_rl(rl_open_str)
        sp_cv, _, _ = parse_rl(rl_close_str)

        if gmc <= hmc:
            fav_side      = 'guest'
            fav_ml_close  = gmc
            fav_ml_open   = gmo
            fav_mon       = ml_money_pct
            fav_tkt       = ml_ticket_pct
        else:
            fav_side      = 'home'
            fav_ml_close  = hmc
            fav_ml_open   = hmo
            fav_mon       = 100.0 - ml_money_pct
            fav_tkt       = 100.0 - ml_ticket_pct

        fav_move = fav_ml_close - fav_ml_open

        rl_flip = 0
        new_rl_fav = None
        if sp_ov is not None and sp_cv is not None:
            if (sp_ov >= 0) != (sp_cv >= 0):          
                rl_flip = 1
                new_rl_fav = 'guest' if sp_cv < 0 else 'home'

        return {
            "fav_side":      fav_side,
            "fav_ml_close":  fav_ml_close,
            "fav_ml_open":   fav_ml_open,
            "fav_move":      round(fav_move, 3),
            "fav_mon":       fav_mon,
            "fav_tkt":       fav_tkt,
            "fav_smd":       fav_mon - fav_tkt,
            "fav_impl_prob": round(1 / fav_ml_close, 4),
            "rl_flip":       rl_flip,
            "new_rl_fav":    new_rl_fav,
        }

    def scan(self, data: MatchData) -> dict:
        if data.has_tbd_pitcher:
            return {
                "match":   data.match,
                "status":  "PASS",
                "signals": [],
                "diagnostics": "⚠️ TBD投手：基線失效，需人工核實後再評估",
            }

        feats = self.parse_inputs(
            data.ml_open_str, data.ml_close_str,
            data.rl_open_str, data.rl_close_str,
            data.ml_ticket_pct, data.ml_money_pct,
        )
        if "error" in feats:
            return {
                "match":   data.match,
                "status":  "PASS",
                "signals": [],
                "diagnostics": f"🚨 DATA_MISSING: {feats['error']}",
            }

        fav   = feats["fav_side"]
        move  = feats["fav_move"]
        mon   = feats["fav_mon"]
        flip  = feats["rl_flip"]
        impl  = feats["fav_impl_prob"]

        signals = []
        fired   = False

        # B2-MLB 信號
        if not fired and flip == 1 and move <= self.FAV_MOVE_THRESHOLD_B2:
            signals.append({
                "signal":     "B2-MLB: RL-Flip-Confirm",
                "tier":       "2-Star ⭐⭐",
                "direction":  f"{fav} ML (新跑壘線盤主方)",
                "train_wr":   "100.0% (N=6)",
                "maturity":   "[Experimental]",
                "edge_est":   "+40.1%",
                "logic":      "跑壘線方向逆轉 + ML賠率同向確認，兩市場雙重暴露莊家意圖",
                "falsify":    "若 N=20 時 WR < 70% 則降級暫停",
            })
            fired = True

        # B1-MLB 信號
        if not fired and move <= self.FAV_MOVE_THRESHOLD_B1 and mon >= self.FAV_MON_THRESHOLD_B1:
            signals.append({
                "signal":     "B1-MLB: Fav-Steam",
                "tier":       "2-Star ⭐⭐",
                "direction":  f"{fav} ML",
                "train_wr":   "80.0% (N=10)",
                "maturity":   "[Emerging]",
                "edge_est":   "+15.4%",
                "logic":      "盤主賠率大幅縮短(≥0.10) + 聰明錢確認(≥55%)，蒸汽信號",
                "falsify":    "若 N=25 時 WR < 65% 則重校閾值",
            })
            fired = True

        diag = (
            f"fav={fav} | fav_move={move:+.3f} | fav_mon={mon:.0f}% | "
            f"fav_smd={feats['fav_smd']:+.0f} | rl_flip={flip} | "
            f"impl_prob={impl:.1%}"
        )

        return {
            "match":       data.match,
            "status":      "ACTIVE" if signals else "PASS",
            "signals":     signals,
            "diagnostics": diag,
        }

# ──────────────────────────────────────────
# 4. FastAPI 路由設定
# ──────────────────────────────────────────
app = FastAPI(title="MLB ML Engine API", version="0.1")
engine = MLB_ML_Engine_V01()

@app.post("/api/v1/scan")
def scan_match(data: MatchData):
    """
    接收單場比賽數據，回傳信號與診斷結果。
    """
    try:
        result = engine.scan(data)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))