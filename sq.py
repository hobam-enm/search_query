import json
import re
import time
import datetime as dt
import io

import streamlit as st
import requests
import pandas as pd
from pytrends.request import TrendReq
import plotly.express as px


# =========================================================
# 0) 설정: Streamlit Secrets에서 API 키 불러오기
# =========================================================

# --- 네이버 DataLab Search API (트렌드 ratio) ---
try:
    CLIENT_ID = st.secrets["NAVER_CLIENT_ID"]
    CLIENT_SECRET = st.secrets["NAVER_CLIENT_SECRET"]
except (KeyError, FileNotFoundError):
    st.error("⚠️ API 키가 없습니다! Streamlit Cloud 대시보드의 [App settings] -> [Secrets]에 네이버 API 키를 등록해주세요.")
    st.code("""
NAVER_CLIENT_ID = "본인의_클라이언트_ID"
NAVER_CLIENT_SECRET = "본인의_클라이언트_시크릿"
    """, language="toml")
    st.stop()

DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"

# --- 앵커/앵커월 (고정) ---
ANCHOR_MONTH_START = "2026-01-01"
ANCHOR_MONTH_END = "2026-01-31"

# 앵커 월간 '절대' 검색량
ANCHORS = [
    {"group": "anchor_tvn", "keyword": "tvN",      "monthly_volume": 47600},
    {"group": "anchor_nf",  "keyword": "넷플릭스", "monthly_volume": 2353200},
]

# 디자인 톤온톤 컬러 팔레트 설정 (블루/네이비 계열)
COLOR_TOTAL = "#93C5FD"      # Light Blue (전체 검색량)
COLOR_DRAMA = "#1E3A8A"      # Deep Blue (드라마 의도 검색량)
COLOR_BROADCAST = "#3730A3"  # Deep Indigo (방영일 평균)
COLOR_NON_BROADCAST = "#A5B4FC" # Light Indigo (비방영일 평균)


# =========================================================
# 1) 연관 검색어 추출 (네이버 자동완성 + 구글 트렌드 하이브리드)
# =========================================================

def fetch_naver_autocomplete(query: str) -> list:
    url = "https://ac.search.naver.com/nx/ac"
    params = {
        "q": query,
        "st": 100,
        "r_format": "json",
        "q_enc": "UTF-8",
        "r_enc": "UTF-8"
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", [[]])[0]
            return [item[0] for item in items if isinstance(item, list) and len(item) > 0]
    except Exception as e:
        print(f"네이버 자동완성 오류: {e}")
    return []


def fetch_google_trends_related(query: str) -> list:
    try:
        pytrend = TrendReq(hl='ko-KR', tz=540)
        pytrend.build_payload(kw_list=[query], timeframe='today 1-m', geo='KR')
        related_payload = pytrend.related_queries()
        
        kws = []
        if query in related_payload and related_payload[query] is not None:
            if 'top' in related_payload[query] and related_payload[query]['top'] is not None:
                kws.extend(related_payload[query]['top']['query'].tolist())
            if 'rising' in related_payload[query] and related_payload[query]['rising'] is not None:
                kws.extend(related_payload[query]['rising']['query'].tolist())
        
        unique_kws = list(dict.fromkeys(kws))
        return unique_kws[:20]
    except Exception as e:
        print(f"구글 트렌드 연관어 오류: {e}")
        return []


def get_combined_related_keywords(seed_keyword: str) -> pd.DataFrame:
    naver_kws = fetch_naver_autocomplete(seed_keyword)
    google_kws = fetch_google_trends_related(seed_keyword)
    
    combined = []
    for kw in naver_kws + google_kws:
        if kw not in combined and seed_keyword in kw:
            combined.append(kw)
            
    df = pd.DataFrame({"keyword": combined})
    return df


# =========================================================
# 2) DataLab 호출 + 절대검색량 추정 (앵커 스케일링)
# =========================================================

def post_datalab(start_date: str, end_date: str, keyword_groups: list[dict]) -> dict:
    headers = {
        "X-Naver-Client-Id": CLIENT_ID,
        "X-Naver-Client-Secret": CLIENT_SECRET,
        "Content-Type": "application/json",
    }
    payload = {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": "date",
        "keywordGroups": keyword_groups,
    }
    resp = requests.post(DATALAB_URL, headers=headers, data=json.dumps(payload, ensure_ascii=False), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"[DATALAB] HTTP {resp.status_code}\n{resp.text}")
    return resp.json()


def datalab_json_to_pivot(api_json: dict) -> pd.DataFrame:
    rows = []
    for r in api_json.get("results", []):
        title = r.get("title")
        for pt in r.get("data", []):
            rows.append({"date": pt.get("period"), "group": title, "ratio": pt.get("ratio")})

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])
    piv = df.pivot_table(index="date", columns="group", values="ratio", aggfunc="first").sort_index()
    return piv


