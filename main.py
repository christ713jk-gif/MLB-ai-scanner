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
        if '%' in str(val) or f > 1.0:
            return f
        else:
            return f * 100.0
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
# 2. 數據模型
# ──────────────────────────────────────────

class SingleMatch(BaseModel):
    match_str:       str               = Field(...,   alias="Match")

    # ── ML 賠率 ──────────────────────────
    ml_open_str:     str               = Field("N/A", alias="ML_Open")
    ml_close_str:    str               = Field("N/A", alias="ML_Close")

    # ── 跑壘線 ───────────────────────────
    rl_open_str:     Union[str, float] = Field("N/A", alias="Spread_Open")
    rl_close_str:    Union[str, float] = Field("N/A", alias="Spread_Close")

    # ── 大小分 ───────────────────────────
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
    daily_dsi:       Optional[float]   = Field(None,  alias="Daily_DSI")

    class Config:
        populate_by_name = True


# ──────────────────────────────────────────
# 3. 引擎參數（每季回顧）
# ──────────────────────────────────────────

# ── 已激活信號 ────────────────────────────
FAV_MOVE_B2       = -0.05   # B2: RL Flip 搭配的 ML 移動門檻
                             #     OOS: 81.8% (N=11) ✅ 穩定

# ── 重校候選（未激活，觀察中）────────────
FAV_MOVE_B1_V2    = -0.08   # B1修正版: 賠率縮短門檻（更嚴）
FAV_SMD_B1_V2     =  10.0   # B1修正版: SMD確認門檻（排除假蒸汽）
                             #     OOS: 75.0% (N=8) ⚠️ 等N=15後評估

# ── 候選假說（未激活，積累中）────────────
Z1_MOVE_MAX       =  0.02   # Z1: ML靜止定義（|move| < 0.02）
Z1_TKT_LO         = 45.0   # Z1: 散戶情緒中性下界
Z1_TKT_HI         = 70.0   # Z1: 散戶情緒中性上界
                             #     OOS: 73.3% (N=33) p=0.017 ⚠️ 需分離OOS驗證

FAV_MOVE_D1       =  0.05   # D1: 盤主走弱門檻（正值=賠率拉長）
FAV_TKT_D1        = 65.0   # D1: 散戶仍壓盤主門檻
                             #     OOS: 69.2% (N=13) p=0.133 ⚠️ 積累中

# ── 環境觀察（不觸發信號，僅標記）────────
EXTREME_FAV_LINE  =  1.35   # 極端熱門陷阱觀察線（盤主WR=28.6% N=7）


# ──────────────────────────────────────────
# 4. 單場分析核心
# ──────────────────────────────────────────

