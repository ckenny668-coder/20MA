"""台股分析工具（入口）：把兩個頁面放在同一個網站，用上方分頁切換。

  - 📈 Weinstein 階段分析：輸入台股編號（或美股代號），看耗盡缺口、Stage 3 與大戶持股籌碼（預設首頁）
  - 🚀 均線發散突破：顯示本機 ma_fan_breakout.py 產生的 index.html 掃描結果

在「均線發散突破」的每檔卡片按「Weinstein 分析 ↗」，會開新分頁並自動分析該檔。
"""
import streamlit as st

st.set_page_config(page_title="台股分析工具", page_icon="📈", layout="wide")

pages = [
    st.Page("views/weinstein.py", title="Weinstein 階段分析", icon="📈", default=True),
    st.Page("views/breakout.py", title="均線發散突破", icon="🚀", url_path="breakout"),
]

try:
    pg = st.navigation(pages, position="top")   # 新版 Streamlit：分頁放在最上方
except TypeError:
    pg = st.navigation(pages)                   # 舊版：放在側邊欄
pg.run()
