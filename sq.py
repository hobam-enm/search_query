import json
import re
import time
import datetime as dt
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
import pandas as pd
from pytrends.request import TrendReq  # 구글 트렌드 API 라이브러리 추가


# =========================================================
# 0) 설정: 여기에 너 키 넣기
# =========================================================

# --- 네이버 DataLab Search API (트렌드 ratio) ---
CLIENT_ID = "MIozR5VgAMmErwYP6yjV"
CLIENT_SECRET = "sHDdopN8ix"
DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"

# --- 앵커/앵커월 (고정) ---
ANCHOR_MONTH_START = "2026-01-01"
ANCHOR_MONTH_END = "2026-01-31"

# 앵커 월간 '절대' 검색량
ANCHORS = [
    {"group": "anchor_tvn", "keyword": "tvN",      "monthly_volume": 47600},
    {"group": "anchor_nf",  "keyword": "넷플릭스", "monthly_volume": 2_353_200},
]


# =========================================================
# 1) 연관 검색어 추출 (네이버 자동완성 + 구글 트렌드 하이브리드)
# =========================================================

def fetch_naver_autocomplete(query: str) -> list:
    """
    네이버 자동완성 API를 호출하여 연관 검색어 리스트를 반환합니다.
    """
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
    """
    pytrends 라이브러리를 사용하여 구글 트렌드의 '관련 검색어'를 반환합니다.
    인기(top) 및 급상승(rising) 검색어를 모두 가져옵니다.
    """
    try:
        # pytrends 객체 초기화 (한국어, 한국 시간대)
        pytrend = TrendReq(hl='ko-KR', tz=540)
        
        # 페이로드 빌드 (최근 1개월 기준, 한국 지역)
        pytrend.build_payload(kw_list=[query], timeframe='today 1-m', geo='KR')
        related_payload = pytrend.related_queries()
        
        kws = []
        if query in related_payload and related_payload[query] is not None:
            # 인기 검색어 추출
            if 'top' in related_payload[query] and related_payload[query]['top'] is not None:
                kws.extend(related_payload[query]['top']['query'].tolist())
            # 급상승 검색어 추출
            if 'rising' in related_payload[query] and related_payload[query]['rising'] is not None:
                kws.extend(related_payload[query]['rising']['query'].tolist())
                
        return kws
    except Exception as e:
        print(f"구글 트렌드 연관어 오류: {e}")
        return []