def analyze_match(m: SingleMatch) -> dict:

    # ── TBD 投手安全門 ────────────────────
    if m.has_tbd_pitcher:
        return {
            "Match":       m.match_str,
            "Status":      "PASS",
            "signals":     [],
            "Diagnostics": "⚠️ TBD投手：基線失效，需人工核實"
        }

    # ── ML 解析 ───────────────────────────
    gmo, hmo = parse_ml_odds(m.ml_open_str)
    gmc, hmc = parse_ml_odds(m.ml_close_str)
    if not all([gmo, hmo, gmc, hmc]):
        return {
            "Match":       m.match_str,
            "Status":      "PASS",
            "signals":     [],
            "Diagnostics": f"🚨 ML_MISSING: Open({m.ml_open_str}) Close({m.ml_close_str})"
        }

    # ── 確定盤主視角 ──────────────────────
    raw_tkt_ml = parse_pct(m.tkt_ml)
    raw_mon_ml = parse_pct(m.mon_ml)

    if gmc <= hmc:
        fav      = 'guest'
        fav_ml_o = gmo;  fav_ml_c = gmc
        fav_tkt  = raw_tkt_ml
        fav_mon  = raw_mon_ml
    else:
        fav      = 'home'
        fav_ml_o = hmo;  fav_ml_c = hmc
        fav_tkt  = (100.0 - raw_tkt_ml) if raw_tkt_ml is not None else None
        fav_mon  = (100.0 - raw_mon_ml)  if raw_mon_ml is not None else None

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

    # ── 環境標記（不阻擋信號，僅附加警示）──
    env_flags = []
    if fav_ml_c <= EXTREME_FAV_LINE:
        env_flags.append(f"⚠️ 極端熱門陷阱({fav_ml_c:.2f}≤{EXTREME_FAV_LINE})：歷史盤主WR僅28.6%，謹慎")

    # ── 信號邏輯（優先順序固定）─────────────
    signals = []
    fired   = False

    # ────────────────────────────────────────
    # B2-MLB: RL-Flip-Confirm【已激活】
    # 條件: rl_flip=1 AND fav_move ≤ -0.05
    # OOS: N=11  WR=81.8%  ✅ 穩定
    # 方向: 新跑壘線盤主方 ML
    # ────────────────────────────────────────
    if not fired and flip == 1 and fav_move <= FAV_MOVE_B2:
        signals.append({
            "Type":        "ML",
            "Target":      f"{new_rl_fav} ML",
            "Tier":        "2-Star ⭐⭐",
            "Rule":        "B2-MLB: RL-Flip-Confirm",
            "Expected_WR": "81.8% (OOS N=11)",
            "Maturity":    "[Emerging] 穩定",
            "Logic":       "跑壘線方向翻轉+ML同向確認，兩市場共振"
        })
        fired = True

    # ────────────────────────────────────────
    # B1v2-MLB: Fav-Steam-Confirmed【重校候選，未激活】
    # 條件: fav_move ≤ -0.08 AND fav_smd ≥ +10
    # OOS: N=8  WR=75.0%  ⚠️ 等N=15後評估
    # 邏輯: 排除假蒸汽（SMD確認=聰明錢真實超越散戶）
    # ────────────────────────────────────────
    if not fired and fav_move <= FAV_MOVE_B1_V2 and (fav_smd or 0) >= FAV_SMD_B1_V2:
        signals.append({
            "Type":        "ML",
            "Target":      f"{fav} ML",
            "Tier":        "Observation",
            "Rule":        "B1v2-MLB: Fav-Steam-Confirmed",
            "Expected_WR": "75.0% (OOS N=8)",
            "Maturity":    "[Experimental] 等N=15激活",
            "Logic":       "盤主賠率縮短≥0.08+SMD≥+10，排除散戶假蒸汽"
        })
        fired = True

    # ────────────────────────────────────────
    # Z1-MLB: Zombie-Static【候選假說，未激活】
    # 條件: |fav_move| < 0.02 AND fav_tkt ∈ [45%, 70%]
    # OOS: N=33  WR=73.3%  p=0.017  ⚠️ 需分離OOS驗證
    # 邏輯: ML靜止+散戶中性=莊家極度自信，初始定價即終局
    # ────────────────────────────────────────
    if not fired and abs(fav_move) < Z1_MOVE_MAX and \
            fav_tkt is not None and Z1_TKT_LO <= fav_tkt <= Z1_TKT_HI:
        signals.append({
            "Type":        "ML",
            "Target":      f"{fav} ML",
            "Tier":        "Observation",
            "Rule":        "Z1-MLB: Zombie-Static",
            "Expected_WR": "73.3% (OOS N=33, p=0.017)",
            "Maturity":    "[Hypothesis] 需分離OOS驗證",
            "Logic":       "ML完全靜止+散戶情緒中性，莊家對初始定價極度自信"
        })
        fired = True

    # ────────────────────────────────────────
    # D1-MLB: Fade-Weak-Fav【候選假說，未激活】
    # 條件: fav_move ≥ +0.05 AND fav_tkt ≥ 65%
    # OOS: N=13  WR=69.2%  p=0.133  ⚠️ 積累中
    # 方向: 冷門 ML（逆盤主）
    # 邏輯: 莊家悄悄撤退，散戶還沒反應
    # ────────────────────────────────────────
    if not fired and fav_move >= FAV_MOVE_D1 and \
            fav_tkt is not None and fav_tkt >= FAV_TKT_D1:
        dog_side = 'home' if fav == 'guest' else 'guest'
        signals.append({
            "Type":        "ML",
            "Target":      f"{dog_side} ML",
            "Tier":        "Observation",
            "Rule":        "D1-MLB: Fade-Weak-Fav",
            "Expected_WR": "69.2% (OOS N=13, p=0.133)",
            "Maturity":    "[Hypothesis] 積累中，等N=20",
            "Logic":       "莊家悄悄放棄盤主(move≥+0.05)，散戶仍≥65%押盤主，逆向冷門"
        })
        fired = True

    # ── 診斷字串 ─────────────────────────
    smd_str = f"{fav_smd:+.0f}" if fav_smd is not None else "N/A"
    tkt_str = f"{fav_tkt:.0f}%" if fav_tkt is not None else "N/A"
    mon_str = f"{fav_mon:.0f}%" if fav_mon is not None else "N/A"
    diagnostics = (
        f"fav={fav} | move={fav_move:+.3f} | "
        f"mon={mon_str} | tkt={tkt_str} | smd={smd_str} | "
        f"impl={impl_prob:.1%} | flip={flip}"
    )
    if env_flags:
        diagnostics += " | " + " | ".join(env_flags)

    return {
        "Match":       m.match_str,
        "Status":      "ACTIVE" if signals else "PASS",
        "signals":     signals,
        "Diagnostics": diagnostics,
    }


