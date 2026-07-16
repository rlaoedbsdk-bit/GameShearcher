"""모바일 게임 사전 시장조사 웹앱 — 100% 무료 버전
API 키·결제 불필요. 실데이터(구글플레이/앱스토어/구글트렌드/네이버) 수집 후
표·차트로 정리하고, AI 분석은 claude.ai에 붙여넣을 수 있는 텍스트로 생성.
"""
import json
import os
from datetime import date, timedelta

import pandas as pd
import requests as rq
import streamlit as st

def secret(name, default=""):
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:
        return os.getenv(name, default)

TEAM_ACCESS_TOKEN = secret("TEAM_ACCESS_TOKEN", "")  # 비워두면 비밀번호 없이 사용
NAVER_CLIENT_ID = secret("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = secret("NAVER_CLIENT_SECRET")

st.set_page_config(page_title="모바일 게임 시장조사 (무료)", page_icon="🎮", layout="wide")
st.title("🎮 모바일 게임 사전 시장조사 — 무료 버전")
st.caption("스토어·트렌드 실데이터를 수집해 정리합니다. API 키·결제 불필요.")

# ---------- 팀 비밀번호 (설정된 경우에만) ----------
if TEAM_ACCESS_TOKEN:
    pw = st.sidebar.text_input("팀 비밀번호", type="password")
    if pw != TEAM_ACCESS_TOKEN:
        st.info("👈 사이드바에 팀 비밀번호를 입력하세요.")
        st.stop()

# ---------- 수집기 ----------
@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_google_play(keywords: tuple, region, lang, per_kw=6, reviews_n=20):
    from google_play_scraper import Sort, app as gp_app, reviews as gp_reviews, search as gp_search
    seen, reviews_bag = {}, []
    for kw in keywords:
        try:
            results = gp_search(kw, lang=lang, country=region, n_hits=per_kw)
        except Exception:
            results = []
        for r in results:
            aid = r.get("appId")
            if not aid or aid in seen:
                continue
            try:
                d = gp_app(aid, lang=lang, country=region)
                seen[aid] = {"앱 이름": d.get("title"), "스토어": "Google Play",
                             "장르": d.get("genre"), "평점": d.get("score"),
                             "평가 수": d.get("ratings"), "설치 구간": d.get("installs"),
                             "인앱결제": "O" if d.get("offersIAP") else "X",
                             "출시일": str(d.get("released") or ""),
                             "개발사": d.get("developer"), "검색 키워드": kw}
                try:
                    revs, _ = gp_reviews(aid, lang=lang, country=region,
                                         sort=Sort.MOST_RELEVANT, count=reviews_n)
                    for v in revs:
                        if v.get("content"):
                            reviews_bag.append({"앱 이름": d.get("title"),
                                                "별점": v["score"],
                                                "리뷰": v["content"][:300]})
                except Exception:
                    pass
            except Exception:
                continue
    df = pd.DataFrame(seen.values())
    if not df.empty:
        df = df.sort_values("평가 수", ascending=False, na_position="last").head(20)
    return df, pd.DataFrame(reviews_bag)

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_app_store(keywords: tuple, region, per_kw=6):
    seen = {}
    for kw in keywords:
        try:
            r = rq.get("https://itunes.apple.com/search", params={
                "term": kw, "country": region, "entity": "software",
                "genreId": 6014, "limit": per_kw}, timeout=15)
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
                         "평가 수": a.get("userRatingCount"),
                         "가격": a.get("formattedPrice"),
                         "출시일": (a.get("releaseDate") or "")[:10],
                         "개발사": a.get("artistName"), "검색 키워드": kw}
    df = pd.DataFrame(seen.values())
    if not df.empty:
        df = df.sort_values("평가 수", ascending=False, na_position="last").head(20)
    return df

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_trends(keywords: tuple, region):
    try:
        from pytrends.request import TrendReq
        py = TrendReq(hl="ko", tz=540)
        py.build_payload(list(keywords)[:5], timeframe="today 12-m", geo=region.upper())
        df = py.interest_over_time()
        if not df.empty and "isPartial" in df.columns:
            df = df.drop(columns=["isPartial"])
        return df
    except Exception as e:
        return pd.DataFrame({"오류": [str(e)]})

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_naver(keywords: tuple):
    if not NAVER_CLIENT_ID:
        return None
    AGE = {"2": "13-18", "3": "19-24", "4": "25-29", "5": "30-34",
           "6": "35-39", "7": "40-44", "8": "45-49", "9": "50-54"}
    def q(ages=None, gender=None):
        end = date.today(); start = end - timedelta(days=365)
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
                "timeUnit": "month",
                "keywordGroups": [{"groupName": k, "keywords": [k]} for k in list(keywords)[:5]]}
        if ages: body["ages"] = ages
        if gender: body["gender"] = gender
        r = rq.post("https://openapi.naver.com/v1/datalab/search",
                    headers={"X-Naver-Client-Id": NAVER_CLIENT_ID,
                             "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                             "Content-Type": "application/json"},
                    data=json.dumps(body), timeout=15)
        r.raise_for_status()
        return r.json()
    rows = []
    try:
        for code, label in AGE.items():
            for g in q(ages=[code]).get("results", []):
                vals = [p["ratio"] for p in g.get("data", [])]
                rows.append({"키워드": g["title"], "구분": f"연령 {label}",
                             "평균 관심도": round(sum(vals) / len(vals), 1) if vals else 0})
        for gd, name in (("m", "남성"), ("f", "여성")):
            for g in q(gender=gd).get("results", []):
                vals = [p["ratio"] for p in g.get("data", [])]
                rows.append({"키워드": g["title"], "구분": f"성별 {name}",
                             "평균 관심도": round(sum(vals) / len(vals), 1) if vals else 0})
    except Exception as e:
        return pd.DataFrame({"오류": [str(e)]})
    return pd.DataFrame(rows)

# ---------- 입력 폼 ----------
with st.form("research"):
    c1, c2, c3 = st.columns([3, 1, 1])
    kw_text = c1.text_input("검색 키워드 (쉼표로 구분, 2~5개)",
                            placeholder="예: 방치형 게임, 힐링 게임, 캐주얼 퍼즐")
    region = c2.selectbox("국가", ["kr", "us", "jp", "tw", "id", "vn", "th", "de", "br"], index=0)
    platform = c3.selectbox("플랫폼", ["둘 다", "Android", "iOS"])
    target = st.text_input("타겟층 메모 (분석 프롬프트에 포함됨, 선택)",
                           placeholder="예: 20대 여성, 짧은 플레이 세션 선호")
    submitted = st.form_submit_button("조사 시작", type="primary")

if submitted and kw_text.strip():
    keywords = tuple(k.strip() for k in kw_text.split(",") if k.strip())[:5]
    lang = "ko" if region == "kr" else "en"

    with st.spinner("스토어·트렌드 실데이터 수집 중... (첫 조사 1~3분)"):
        gp_df, reviews_df = (pd.DataFrame(), pd.DataFrame())
        as_df = pd.DataFrame()
        if platform in ("둘 다", "Android"):
            gp_df, reviews_df = collect_google_play(keywords, region, lang)
        if platform in ("둘 다", "iOS"):
            as_df = collect_app_store(keywords, region)
        trends_df = collect_trends(keywords, region)
        naver_df = collect_naver(keywords) if region == "kr" else None

    st.success("수집 완료. 같은 조건 재조사는 24시간 동안 즉시 표시됩니다.")

    # ---------- 결과 표시 ----------
    st.header("1. 경쟁작 현황 (실데이터)")
    comp = pd.concat([gp_df, as_df], ignore_index=True) if not (gp_df.empty and as_df.empty) else pd.DataFrame()
    if not comp.empty:
        st.dataframe(comp, use_container_width=True, hide_index=True)
        st.download_button("경쟁작 표 다운로드 (CSV)", comp.to_csv(index=False).encode("utf-8-sig"),
                           "competitors.csv")
    else:
        st.warning("경쟁작 데이터를 찾지 못했습니다. 키워드를 바꿔보세요.")

    st.header("2. 검색 관심도 추이 — 최근 12개월 (구글 트렌드)")
    if "오류" in trends_df.columns:
        st.warning(f"트렌드 수집 실패: {trends_df['오류'][0]} — 잠시 후 다시 시도하세요.")
    elif not trends_df.empty:
        st.line_chart(trends_df)

    if naver_df is not None:
        st.header("3. 한국 연령·성별 관심도 (네이버 데이터랩)")
        if "오류" in naver_df.columns:
            st.warning(f"네이버 수집 실패: {naver_df['오류'][0]}")
        elif not naver_df.empty:
            pivot = naver_df.pivot_table(index="구분", columns="키워드", values="평균 관심도")
            st.bar_chart(pivot)
            st.dataframe(naver_df, use_container_width=True, hide_index=True)
    elif region == "kr":
        st.info("네이버 데이터랩 키를 설정하면 연령·성별 관심도 데이터가 추가됩니다 (무료 발급).")

    st.header("4. 유저 리뷰 샘플 (구글플레이)")
    if not reviews_df.empty:
        st.dataframe(reviews_df, use_container_width=True, hide_index=True, height=300)

    # ---------- claude.ai 붙여넣기용 분석 프롬프트 ----------
    st.header("5. AI 분석 리포트 받기 (무료)")
    st.markdown("아래 텍스트를 복사해서 **claude.ai 채팅에 붙여넣으면** 분석 리포트를 받을 수 있습니다.")

    data_summary = {
        "조사 키워드": list(keywords), "국가": region, "플랫폼": platform,
        "타겟층 메모": target or "미지정",
        "경쟁작": comp.to_dict("records") if not comp.empty else [],
        "리뷰 샘플": reviews_df.head(60).to_dict("records") if not reviews_df.empty else [],
        "트렌드_월별": (trends_df.tail(12).reset_index().astype(str).to_dict("records")
                        if not trends_df.empty and "오류" not in trends_df.columns else []),
        "네이버_연령성별": (naver_df.to_dict("records")
                            if naver_df is not None and not naver_df.empty
                            and "오류" not in naver_df.columns else []),
    }
    analysis_prompt = f"""당신은 모바일 게임 퍼블리셔의 시니어 시장분석가입니다.
아래는 스토어와 트렌드에서 수집한 실데이터입니다. 이 데이터만을 근거로 분석하고,
데이터에 없는 수치는 지어내지 마세요. 설치 수는 구간값임을 감안하세요.

다음 구조의 한국어 리포트를 작성해주세요:
## 요약(3줄) / ## 시장 개요 / ## 경쟁작 분석 / ## 유저 리뷰 인사이트(불만·만족)
## 타겟층 적합성 평가 / ## 진입 리스크와 기회 / ## 포지셔닝 제언(3~5개) / ## 데이터 한계

[수집 데이터]
{json.dumps(data_summary, ensure_ascii=False, indent=1)[:50000]}"""

    st.code(analysis_prompt[:2000] + "\n... (전체는 아래 버튼으로 복사)", language=None)
    st.download_button("📋 분석용 전체 텍스트 다운로드 (.txt) — claude.ai에 붙여넣기",
                       analysis_prompt, "analysis_prompt.txt")
    st.caption("우측 상단 복사 아이콘으로 미리보기 일부만 복사되니, 전체는 .txt 다운로드 후 내용을 붙여넣으세요.")