def compute_k_from_anchor_month(piv: pd.DataFrame, anchor_group: str, monthly_volume: float) -> float:
    jan_mask = (piv.index >= pd.to_datetime(ANCHOR_MONTH_START)) & (piv.index <= pd.to_datetime(ANCHOR_MONTH_END))
    if anchor_group not in piv.columns:
        raise RuntimeError(f"DataLab 결과에 앵커 그룹 '{anchor_group}' 컬럼이 없음")

    ratio_sum = float(piv.loc[jan_mask, anchor_group].sum())
    if ratio_sum <= 0:
        raise RuntimeError(f"앵커 '{anchor_group}'의 1월 ratio 합이 0")

    return monthly_volume / ratio_sum


def estimate_total_abs_timeseries(seed_keyword: str, start_date: str, end_date: str) -> pd.DataFrame:
    kw_group = f"kw_{seed_keyword}"

    keyword_groups = []
    for a in ANCHORS:
        keyword_groups.append({"groupName": a["group"], "keywords": [a["keyword"]]})
    keyword_groups.append({"groupName": kw_group, "keywords": [seed_keyword]})

    api = post_datalab(start_date, end_date, keyword_groups)
    piv = datalab_json_to_pivot(api)
    if piv.empty:
        raise RuntimeError("DataLab 결과가 비었습니다. 키워드/기간/인증 확인")

    k_map = {}
    for a in ANCHORS:
        k_map[a["group"]] = compute_k_from_anchor_month(piv, a["group"], a["monthly_volume"])

    total_anchor_vol = sum(a["monthly_volume"] for a in ANCHORS)
    weights = {a["group"]: a["monthly_volume"] / total_anchor_vol for a in ANCHORS}

    out = pd.DataFrame(index=piv.index)
    out["total_abs_est"] = 0.0

    for a in ANCHORS:
        g = a["group"]
        out["total_abs_est"] += weights[g] * (piv[kw_group] * k_map[g])

    out = out.reset_index().rename(columns={"index": "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    return out


def calculate_related_kws_volume(seed_keyword: str, related_df: pd.DataFrame, start_date: str, end_date: str, seed_total_abs: float) -> pd.DataFrame:
    if "keyword" not in related_df.columns:
        return pd.DataFrame(columns=["연관어", "전체 검색량"])

    kws = related_df["keyword"].dropna().astype(str).unique().tolist()
    kws = [k for k in kws if k != seed_keyword]

    results = []
    chunk_size = 4
    for i in range(0, len(kws), chunk_size):
        chunk = kws[i:i+chunk_size]
        groups = [{"groupName": "SEED", "keywords": [seed_keyword]}]
        for kw in chunk:
            groups.append({"groupName": kw, "keywords": [kw]})

        try:
            api_res = post_datalab(start_date, end_date, groups)
            piv = datalab_json_to_pivot(api_res)

            if piv.empty or "SEED" not in piv.columns:
                for kw in chunk:
                    results.append({"연관어": kw, "전체 검색량": 0})
                continue

            seed_ratio_sum = float(piv["SEED"].sum())
            for kw in chunk:
                if kw in piv.columns and seed_ratio_sum > 0:
                    kw_ratio_sum = float(piv[kw].sum())
                    kw_abs_vol = seed_total_abs * (kw_ratio_sum / seed_ratio_sum)
                    results.append({"연관어": kw, "전체 검색량": kw_abs_vol})
                else:
                    results.append({"연관어": kw, "전체 검색량": 0})
        except Exception as e:
            for kw in chunk:
                results.append({"연관어": kw, "전체 검색량": 0})

    res_df = pd.DataFrame(results)
    if not res_df.empty:
        res_df = res_df.sort_values("전체 검색량", ascending=False)
    return res_df


# =========================================================
# 3) 드라마 의도 비율 p (데이터랩 기반)
# =========================================================

def compute_drama_share_p_via_datalab(related_csv_df: pd.DataFrame, start_date: str, end_date: str) -> float:
    df = related_csv_df.copy()
    
    if "is_drama" not in df.columns or "keyword" not in df.columns:
        raise RuntimeError("연관어 데이터에 'keyword'와 'is_drama' 컬럼이 필요합니다.")

    df["is_drama"] = pd.to_numeric(df["is_drama"], errors="coerce")
    
    drama_kws = df[df["is_drama"] == 1]["keyword"].dropna().astype(str).tolist()[:20]
    nondrama_kws = df[df["is_drama"] == 0]["keyword"].dropna().astype(str).tolist()[:20]

    if not drama_kws and not nondrama_kws:
        return float("nan")
    if not drama_kws:
        return 0.0
    if not nondrama_kws:
        return 1.0

    groups = []
    if drama_kws:
        groups.append({"groupName": "drama", "keywords": drama_kws})
    if nondrama_kws:
        groups.append({"groupName": "nondrama", "keywords": nondrama_kws})

    api_res = post_datalab(start_date, end_date, groups)
    piv = datalab_json_to_pivot(api_res)

    if piv.empty:
        return float("nan")

    drama_sum = float(piv["drama"].sum()) if "drama" in piv.columns else 0.0
    nondrama_sum = float(piv["nondrama"].sum()) if "nondrama" in piv.columns else 0.0

    denom = drama_sum + nondrama_sum
    if denom <= 0:
        return float("nan")
        
    return drama_sum / denom


# =========================================================
# 4) Streamlit UI 및 메인 로직 (세션 상태 활용 & 디자인 개선)
# =========================================================

st.set_page_config(page_title="드라마 검색량 분석 도구", page_icon="📈", layout="wide")

st.markdown(f"<h2 style='text-align: center; color: {COLOR_DRAMA}; margin-bottom: 30px;'>📈 드라마 검색량 및 의도 분석 도구</h2>", unsafe_allow_html=True)

# 세션 상태 초기화
if "related_kws_df" not in st.session_state:
    st.session_state.related_kws_df = None
if "analysis_done" not in st.session_state:
    st.session_state.analysis_done = False
if "excel_data" not in st.session_state:
    st.session_state.excel_data = None
if "daily_df" not in st.session_state:
    st.session_state.daily_df = None
if "weekly_df" not in st.session_state:
    st.session_state.weekly_df = None
if "b_nb_df" not in st.session_state:
    st.session_state.b_nb_df = None
if "p_value" not in st.session_state:
    st.session_state.p_value = 0.0
if "period_total" not in st.session_state:
    st.session_state.period_total = 0
if "period_drama" not in st.session_state:
    st.session_state.period_drama = 0
if "non_bc_ratio" not in st.session_state:
    st.session_state.non_bc_ratio = 0.0
if "schedule_val" not in st.session_state:
    st.session_state.schedule_val = "드라마 아님"

# --- 설정 박스 ---
with st.container(border=True):
    st.markdown("#### ⚙️ 분석 기본 설정")
    col_k, col_d, col_s, col_e, col_b = st.columns([1.5, 1, 1, 1, 1.2])
    with col_k:
        seed_keyword = st.text_input("분석 키워드", value="세이렌", label_visibility="collapsed", placeholder="키워드 입력 (예: 세이렌)")
    with col_d:
        schedule = st.selectbox("방영 요일", ["드라마 아님", "월화", "수목", "토일"], label_visibility="collapsed")
    with col_s:
        start_date = st.date_input("시작일", value=dt.date(2026, 2, 1), label_visibility="collapsed")
    with col_e:
        end_date = st.date_input("종료일", value=dt.date(2026, 3, 5), label_visibility="collapsed")
    with col_b:
        if st.button("🔍 연관어 가져오기", use_container_width=True):
            if start_date > end_date:
                st.error("시작일이 종료일보다 늦습니다.")
            else:
                with st.spinner("연관어를 수집 중입니다..."):
                    df_kws = get_combined_related_keywords(seed_keyword)
                    if not df_kws.empty:
                        df_kws["드라마 의도 (체크)"] = False 
                        st.session_state.related_kws_df = df_kws
                        st.session_state.analysis_done = False
                        st.session_state.schedule_val = schedule
                    else:
                        st.warning("조건에 맞는 연관어를 찾을 수 없습니다.")

st.markdown("<br>", unsafe_allow_html=True)

# --- 연관어 라벨링 영역 (진행 중일 때만 노출) ---
if st.session_state.related_kws_df is not None and not st.session_state.analysis_done:
    st.markdown("#### 💡 연관어 라벨링")
    st.caption("드라마를 의미하는 검색어에만 체크해주세요.")
    
    # 모두 선택 / 모두 해제 버튼
    col_btn1, col_btn2, _ = st.columns([1.5, 1.5, 7])
    if col_btn1.button("✅ 모두 선택", use_container_width=True):
        st.session_state.related_kws_df["드라마 의도 (체크)"] = True
        st.rerun()
    if col_btn2.button("🔲 모두 해제", use_container_width=True):
        st.session_state.related_kws_df["드라마 의도 (체크)"] = False
        st.rerun()

    edited_df = st.data_editor(
        st.session_state.related_kws_df,
        column_config={
            "드라마 의도 (체크)": st.column_config.CheckboxColumn("드라마 의도", width="small"),
            "keyword": st.column_config.TextColumn("연관어", disabled=True)
        },
        hide_index=True,
        use_container_width=True,
        height=300
    )

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🚀 검색량 분석 및 시각화 실행", type="primary", use_container_width=True):
        with st.spinner("데이터랩 트렌드 분석 및 절대 검색량 역산 중... (시간이 소요될 수 있습니다)"):
            try:
                start_str = start_date.strftime("%Y-%m-%d")
                end_str = end_date.strftime("%Y-%m-%d")

                st.session_state.related_kws_df = edited_df
                backend_df = edited_df.copy()
                backend_df["is_drama"] = backend_df["드라마 의도 (체크)"].astype(int)

                p = compute_drama_share_p_via_datalab(backend_df, start_str, end_str)
                if pd.isna(p):
                    st.error("계산 오류: 최소 1개 이상의 키워드에 체크를 하거나 해제하여 데이터를 구성해주세요.")
                    st.stop()
                
                user_start_dt = pd.to_datetime(start_str)
                user_end_dt = pd.to_datetime(end_str)
                anchor_start_dt = pd.to_datetime(ANCHOR_MONTH_START)
                anchor_end_dt = pd.to_datetime(ANCHOR_MONTH_END)
                
                fetch_start_str = min(user_start_dt, anchor_start_dt).strftime("%Y-%m-%d")
                fetch_end_str = max(user_end_dt, anchor_end_dt).strftime("%Y-%m-%d")
                
                total_df = estimate_total_abs_timeseries(seed_keyword, fetch_start_str, fetch_end_str)
                
                total_df["date_dt"] = pd.to_datetime(total_df["date"])
                total_df = total_df[
                    (total_df["date_dt"] >= user_start_dt) & 
                    (total_df["date_dt"] <= user_end_dt)
                ].copy()

                total_df["전체 검색량"] = total_df["total_abs_est"].round().astype(int)
                total_df["드라마 의도 검색량"] = (total_df["total_abs_est"] * p).round().astype(int)
                period_total_abs = int(total_df["전체 검색량"].sum())
                period_drama_abs = int(total_df["드라마 의도 검색량"].sum())

                # --- 방영일/비방영일 로직 처리 ---
                non_bc_ratio = 0.0
                b_nb_df = None
                
                if st.session_state.schedule_val != "드라마 아님":
                    schedule_map = {"월화": [0, 1], "수목": [2, 3], "토일": [5, 6]}
                    target_days = schedule_map[st.session_state.schedule_val]
                    
                    total_df["dayofweek"] = total_df["date_dt"].dt.dayofweek
                    total_df["is_broadcast"] = total_df["dayofweek"].isin(target_days)
                    
                    # 주차 계산 (월요일 시작)
                    total_df['week_start'] = total_df['date_dt'] - pd.to_timedelta(total_df["dayofweek"], unit='d')
                    total_df['주차'] = total_df['week_start'].dt.month.astype(str) + "월" + total_df['week_start'].dt.day.astype(str) + "일주차"
                    
                    # 온전한 7일치 데이터가 있는 주차만 필터링하여 비방영일 검색 유지율 계산
                    full_weeks_count = total_df.groupby('week_start').size()
                    full_weeks = full_weeks_count[full_weeks_count == 7].index
                    
                    if len(full_weeks) > 0:
                        ratio_list = []
                        for w in full_weeks:
                            w_df = total_df[total_df['week_start'] == w]
                            avg_bc = w_df.loc[w_df["is_broadcast"], "드라마 의도 검색량"].mean()
                            avg_nbc = w_df.loc[~w_df["is_broadcast"], "드라마 의도 검색량"].mean()
                            if pd.notna(avg_bc) and avg_bc > 0 and pd.notna(avg_nbc):
                                ratio_list.append(avg_nbc / avg_bc)
                        if ratio_list:
                            non_bc_ratio = sum(ratio_list) / len(ratio_list)
                        else:
                            non_bc_ratio = 0.0
                    else:
                        # 7일이 꽉 찬 주차가 없을 경우 전체 기간 평균으로 Fallback 계산
                        avg_bc_total = total_df.loc[total_df["is_broadcast"], "드라마 의도 검색량"].mean()
                        avg_nbc_total = total_df.loc[~total_df["is_broadcast"], "드라마 의도 검색량"].mean()
                        if pd.notna(avg_bc_total) and avg_bc_total > 0 and pd.notna(avg_nbc_total):
                            non_bc_ratio = avg_nbc_total / avg_bc_total
                        else:
                            non_bc_ratio = 0.0

                    # 주차별 방영일/비방영일 평균 시각화용 데이터
                    b_nb_raw = total_df.groupby(['주차', 'is_broadcast', 'week_start'])['드라마 의도 검색량'].mean().reset_index()
                    b_nb_raw["구분"] = b_nb_raw["is_broadcast"].map({True: "방영일 평균", False: "비방영일 평균"})
                    b_nb_raw["드라마 의도 검색량"] = b_nb_raw["드라마 의도 검색량"].round().astype(int)
                    b_nb_df = b_nb_raw.sort_values('week_start')

                # --- 결과 데이터 프레임 구성 ---
                summary_df = pd.DataFrame({
                    "항목": ["분석 키워드", "조회 기간", "드라마 의도 비중", "총 전체 검색량", "총 드라마 의도 검색량"],
                    "내용": [seed_keyword, f"{start_str} ~ {end_str}", f"{p * 100:.2f}%", f"{period_total_abs:,}", f"{period_drama_abs:,}"]
                })
                # 엑셀용(전체/드라마 의도 모두 포함)
                daily_excel_df = total_df[["date", "전체 검색량", "드라마 의도 검색량"]].copy()
                daily_excel_df.rename(columns={"date": "날짜"}, inplace=True)

                # 차트용(드라마 의도만) + 날짜 표기: M/D (예: 3/4)
                daily_chart_df = total_df[["date_dt", "드라마 의도 검색량"]].copy()
                daily_chart_df["날짜"] = daily_chart_df["date_dt"].dt.month.astype(str) + "/" + daily_chart_df["date_dt"].dt.day.astype(str)
                daily_chart_df = daily_chart_df[["날짜", "드라마 의도 검색량"]]
                weekly_calc = total_df.copy()
                if 'week_start' not in weekly_calc.columns:
                    weekly_calc['week_start'] = weekly_calc['date_dt'] - pd.to_timedelta(weekly_calc['date_dt'].dt.dayofweek, unit='d')
                weekly_grouped = weekly_calc.groupby('week_start')[['전체 검색량', '드라마 의도 검색량']].sum().reset_index()
                weekly_grouped['주차'] = weekly_grouped['week_start'].dt.month.astype(str) + "월" + weekly_grouped['week_start'].dt.day.astype(str) + "일주차"
                # 엑셀용(전체/드라마 의도 모두 포함)
                weekly_excel_df = weekly_grouped[['주차', '전체 검색량', '드라마 의도 검색량']].copy()

                # 차트용(드라마 의도만)
                weekly_chart_df = weekly_grouped[['주차', '드라마 의도 검색량']].copy()
                related_abs_df = calculate_related_kws_volume(seed_keyword, backend_df, start_str, end_str, period_total_abs)
                if not related_abs_df.empty:
                    ox_map = backend_df.set_index("keyword")["is_drama"].map({1: "O", 0: "X"})
                    related_abs_df["드라마 의도"] = related_abs_df["연관어"].map(ox_map).fillna("X")
                    related_abs_df["전체 검색량"] = related_abs_df["전체 검색량"].round().astype(int)
                    related_abs_df = related_abs_df[["연관어", "드라마 의도", "전체 검색량"]]

                # 엑셀 바이너리 생성
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                    summary_df.to_excel(writer, sheet_name="요약", index=False)
                    daily_excel_df.to_excel(writer, sheet_name="일자별 결과", index=False)
                    weekly_excel_df.to_excel(writer, sheet_name="주차별 결과", index=False)
                    if b_nb_df is not None:
                        b_nb_df[['주차', '구분', '드라마 의도 검색량']].to_excel(writer, sheet_name="방영_비방영_비교", index=False)
                    if not related_abs_df.empty:
                        related_abs_df.to_excel(writer, sheet_name="연관어", index=False)

                    workbook = writer.book
                    format_comma = workbook.add_format({'num_format': '#,##0'})
                    writer.sheets['일자별 결과'].set_column('B:C', 15, format_comma)
                    writer.sheets['주차별 결과'].set_column('B:C', 15, format_comma)
                    if b_nb_df is not None:
                        writer.sheets['방영_비방영_비교'].set_column('C:C', 15, format_comma)
                    if not related_abs_df.empty:
                        writer.sheets['연관어'].set_column('C:C', 15, format_comma)

                # 상태 업데이트
                st.session_state.excel_data = output.getvalue()
                st.session_state.daily_df = daily_chart_df
                st.session_state.weekly_df = weekly_chart_df
                st.session_state.b_nb_df = b_nb_df
                st.session_state.p_value = p
                st.session_state.period_total = period_total_abs
                st.session_state.period_drama = period_drama_abs
                st.session_state.non_bc_ratio = non_bc_ratio
                st.session_state.analysis_done = True
                
                st.rerun()

            except Exception as e:
                st.error(f"오류가 발생했습니다: {str(e)}")


# --- 분석 결과 화면 (라벨링을 숨기고 세련된 지표 노출) ---
if st.session_state.analysis_done:
    st.markdown("#### 📊 분석 결과 요약")
    
    # 지표 노출
    if st.session_state.schedule_val != "드라마 아님":
        m1, m2, m3, m4 = st.columns(4)
        m4.metric(label="비방영일 검색 유지율", value=f"{st.session_state.non_bc_ratio * 100:.1f}%")
    else:
        m1, m2, m3 = st.columns(3)

    m1.metric(label="총 전체 검색량", value=f"{st.session_state.period_total:,}회")
    m2.metric(label="총 드라마 의도 검색량", value=f"{st.session_state.period_drama:,}회")
    m3.metric(label="드라마 의도 비중", value=f"{st.session_state.p_value * 100:.1f}%")
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # 차트 시각화
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**일자별 트렌드**")
        fig_daily = px.line(
            st.session_state.daily_df,
            x="날짜",
            y="드라마 의도 검색량",
            color_discrete_sequence=[COLOR_DRAMA]
        )
        fig_daily.update_layout(
            xaxis_title=None,
            yaxis_title=None,
            xaxis=dict(type="category", nticks=10),
            yaxis=dict(tickformat=","),
            hovermode="x unified",
            legend_title_text="",
            margin=dict(l=0, r=0, t=30, b=0)
        )
        st.plotly_chart(fig_daily, use_container_width=True)

    with col2:
        st.markdown("**주차별 트렌드**")
        fig_weekly = px.bar(
            st.session_state.weekly_df,
            x="주차",
            y="드라마 의도 검색량",
            color_discrete_sequence=[COLOR_DRAMA]
        )
        fig_weekly.update_layout(
            xaxis_title=None,
            yaxis_title=None,
            yaxis=dict(tickformat=","),
            legend_title_text="",
            margin=dict(l=0, r=0, t=30, b=0)
        )
        st.plotly_chart(fig_weekly, use_container_width=True)

    # 방영일 vs 비방영일 시각화 (선택 시 노출, 가로 너비 절반 차지하도록 확실히 강제)
    if st.session_state.schedule_val != "드라마 아님" and st.session_state.b_nb_df is not None:
        st.markdown("<br>", unsafe_allow_html=True)
        
        # 확실히 절반만 사용하도록 컨테이너 분리 (오른쪽은 비워둠)
        col_left, col_right = st.columns(2)
        with col_left:
            st.markdown("**주차별 방영일/비방영일 평균 검색량 비교 (드라마 의도)**")
            fig_bnb = px.bar(
                st.session_state.b_nb_df, 
                x="주차", 
                y="드라마 의도 검색량", 
                color="구분",
                barmode="group",
                color_discrete_map={"방영일 평균": COLOR_BROADCAST, "비방영일 평균": COLOR_NON_BROADCAST}
            )
            fig_bnb.update_layout(
                xaxis_title=None,
                yaxis_title=None,
                yaxis=dict(tickformat=","),
                legend_title_text="",
                margin=dict(l=0, r=0, t=30, b=0)
            )
            st.plotly_chart(fig_bnb, use_container_width=True)

    # 하단 액션 버튼 그룹
    st.divider()
    dl_col, reset_col, empty_col = st.columns([2, 2, 4])
    with dl_col:
        st.download_button(
            label="📥 엑셀(Excel) 다운로드",
            data=st.session_state.excel_data,
            file_name=f"search_{seed_keyword}_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True
        )
    with reset_col:
        if st.button("🔄 조건 수정하기", use_container_width=True):
            st.session_state.analysis_done = False
            st.rerun()