# ──────────────────────────────────────────
# 5. FastAPI 路由（自動正規化攔截器）
# ──────────────────────────────────────────

app = FastAPI(title="MLB AI Scanner V0.3")


def normalize_payload(raw_dict: dict) -> dict:
    """動態攔截 n8n 傳來的各種欄位名稱變體"""
    new_dict = raw_dict.copy()
    key_mapping = {
        "Spread_Ticket": "Ticket_Spread_G",
        "Spread_Money":  "Money_Spread_G",
        "Total_Ticket":  "Ticket_Total_Over",
        "Total_Money":   "Money_Total_Over",
        "ML_Ticket":     "Ticket_ML_G",
        "ML_Money":      "Money_ML_G",
        # V0.2 舊別名相容
        "ML_Ticket_Pct":     "Ticket_ML_G",
        "ML_Money_Pct":      "Money_ML_G",
        "Spread_Ticket_Pct": "Ticket_Spread_G",
        "Spread_Money_Pct":  "Money_Spread_G",
        "Total_Ticket_Pct":  "Ticket_Total_Over",
        "Total_Money_Pct":   "Money_Total_Over",
        # FG_ 舊別名相容（V0.1格式）
        "FG_Open":  "Spread_Open",
        "FG_Close": "Spread_Close",
    }
    for k, v in raw_dict.items():
        # 精確匹配
        if k in key_mapping:
            new_dict[key_mapping[k]] = v
            continue
        # 前綴匹配
        for prefix, standard_key in key_mapping.items():
            if k.startswith(prefix) and standard_key not in new_dict:
                new_dict[standard_key] = v
    return new_dict


@app.get("/")
def home():
    return {
        "status":  "Online",
        "version": "MLB-Scanner-V0.3",
        "signals": {
            "active":      ["B2-MLB: RL-Flip-Confirm"],
            "observation": ["B1v2-MLB: Fav-Steam-Confirmed",
                            "Z1-MLB: Zombie-Static",
                            "D1-MLB: Fade-Weak-Fav"],
            "retired":     ["B1-MLB: Fav-Steam (原版，已重校)"]
        }
    }


@app.post("/scan")
@app.post("/api/v1/scan")
async def scan_endpoint(request: Request):
    payload = await request.json()

    # 支援單筆 dict / 多筆 list / {matches:[...]} 包裝格式
    if isinstance(payload, dict):
        raw_list = payload.get("matches", [payload])
    elif isinstance(payload, list):
        # 支援 [{matches:[...]}, ...] 的外層陣列
        raw_list = []
        for item in payload:
            if isinstance(item, dict) and "matches" in item:
                raw_list.extend(item["matches"])
            else:
                raw_list.append(item)
    else:
        raw_list = [payload]

    results = []
    for raw_match in raw_list:
        normalized = normalize_payload(raw_match)
        try:
            m = SingleMatch(**normalized)
            results.append(analyze_match(m))
        except Exception as e:
            results.append({
                "Match":       raw_match.get("Match", "Unknown"),
                "Status":      "ERROR",
                "Diagnostics": f"Data parsing error: {str(e)}"
            })

    return [{
        "version": "MLB-Scanner-V0.3",
        "results": results
    }]


# ──────────────────────────────────────────
# 6. 啟動設定（Cloud Run / 本機通用）
# ──────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)