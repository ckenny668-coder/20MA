"""Streamlit 外殼：直接顯示同資料夾的 index.html（由 ma_fan_breakout.py 在本機產生）。
掃描太久、免費空間會逾時，所以資料在本機算好，這裡只負責顯示。"""
import pathlib
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="均線發散突破", layout="wide", initial_sidebar_state="collapsed")
st.markdown("<style>.block-container{padding:0.5rem 0.5rem 0}header{visibility:hidden}</style>", unsafe_allow_html=True)

path = pathlib.Path(__file__).with_name("index.html")
if path.exists():
    components.html(path.read_text(encoding="utf-8"), height=1800, scrolling=True)
else:
    st.error("找不到 index.html，請先在本機執行 ma_fan_breakout.py，並把 index.html 上傳到 repo 最外層。")