def get_combined_related_keywords(seed_keyword: str) -> pd.DataFrame:
    """
    네이버 자동완성과 구글 트렌드 결과를 합쳐 중복을 제거한 DataFrame을 반환합니다.
    """
    naver_kws = fetch_naver_autocomplete(seed_keyword)
    google_kws = fetch_google_trends_related(seed_keyword)
    
    # 중복 제거 (순서 유지)
    combined = []
    for kw in naver_kws + google_kws:
        if kw not in combined:
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
    """
    백엔드 처리용: 지정된 start_date부터 end_date까지의 절대 검색량을 추정합니다.
    (앵커 계산을 위해 start_date는 최소 1월 1일 이전이어야 함)
    return:
      - date (YYYY-MM-DD)
      - total_abs_est (일자별 전체검색량 절대추정)
    """
    kw_group = f"kw_{seed_keyword}"

    keyword_groups = []
    for a in ANCHORS:
        keyword_groups.append({"groupName": a["group"], "keywords": [a["keyword"]]})
    keyword_groups.append({"groupName": kw_group, "keywords": [seed_keyword]})

    api = post_datalab(start_date, end_date, keyword_groups)
    piv = datalab_json_to_pivot(api)
    if piv.empty:
        raise RuntimeError("DataLab 결과가 비었습니다. 키워드/기간/인증 확인")

    # 앵커별 k
    k_map = {}
    for a in ANCHORS:
        k_map[a["group"]] = compute_k_from_anchor_month(piv, a["group"], a["monthly_volume"])

    # 키워드 절대량: 앵커별 추정 후 월간량 가중 결합
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
    """
    DataLab 그룹핑 호출을 통해 씨앗 키워드 트렌드 대비 연관어의 비율을 구하고, 
    씨앗 키워드의 기간 내 전체 절대 검색량을 곱하여 연관어의 검색량을 역산합니다.
    """
    if "keyword" not in related_df.columns:
        return pd.DataFrame(columns=["연관어", "전체 검색량"])

    kws = related_df["keyword"].dropna().astype(str).unique().tolist()
    kws = [k for k in kws if k != seed_keyword]

    results = []
    # DataLab 키워드 그룹은 최대 5개이므로, Seed + 연관어 4개씩 청크(Chunk)로 쪼개서 호출
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
            print(f"연관어 DataLab 오류 ({chunk}): {e}")
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
    """
    사용자가 라벨링한 연관어 CSV를 바탕으로,
    지정된 기간 동안의 데이터랩 트렌드를 조회하여 드라마 검색 의도 비중(p)을 계산합니다.
    """
    df = related_csv_df.copy()
    
    if "is_drama" not in df.columns or "keyword" not in df.columns:
        raise RuntimeError("연관어 CSV에 'keyword'와 'is_drama' 컬럼이 필요합니다.")

    df["is_drama"] = pd.to_numeric(df["is_drama"], errors="coerce")
    
    # 1(드라마), 0(비드라마) 키워드 분류 (데이터랩 그룹당 최대 20개 제한에 맞춰 슬라이싱)
    drama_kws = df[df["is_drama"] == 1]["keyword"].dropna().astype(str).tolist()[:20]
    nondrama_kws = df[df["is_drama"] == 0]["keyword"].dropna().astype(str).tolist()[:20]

    if not drama_kws and not nondrama_kws:
        return float("nan")
    if not drama_kws:
        return 0.0
    if not nondrama_kws:
        return 1.0

    # 데이터랩 조회를 위한 그룹 구성
    groups = []
    if drama_kws:
        groups.append({"groupName": "drama", "keywords": drama_kws})
    if nondrama_kws:
        groups.append({"groupName": "nondrama", "keywords": nondrama_kws})

    # 데이터랩 호출 및 피벗 변환
    api_res = post_datalab(start_date, end_date, groups)
    piv = datalab_json_to_pivot(api_res)

    if piv.empty:
        return float("nan")

    # 해당 기간 동안의 각 그룹별 트렌드 지수 합산
    drama_sum = float(piv["drama"].sum()) if "drama" in piv.columns else 0.0
    nondrama_sum = float(piv["nondrama"].sum()) if "nondrama" in piv.columns else 0.0

    denom = drama_sum + nondrama_sum
    if denom <= 0:
        return float("nan")
        
    return drama_sum / denom


# =========================================================
# 4) UI
# =========================================================

