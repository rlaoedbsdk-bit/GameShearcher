"""모바일 게임 사전 시장조사 — 무료 버전 v2
변경점:
- 경쟁작 수집 대폭 확대 (키워드당 25개 검색, 최대 60개 앱, 병렬 상세조회)
- 구글 트렌드 제거 → 스토어 실데이터 기반 시장 차트(설치 구간/출시 연도/평점 분포)로 교체
- 네이버 데이터랩: 앵커('게임') 보정 방식으로 연령·성별 비교가 성립하도록 재구현
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import requests as rq
import streamlit as st

def secret(name, default=""):
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:
        return os.getenv(name, default)

TEAM_ACCESS_TOKEN = secret("TEAM_ACCESS_TOKEN", "")
NAVER_CLIENT_ID = secret("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = secret("NAVER_CLIENT_SECRET")
ANCHOR_KW = "게임"  # 네이버 스케일 보정용 기준 키워드

st.set_page_config(page_title="모바일 게임 시장조사 (무료)", page_icon="🎮", layout="wide")
st.title("🎮 모바일 게임 사전 시장조사 — 무료 v2")
st.caption("스토어 실데이터 중심. 설치 수는 구간값이며, 모든 수치는 원본 대조 가능합니다.")

if TEAM_ACCESS_TOKEN:
    pw = st.sidebar.text_input("팀 비밀번호", type="password")
    if pw != TEAM_ACCESS_TOKEN:
        st.info("👈 사이드바에 팀 비밀번호를 입력하세요.")
        st.stop()

# ================= 수집기 =================
@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_google_play(keywords: tuple, region, lang, per_kw=25, max_apps=60,
                        review_apps=10, reviews_n=20):
    from google_play_scraper import Sort, app as gp_app, reviews as gp_reviews, search as gp_search

    # 1) 키워드별 검색으로 후보 앱 ID 수집 (중복 제거)
    candidates = {}
    for kw in keywords:
        try:
            for r in gp_search(kw, lang=lang, country=region, n_hits=per_kw):
                aid = r.get("appId")
                if aid and aid not in candidates:
                    candidates[aid] = kw
        except Exception:
            continue

    # 2) 상세정보 병렬 조회 (속도 확보)
    def fetch_detail(aid, kw):
        try:
            d = gp_app(aid, lang=lang, country=region)
            return {"app_id": aid, "앱 이름": d.get("title"), "스토어": "Google Play",
                    "장르": d.get("genre"), "평점": round(d.get("score") or 0, 2),
                    "평가 수": d.get("ratings") or 0, "설치 구간": d.get("installs"),
                    "인앱결제": "O" if d.get("offersIAP") else "X",
                    "출시일": str(d.get("released") or ""),
                    "개발사": d.get("developer"), "검색 키워드": kw}
        except Exception:
            return None

    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch_detail, aid, kw)
                   for aid, kw in list(candidates.items())[:max_apps * 2]]
        for f in as_completed(futures):
            r = f.result()
            if r:
                rows.append(r)

    df = pd.DataFrame(rows)
    if df.empty:
        return df, pd.DataFrame()
    df = df.sort_values("평가 수", ascending=False, na_position="last").head(max_apps)

    # 3) 상위 앱만 리뷰 수집 (병렬)
    top_ids = df.head(review_apps)[["app_id", "앱 이름"]].values.tolist()

    def fetch_reviews(aid, title):
        try:
            revs, _ = gp_reviews(aid, lang=lang, country=region,
                                 sort=Sort.MOST_RELEVANT, count=reviews_n)
            return [{"앱 이름": title, "별점": v["score"], "리뷰": v["content"][:300]}
                    for v in revs if v.get("content")]
        except Exception:
            return []

    reviews = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for f in as_completed([ex.submit(fetch_reviews, a, t) for a, t in top_ids]):
            reviews.extend(f.result())

    return df.drop(columns=["app_id"]), pd.DataFrame(reviews)

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_app_store(keywords: tuple, region, per_kw=25, max_apps=60):
    seen = {}
    for kw in keywords:
        try:
            r = rq.get("https://itunes.apple.com/search", params={
                "term": kw, "country": region, "entity": "software",
                "genreId": 6014, "limit": min(per_kw, 50)}, timeout=15)
            results = r.json().get("results", [])
        except Exception:
            results = []
        for a in results:
            aid = a.get("trackId")
            if not aid or aid in seen:
                continue
            seen[aid] = {"앱 이름": a.get("trackName"), "스토어": "App Store",
                         "장르": ", ".join(a.get("genres", [])[:2]),
                         "평점": round(a.get("averageUserRating") or 0, 2),
                         "평가 수": a.get("userRatingCount") or 0,
                         "설치 구간": "-", "인앱결제": "-",
                         "출시일": (a.get("releaseDate") or "")[:10],
                         "개발사": a.get("artistName"), "검색 키워드": kw}
    df = pd.DataFrame(seen.values())
    if not df.empty:
        df = df.sort_values("평가 수", ascending=False, na_position="last").head(max_apps)
    return df

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_naver_anchored(keywords: tuple):
    """앵커 보정: 모든 조회에 ANCHOR_KW를 포함시켜 조회 간 스케일을 통일.
    결과값 = (키워드 평균 관심도 / 앵커 평균 관심도) * 100
    → '게임'이라는 공통 기준 대비 상대지수라 연령/성별 간 비교가 성립."""
    if not NAVER_CLIENT_ID:
        return None
    AGE = {"2": "13-18", "3": "19-24", "4": "25-29", "5": "30-34",
           "6": "35-39", "7": "40-44", "8": "45-49", "9": "50-54"}

    def q(ages=None, gender=None):
        end = date.today(); start = end - timedelta(days=365)
        groups = [{"groupName": ANCHOR_KW, "keywords": [ANCHOR_KW]}] + \
                 [{"groupName": k, "keywords": [k]} for k in list(keywords)[:4]]
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
                "timeUnit": "month", "keywordGroups": groups}
        if ages: body["ages"] = ages
        if gender: body["gender"] = gender
        r = rq.post("https://openapi.naver.com/v1/datalab/search",
                    headers={"X-Naver-Client-Id": NAVER_CLIENT_ID,
                             "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                             "Content-Type": "application/json"},
                    data=json.dumps(body), timeout=15)
        r.raise_for_status()
        return r.json()

    def relative_index(data):
        avgs = {}
        for g in data.get("results", []):
            vals = [p["ratio"] for p in g.get("data", [])]
            avgs[g["title"]] = (sum(vals) / len(vals)) if vals else 0
        anchor = avgs.get(ANCHOR_KW, 0)
        if anchor <= 0:
            return {}
        return {k: round(v / anchor * 100, 1) for k, v in avgs.items() if k != ANCHOR_KW}

    rows = []
    try:
        for code, label in AGE.items():
            for kw, idx in relative_index(q(ages=[code])).items():
                rows.append({"키워드": kw, "축": "연령", "구분": label, "상대지수": idx})
        for gd, name in (("m", "남성"), ("f", "여성")):
            for kw, idx in relative_index(q(gender=gd)).items():
                rows.append({"키워드": kw, "축": "성별", "구분": name, "상대지수": idx})
    except Exception as e:
        return pd.DataFrame({"오류": [str(e)]})
    return pd.DataFrame(rows)

# ================= 스토어 파생 차트 =================
def installs_to_num(s):
    if not isinstance(s, str):
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None

def market_charts(comp: pd.DataFrame):
    gp = comp[comp["스토어"] == "Google Play"].copy()
    c1, c2 = st.columns(2)

    with c1:
        st.subheader("설치 구간 분포 (Google Play)")
        if not gp.empty and gp["설치 구간"].notna().any():
            gp["_n"] = gp["설치 구간"].map(installs_to_num)
            order = gp.dropna(subset=["_n"]).sort_values("_n")
            dist = order.groupby("설치 구간", sort=False).size()
            st.bar_chart(dist)
            big = (gp["_n"] >= 1_000_000).sum()
            st.caption(f"수집된 경쟁작 중 100만+ 설치 구간: {big}개 — 많을수록 검증된 수요가 있으나 경쟁 강도도 높음")
        else:
            st.caption("데이터 부족")

    with c2:
        st.subheader("출시 연도 분포 (시장 활성도)")
        comp2 = comp.copy()
        comp2["출시연도"] = comp2["출시일"].astype(str).str.extract(r"(20\d{2})")
        years = comp2["출시연도"].dropna()
        if not years.empty:
            st.bar_chart(years.value_counts().sort_index())
            recent = (years.astype(int) >= 2024).sum()
            st.caption(f"2024년 이후 출시작 {recent}개 — 최근 출시가 많으면 아직 신규 진입이 활발한 시장")
        else:
            st.caption("데이터 부족")

# ================= 입력 폼 =================
with st.form("research"):
    c1, c2, c3 = st.columns([3, 1, 1])
    kw_text = c1.text_input("검색 키워드 (쉼표 구분, 2~4개 권장)",
                            placeholder="예: 방치형 게임, 키우기 게임, idle RPG")
    region = c2.selectbox("국가", ["kr", "us", "jp", "tw", "id", "vn", "th", "de", "br"])
    platform = c3.selectbox("플랫폼", ["둘 다", "Android", "iOS"])
    target = st.text_input("타겟층 메모 (AI 분석 시 해석 관점으로 사용, 선택)",
                           placeholder="예: 20대 여성, 10분 내외 짧은 세션, 힐링·수집 선호, 소과금")
    submitted = st.form_submit_button("조사 시작", type="primary")

if submitted and kw_text.strip():
    keywords = tuple(k.strip() for k in kw_text.split(",") if k.strip())[:4]
    lang = "ko" if region == "kr" else "en"

    with st.spinner("스토어 실데이터 수집 중... (앱 수가 늘어 첫 조사는 2~4분)"):
        gp_df, reviews_df, as_df = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        if platform in ("둘 다", "Android"):
            gp_df, reviews_df = collect_google_play(keywords, region, lang)
        if platform in ("둘 다", "iOS"):
            as_df = collect_app_store(keywords, region)
        naver_df = collect_naver_anchored(keywords) if region == "kr" else None

    comp = pd.concat([gp_df, as_df], ignore_index=True) if not (gp_df.empty and as_df.empty) else pd.DataFrame()
    st.success(f"수집 완료 — 경쟁작 {len(comp)}개, 리뷰 {len(reviews_df)}건 (24시간 캐시)")

    st.header("1. 경쟁작 현황")
    if not comp.empty:
        st.dataframe(comp, use_container_width=True, hide_index=True, height=480)
        st.download_button("CSV 다운로드", comp.to_csv(index=False).encode("utf-8-sig"), "competitors.csv")
    else:
        st.warning("경쟁작을 찾지 못했습니다. 키워드를 더 일반적인 표현으로 바꿔보세요.")

    if not comp.empty:
        st.header("2. 시장 구조 (스토어 실데이터 기반)")
        market_charts(comp)

    if naver_df is not None:
        st.header("3. 한국 연령·성별 관심 상대지수 (네이버 데이터랩, 앵커 보정)")
        if "오류" in getattr(naver_df, "columns", []):
            st.warning(f"네이버 수집 실패: {naver_df['오류'][0]}")
        elif naver_df is not None and not naver_df.empty:
            st.caption(f"각 값은 해당 집단에서 '{ANCHOR_KW}' 검색 관심도를 100으로 놓았을 때의 상대지수입니다. "
                       "집단 간 비교가 가능하도록 보정한 값이며, 절대 검색량이 아닙니다.")
            age_df = naver_df[naver_df["축"] == "연령"].pivot_table(index="구분", columns="키워드", values="상대지수")
            gen_df = naver_df[naver_df["축"] == "성별"].pivot_table(index="구분", columns="키워드", values="상대지수")
            cc1, cc2 = st.columns([2, 1])
            with cc1:
                st.subheader("연령대별")
                st.bar_chart(age_df)
            with cc2:
                st.subheader("성별")
                st.bar_chart(gen_df)
            st.caption("주의: 네이버 검색자 기준이므로 저연령·해외 유저는 과소 반영될 수 있고, 검색자≠플레이어입니다. "
                       "타겟 확정이 아니라 가설 점검용으로 사용하세요.")
    elif region == "kr":
        st.info("네이버 데이터랩 키를 설정하면 연령·성별 상대지수가 추가됩니다.")

    st.header("4. 유저 리뷰 샘플 (상위 앱)")
    if not reviews_df.empty:
        low = reviews_df[reviews_df["별점"] <= 2]
        t1, t2 = st.tabs([f"전체 ({len(reviews_df)})", f"불만 리뷰만 (별점≤2, {len(low)})"])
        with t1:
            st.dataframe(reviews_df, use_container_width=True, hide_index=True, height=300)
        with t2:
            st.dataframe(low, use_container_width=True, hide_index=True, height=300)

    st.header("5. AI 분석 리포트 받기 (무료)")
    st.markdown("아래 파일을 받아 내용을 **claude.ai 채팅에 붙여넣으면** 분석 리포트를 받을 수 있습니다.")
    data_summary = {
        "조사 키워드": list(keywords), "국가": region, "플랫폼": platform,
        "타겟층 메모": target or "미지정",
        "경쟁작": comp.to_dict("records") if not comp.empty else [],
        "리뷰 샘플": reviews_df.head(80).to_dict("records") if not reviews_df.empty else [],
        "네이버_상대지수(게임=100)": (naver_df.to_dict("records")
                                      if naver_df is not None and not naver_df.empty
                                      and "오류" not in naver_df.columns else []),
    }
    analysis_prompt = f"""당신은 모바일 게임 퍼블리셔의 시니어 시장분석가입니다.
아래 실데이터만 근거로 분석하고 데이터에 없는 수치는 지어내지 마세요.
설치 수는 구간값, 네이버 지수는 '게임'=100 기준 상대지수(검색자 기준, 검색자≠플레이어)임을 감안하세요.

한국어 리포트 구조:
## 요약(3줄) / ## 경쟁 구도(밀도·강자·빈틈) / ## 유저 리뷰 인사이트(반복 불만→차별화 기회)
## 타겟층 적합성 평가(메모의 가설 vs 데이터) / ## 진입 리스크와 기회 / ## 포지셔닝 제언(3~5개) / ## 데이터 한계

[수집 데이터]
{json.dumps(data_summary, ensure_ascii=False, indent=1)[:50000]}"""
    st.download_button("📋 분석용 전체 텍스트 다운로드 (.txt)", analysis_prompt, "analysis_prompt.txt")

