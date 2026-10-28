"""均線發散突破：顯示同 repo 根目錄的 index.html（由本機 ma_fan_breakout.py 產生）。
掃描太久、免費空間會逾時，所以資料在本機算好，這裡只負責顯示。

另外在載入時，替每檔卡片標題加一個「Weinstein 分析 ↗」連結（/?ticker=代號），
點下去會開新分頁並自動分析該檔。這個修改只在讀檔後做，不會動到 index.html 本身，
所以之後重新產生 index.html 不用任何調整。
"""
import pathlib

import streamlit as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"

_OLD = """<div class="tag">'+stock.m+' · '+stock.i+'</div></div>'+"""
_NEW = (
    """<div class="tag">'+stock.m+' · '+stock.i+' · """
    """<a href="/?ticker='+stock.c+'" target="_blank" rel="noopener" """
    """style="color:var(--accent);font-weight:700;text-decoration:none">Weinstein 分析 ↗</a>"""
    """</div></div>'+"""
)


@st.cache_data(show_spinner="載入掃描資料…")
def load_html(mtime: float) -> str:
    """mtime 當作快取鍵：index.html 換新檔後會自動重讀。"""
    html = INDEX.read_text(encoding="utf-8")
    if "/?ticker='+stock.c" not in html:
        html = html.replace(_OLD, _NEW, 1)
    return html


st.markdown(
    "<style>.block-container{padding-top:2.5rem;padding-left:.5rem;padding-right:.5rem}</style>",
    unsafe_allow_html=True,
)

def show_html(html: str, height: int = 1800) -> None:
    """新版 Streamlit 用 st.iframe（components.html 已被標示將移除），舊版退回 components.html。"""
    if hasattr(st, "iframe"):
        st.iframe(html, height=height)
    else:
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=True)


if INDEX.exists():
    show_html(load_html(INDEX.stat().st_mtime))
else:
    st.error("找不到 index.html，請先在本機執行 ma_fan_breakout.py，並把 index.html 上傳到 repo 最外層。")