def safe_filename(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    return s[:120] if len(s) > 120 else s


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("드라마 검색량 도구 (연관어/전체/드라마의도)")
        self.geometry("900x580")

        self.keyword_var = tk.StringVar(value="세이렌")
        self.startdate_var = tk.StringVar(value="2026-02-01")  # 사용자가 원하는 시작일
        self.enddate_var = tk.StringVar(value="2026-03-05")

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 8}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True)

        box = ttk.LabelFrame(root, text="입력 (앱은 닫히지 않고 계속 유지됨)")
        box.pack(fill="x", **pad)

        ttk.Label(box, text="키워드").grid(row=0, column=0, sticky="w", padx=10, pady=8)
        ttk.Entry(box, textvariable=self.keyword_var, width=28).grid(row=0, column=1, sticky="w", padx=10, pady=8)

        # 시작일과 종료일 UI 배치
        ttk.Label(box, text="시작일 (YYYY-MM-DD)").grid(row=1, column=0, sticky="w", padx=10, pady=8)
        ttk.Entry(box, textvariable=self.startdate_var, width=18).grid(row=1, column=1, sticky="w", padx=10, pady=8)
        
        ttk.Label(box, text="종료일 (YYYY-MM-DD)").grid(row=1, column=2, sticky="w", padx=10, pady=8)
        ttk.Entry(box, textvariable=self.enddate_var, width=18).grid(row=1, column=3, sticky="w", padx=10, pady=8)

        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="연관어 추출하기", command=self.on_extract_related).pack(side="left", padx=10)
        ttk.Button(btns, text="검색량 추출하기", command=self.on_extract_volume).pack(side="left", padx=10)

        logbox = ttk.LabelFrame(root, text="로그")
        logbox.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logbox, wrap="word", height=18)
        self.log.pack(fill="both", expand=True, padx=10, pady=10)

        self._log(
            "사용법\n"
            "0) 터미널에서 'pip install pytrends openpyxl'을 먼저 실행해주세요. (다중 탭 엑셀 저장용)\n"
            "1) [연관어 추출하기] → 자동완성(네이버/구글트렌드) CSV 저장 → is_drama 컬럼에 1(드라마), 0(비드라마) 편집\n"
            "2) [검색량 추출하기] → 편집한 CSV 선택 → 일자별/주차별/연관어 엑셀(Excel) 생성\n"
        )

    def _log(self, msg: str):
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.update_idletasks()

    def _validate_inputs(self):
        kw = self.keyword_var.get().strip()
        startd = self.startdate_var.get().strip()
        endd = self.enddate_var.get().strip()

        if not kw:
            raise ValueError("키워드를 입력해줘.")
        dt.date.fromisoformat(startd)
        dt.date.fromisoformat(endd)
        if dt.date.fromisoformat(startd) > dt.date.fromisoformat(endd):
            raise ValueError("시작일이 종료일보다 늦습니다.")
        return kw, startd, endd

    def on_extract_related(self):
        try:
            seed, startd, endd = self._validate_inputs()
            self._log(f"[연관어] 시작 (네이버 자동완성 + 구글 트렌드): {seed}")

            df = get_combined_related_keywords(seed_keyword=seed)
            if df is None or df.empty:
                raise RuntimeError("연관어 결과가 비어있음(키워드 확인)")

            df = df.copy()
            df["is_drama"] = ""  # 사용자가 편집할 수 있도록 빈 컬럼 생성

            default_name = f"related_{safe_filename(seed)}_{now_stamp()}.csv"
            path = filedialog.asksaveasfilename(
                title="연관어 CSV 저장",
                defaultextension=".csv",
                initialfile=default_name,
                filetypes=[("CSV", "*.csv")],
            )
            if not path:
                self._log("[연관어] 저장 취소")
                return

            df.to_csv(path, index=False, encoding="utf-8-sig")
            self._log(f"[연관어] 저장 완료: {path}")
            self._log("→ is_drama 컬럼에 1/0 라벨링 후, [검색량 추출하기]에서 이 파일을 선택해.")

        except Exception as e:
            messagebox.showerror("오류", str(e))
            self._log(f"[연관어] ERROR: {e}")

    def on_extract_volume(self):
        try:
            seed, startd, endd = self._validate_inputs()
            self._log(f"[검색량] 시작: {seed}, 기간: {startd} ~ {endd}")

            rel_path = filedialog.askopenfilename(
                title="편집한 연관어 CSV 선택 (is_drama 포함)",
                filetypes=[("CSV", "*.csv")],
            )
            if not rel_path:
                self._log("[검색량] CSV 선택 취소")
                return

            related = pd.read_csv(rel_path)
            
            # 데이터랩을 활용하여 기간 맞춤형 p 계산
            p = compute_drama_share_p_via_datalab(related, startd, endd)
            if pd.isna(p):
                raise RuntimeError("드라마 비중 p 계산 실패. is_drama에 1/0 라벨을 더 입력해줘.")
            self._log(f"[검색량] 드라마 의도 비중={p:.4f} (조회 기간 트렌드 기준)")

            # 백엔드 호출용 시작일 결정: 앵커 계산을 위해 사용자가 지정한 시작일과 1월 1일 중 더 빠른 날짜를 사용
            user_start_dt = pd.to_datetime(startd)
            anchor_start_dt = pd.to_datetime(ANCHOR_MONTH_START)
            fetch_start_str = min(user_start_dt, anchor_start_dt).strftime("%Y-%m-%d")

            # 데이터랩 API 호출 및 절대검색량 추정 (1월 데이터 보장)
            total_df = estimate_total_abs_timeseries(seed, fetch_start_str, endd)

            # 사용자에게 보여줄 구간 자르기 (사용자가 설정한 startd부터 endd까지)
            total_df["date_dt"] = pd.to_datetime(total_df["date"])
            total_df = total_df[
                (total_df["date_dt"] >= user_start_dt) &
                (total_df["date_dt"] <= pd.to_datetime(endd))
            ].copy()
            
            # 쿼리 볼륨 계산
            total_df["전체 쿼리"] = total_df["total_abs_est"]
            total_df["드라마 의도 쿼리"] = total_df["total_abs_est"] * p
            period_total_abs = total_df["전체 쿼리"].sum()

            # --- 시트 1: 요약 ---
            summary_df = pd.DataFrame({
                "항목": ["분석 키워드", "조회 기간", "드라마 의도 비중"],
                "내용": [seed, f"{startd} ~ {endd}", f"{p:.4f}"]
            })

            # --- 시트 2: 일자별 결과 ---
            daily_df = total_df[["date", "전체 쿼리", "드라마 의도 쿼리"]].copy()
            daily_df.columns = ["날짜", "전체 쿼리", "드라마 의도 쿼리"]
            daily_df["전체 쿼리"] = daily_df["전체 쿼리"].apply(lambda x: f"{int(x):,}")
            daily_df["드라마 의도 쿼리"] = daily_df["드라마 의도 쿼리"].apply(lambda x: f"{int(x):,}")

            # --- 시트 3: 주차별 결과 (월요일 기준 병합) ---
            weekly_calc = total_df.copy()
            # dayofweek: 월요일=0 ~ 일요일=6
            weekly_calc['week_start'] = weekly_calc['date_dt'] - pd.to_timedelta(weekly_calc['date_dt'].dt.dayofweek, unit='d')
            weekly_grouped = weekly_calc.groupby('week_start')[['전체 쿼리', '드라마 의도 쿼리']].sum().reset_index()
            # "M월D일주차" 형식으로 라벨링
            weekly_grouped['주차'] = weekly_grouped['week_start'].dt.month.astype(str) + "월" + weekly_grouped['week_start'].dt.day.astype(str) + "일주차"
            
            weekly_df = weekly_grouped[['주차', '전체 쿼리', '드라마 의도 쿼리']].copy()
            weekly_df["전체 쿼리"] = weekly_df["전체 쿼리"].apply(lambda x: f"{int(x):,}")
            weekly_df["드라마 의도 쿼리"] = weekly_df["드라마 의도 쿼리"].apply(lambda x: f"{int(x):,}")

            # --- 시트 4: 연관어 ---
            self._log(f"[검색량] 연관어 볼륨 추출 중... (약간의 시간이 소요될 수 있습니다)")
            related_abs_df = calculate_related_kws_volume(seed, related, startd, endd, period_total_abs)
            if not related_abs_df.empty:
                related_abs_df["전체 검색량"] = related_abs_df["전체 검색량"].apply(lambda x: f"{int(x):,}")

            # --- 엑셀 저장 ---
            default_name = f"search_{safe_filename(seed)}_{now_stamp()}.xlsx"
            path = filedialog.asksaveasfilename(
                title="검색량 결과 엑셀 저장",
                defaultextension=".xlsx",
                initialfile=default_name,
                filetypes=[("Excel", "*.xlsx")],
            )
            if not path:
                self._log("[검색량] 저장 취소")
                return

            try:
                with pd.ExcelWriter(path, engine="openpyxl") as writer:
                    summary_df.to_excel(writer, sheet_name="요약", index=False)
                    daily_df.to_excel(writer, sheet_name="일자별 결과", index=False)
                    weekly_df.to_excel(writer, sheet_name="주차별 결과", index=False)
                    related_abs_df.to_excel(writer, sheet_name="연관어", index=False)
                self._log(f"[검색량] 저장 완료: {path}")
            except ModuleNotFoundError:
                raise RuntimeError("엑셀 저장을 위해 'openpyxl' 라이브러리가 필요합니다. 터미널에서 'pip install openpyxl'을 실행해주세요.")

        except Exception as e:
            messagebox.showerror("오류", str(e))
            self._log(f"[검색량] ERROR: {e}")


if __name__ == "__main__":
    app = App()
    app.mainloop()