# 使用輕量級 Python 鏡像
FROM python:3.10-slim

# 設定環境變數，確保 Python 輸出直接顯示在日誌中，不緩衝
ENV PYTHONUNBUFFERED=1

# 設定工作目錄
WORKDIR /app

# 先複製 requirements.txt 以利用 Docker 層快取優化安裝速度
COPY requirements.txt .

# 安裝必要的套件
RUN pip install --no-cache-dir -r requirements.txt

# 複製其餘程式碼
COPY . .

# Cloud Run 預設會提供 $PORT 環境變數，通常為 8080
# 這裡使用 uvicorn 啟動 FastAPI，並綁定到 0.0.0.0
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]