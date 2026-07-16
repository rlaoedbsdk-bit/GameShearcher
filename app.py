"""모바일 게임 사전 시장조사 — 무료 v3 (실무 재설계)
설계 원칙:
- 조사 목적별 2모드 분리: 데이터가 유효한 맥락에서만 보여준다
  · 시장/장르 조사: 진입 여부·포지셔닝 판단용
  · 특정 게임 딥다이브: 벤치마킹·업데이트 방향 판단용
- 모든 차트는 정직한 이름 + 표본 한계 명시
- 각 섹션에 '실무 해석 가이드' 내장 (팀원 누구나 같은 기준으로 읽게)
"""
import json
import os
import re
from collections import Counter
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
ANCHOR_KW = "게임"

st.set_page_config(page_title="모바일 게임 시장조사", page_icon="🎮", layout="wide")

if TEAM_ACCESS_TOKEN:
    pw = st.sidebar.text_input("팀 비밀번호", type="password")
    if pw != TEAM_ACCESS_TOKEN:
        st.info("👈 사이드바에 팀 비밀번호를 입력하세요.")
        st.stop()

# ==================== 공용 수집기 ====================
@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def expand_queries(keywords: tuple, region: str, lang: str, cap: int = 12) -> tuple:
    qs = list(keywords)
    for kw in keywords:
        sufs = (" 게임", " 신작", " 인기") if lang == "ko" else (" game", " games", " new")
        for suf in sufs:
            if suf.strip().lower() not in kw.lower():
                qs.append(kw + suf)
    for kw in keywords:
        try:
            r = rq.get("https://market.android.com/suggest/SuggRequest",
                       params={"json": 1, "c": 3, "query": kw, "hl": lang, "gl": region}, timeout=8)
            for s in r.json():
                if s.get("s"):
                    qs.append(s["s"])
        except Exception:
            pass
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q); out.append(q)
    return tuple(out[:cap])

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def collect_google_play(queries: tuple, region, lang, max_apps=60, review_apps=10, reviews_n=20):
    from google_play_scraper import Sort, app as gp_app, reviews as gp_reviews, search as gp_search
    candidates = {}
    for kw in queries:
        try:
            for r in gp_search(kw, lang=lang, country=region, n_hits=30):
                aid = r.get("appId")
                if aid and aid not in candidates:
                    candidates[aid] = kw
        except Exception:
            continue

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
        for f in as_completed([ex.submit(fetch_detail, a, k)
                               for a, k in list(candidates.items())[:max_apps * 3]]):
            r = f.result()
            if r:
                rows.append(r)
    df = pd.DataFrame(rows)
    if df.empty:
        return df, pd.DataFrame()
    df = df.sort_values("평가 수", ascending=False, na_position="last").head(max_apps)

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
def collect_app_store(keywords: tuple, region, max_apps=60):
    seen = {}
    for kw in keywords:
        try:
            r = rq.get("https://itunes.apple.com/search", params={
                "term": kw, "country": region, "entity": "software",
                "genreId": 6014, "limit": 100}, timeout=15)
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

    def rel(data):
        avgs = {}
        for g in data.get("results", []):
            vals = [p["ratio"] for p in g.get("data", [])]
            avgs[g["title"]] = (sum(vals) / len(vals)) if vals else 0
        anchor = avgs.get(ANCHOR_KW, 0)
        return {} if anchor <= 0 else \
            {k: round(v / anchor * 100, 1) for k, v in avgs.items() if k != ANCHOR_KW}

    rows = []
    try:
        for code, label in AGE.items():
            for kw, idx in rel(q(ages=[code])).items():
                rows.append({"키워드": kw, "축": "연령", "구분": label, "상대지수": idx})
        for gd, name in (("m", "남성"), ("f", "여성")):
            for kw, idx in rel(q(gender=gd)).items():
                rows.append({"키워드": kw, "축": "성별", "구분": name, "상대지수": idx})
    except Exception as e:
        return pd.DataFrame({"오류": [str(e)]})
    return pd.DataFrame(rows)

def _play_search_ids(name: str, region, lang):
    """구글플레이 검색 페이지 HTML에서 앱 ID를 직접 추출.
    라이브러리 search()는 정확 일치 시 뜨는 상단 '대표 카드'를 놓치는 맹점이 있어,
    페이지를 직접 읽어 대표 카드를 포함한 전체 노출 앱을 잡는다."""
    try:
        r = rq.get("https://play.google.com/store/search",
                   params={"q": name, "c": "apps", "hl": lang, "gl": region},
                   headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                   timeout=15)
        ids = re.findall(r"/store/apps/details\?id=([\w\.]+)", r.text)
        seen, out = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i); out.append(i)
        return out[:10]
    except Exception:
        return []

@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def search_game_candidates(name: str, region, lang):
    """직접 파싱(대표 카드 포함) + 라이브러리 검색을 병합한 뒤 상세 페이지를 사전 검증.
    확정 404만 제외하고 일시 오류는 '미확인'으로 목록에 유지."""
    from google_play_scraper import app as gp_app, search as gp_search
    from google_play_scraper.exceptions import NotFoundError

    merged, seen = [], set()
    for aid in _play_search_ids(name, region, lang):  # 대표 카드가 맨 앞에 오도록 먼저
        merged.append({"appId": aid}); seen.add(aid)
    try:
        for r in gp_search(name, lang=lang, country=region, n_hits=10):
            if r.get("appId") and r["appId"] not in seen:
                merged.append(r); seen.add(r["appId"])
    except Exception:
        pass
    merged = merged[:12]
    if not merged:
        return []

    def validate(r):
        for lg, ct in ((lang, region), ("en", "us")):
            try:
                d = gp_app(r["appId"], lang=lg, country=ct)
                return {"appId": r["appId"], "title": d.get("title"),
                        "developer": d.get("developer", ""),
                        "installs": d.get("installs", "?"),
                        "score": round(d.get("score") or 0, 2),
                        "lang": lg, "country": ct}
            except NotFoundError:
                continue  # 이 조합에선 확정 404 → 다음 조합 시도
            except Exception:
                return {"appId": r["appId"], "title": r.get("title") or r["appId"],
                        "developer": r.get("developer", ""),
                        "installs": "미확인", "score": round(r.get("score") or 0, 2),
                        "lang": lang, "country": region}
        return None  # 모든 조합에서 확정 404 → 죽은 앱, 제외

    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(validate, merged))
    return [v for v in results if v]

@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def deep_dive(app_id: str, region, lang, n_reviews=80):
    from google_play_scraper import Sort, app as gp_app, reviews as gp_reviews
    d = None
    for lg, ct in ((lang, region), ("en", "us"), ("ko", "kr"), ("en", region)):
        try:
            d = gp_app(app_id, lang=lg, country=ct)
            lang, region = lg, ct
            break
        except Exception:
            continue
    if d is None:  # 사전예약/미출시/내려간 앱 등 상세 페이지 404
        return None, [], pd.DataFrame(), pd.DataFrame()
    info = {"앱 이름": d.get("title"), "장르": d.get("genre"),
            "평점": round(d.get("score") or 0, 2), "평가 수": d.get("ratings"),
            "설치 구간": d.get("installs"), "인앱결제": "O" if d.get("offersIAP") else "X",
            "출시일": str(d.get("released") or ""),
            "최근 업데이트 안내": (d.get("recentChanges") or "")[:500],
            "개발사": d.get("developer")}
    histogram = d.get("histogram") or []

    def get_revs(sort):
        try:
            revs, _ = gp_reviews(app_id, lang=lang, country=region, sort=sort, count=n_reviews)
            return [{"별점": v["score"], "날짜": str(v.get("at") or "")[:10],
                     "리뷰": v["content"][:300]} for v in revs if v.get("content")]
        except Exception:
            return []

    recent = pd.DataFrame(get_revs(Sort.NEWEST))
    relevant = pd.DataFrame(get_revs(Sort.MOST_RELEVANT))
    return info, histogram, recent, relevant

# ==================== 공용 표시 ====================
STOP = set("게임 진짜 너무 그냥 근데 해서 하고 하는 있는 없는 이거 저는 제가 그리고 하면 합니다 있습니다 없습니다 인데 는데 지만 정말 좀 더 안 왜 잘 다 못".split())

def complaint_keywords(reviews: pd.DataFrame, topn=15):
    """불만 리뷰(별점≤2)에서 자주 등장하는 단어 집계 (단순 빈도 — 경향 파악용)"""
    low = reviews[reviews["별점"] <= 2]["리뷰"] if "별점" in reviews else pd.Series(dtype=str)
    words = []
    for t in low:
        for w in re.findall(r"[가-힣a-zA-Z]{2,}", str(t)):
            if w not in STOP:
                words.append(w)
    return pd.DataFrame(Counter(words).most_common(topn), columns=["단어", "빈도"]), len(low)

def naver_section(naver_df, context: str):
    if naver_df is None:
        st.info("네이버 데이터랩 키를 설정하면 연령·성별 상대지수가 추가됩니다.")
        return
    if "오류" in getattr(naver_df, "columns", []):
        st.warning(f"네이버 수집 실패: {naver_df['오류'][0]}")
        return
    if naver_df.empty:
        return
    st.caption(f"각 값 = 해당 집단의 '{ANCHOR_KW}' 검색 관심도를 100으로 놓은 상대지수 (앵커 보정). 절대 검색량 아님.")
    age_df = naver_df[naver_df["축"] == "연령"].pivot_table(index="구분", columns="키워드", values="상대지수")
    gen_df = naver_df[naver_df["축"] == "성별"].pivot_table(index="구분", columns="키워드", values="상대지수")
    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown("**연령대별**"); st.bar_chart(age_df)
    with c2:
        st.markdown("**성별**"); st.bar_chart(gen_df)
    with st.expander("📖 실무 해석 가이드 — 이 차트를 어떻게 쓰나"):
        if context == "market":
            st.markdown("""
- **용도**: 기획서의 타겟 가설 점검. "20대 여성 타겟"이라 썼는데 지수가 30대 남성에 몰려 있으면, 타겟 재정의 또는 마케팅 채널 재검토 신호.
- **판단 기준**: 특정 구간이 다른 구간의 2배 이상이면 유의미한 쏠림. 10~20% 차이는 노이즈일 수 있으니 근거로 쓰지 말 것.
- **한계**: 네이버 '검색자' 기준 (검색자≠플레이어). 검색 없이 광고로 유입되는 층, 유튜브/앱스토어에서 검색하는 저연령층은 과소 반영.
- **하지 말 것**: 이 차트만으로 타겟 확정. 확정이 필요하면 컨셉 이미지 소액 광고 테스트(성별·연령별 CTR)가 정석.""")
        else:
            st.markdown("""
- **용도**: 이 게임의 실제 관심층 프로파일. 게임명 검색자는 쿠폰·공략·업데이트를 찾는 유저라 현재 유저층의 근사치로 신뢰도가 높은 편.
- **출시 전**: 유사 장르 기획 시 '검증된 수요층'의 인구통계 출발점. 경쟁작 여러 개를 각각 조회하면 같은 장르 내 유저층 분화 비교 가능.
- **업데이트 방향**: 경쟁작이 잡고 우리가 못 잡은 인구 구간 = 확장 후보.
- **한계**: 게임명이 일반명사와 겹치면 오염. 쿠폰/공략 검색 문화가 없는 장르(하이퍼캐주얼)에선 표본 빈약.""")

# ==================== UI ====================
st.title("🎮 모바일 게임 사전 시장조사")
mode = st.radio("조사 목적 선택 — 목적에 따라 유효한 데이터가 다릅니다",
                ["🗺️ 시장/장르 조사 (출시 전 진입·포지셔닝 판단)",
                 "🔬 특정 게임 딥다이브 (벤치마킹·업데이트 방향)"],
                horizontal=True)

# ---------- 모드 1: 시장/장르 조사 ----------
if mode.startswith("🗺️"):
    st.caption("장르·컨셉 키워드로 경쟁 구도와 수요층을 봅니다. 특정 게임 이름은 딥다이브 모드를 쓰세요.")
    with st.form("market"):
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        kw_text = c1.text_input("장르/컨셉 키워드 (쉼표 구분, 서로 다른 각도로 2~4개)",
                                placeholder="예: 방치형 게임, 키우기 게임, 클리커")
        region = c2.selectbox("국가", ["kr", "us", "jp", "tw", "id", "vn", "th", "de", "br"])
        platform = c3.selectbox("플랫폼", ["둘 다", "Android", "iOS"])
        scale = c4.selectbox("수집 규모", ["표준 (~60개)", "대량 (~120개)"])
        target = st.text_input("타겟 가설 메모 (AI 분석 시 '가설 vs 데이터' 비교에 사용, 선택)",
                               placeholder="예: 20대 여성, 10분 내외 세션, 힐링·수집 선호, 소과금")
        go = st.form_submit_button("조사 시작", type="primary")

    if go and kw_text.strip():
        keywords = tuple(k.strip() for k in kw_text.split(",") if k.strip())[:4]
        lang = "ko" if region == "kr" else "en"
        max_apps = 120 if scale.startswith("대량") else 60
        queries = expand_queries(keywords, region, lang, cap=16 if max_apps > 60 else 12)

        with st.spinner(f"스토어 실데이터 수집 중... (확장 쿼리 {len(queries)}개, 첫 조사 2~5분)"):
            gp_df, reviews_df, as_df = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
            if platform in ("둘 다", "Android"):
                gp_df, reviews_df = collect_google_play(queries, region, lang, max_apps=max_apps)
            if platform in ("둘 다", "iOS"):
                as_df = collect_app_store(keywords, region, max_apps=max_apps)
            naver_df = collect_naver_anchored(keywords) if region == "kr" else None

        comp = pd.concat([gp_df, as_df], ignore_index=True) if not (gp_df.empty and as_df.empty) else pd.DataFrame()
        st.success(f"수집 완료 — 상위 노출 앱 {len(comp)}개, 리뷰 {len(reviews_df)}건 (24시간 캐시)")

        st.header("1. 상위 노출 경쟁작")
        if comp.empty:
            st.warning("결과 없음. 키워드를 더 일반적인 표현으로 바꿔보세요.")
        else:
            st.dataframe(comp, use_container_width=True, hide_index=True, height=430)
            st.download_button("CSV 다운로드", comp.to_csv(index=False).encode("utf-8-sig"), "competitors.csv")
            with st.expander("📖 실무 해석 가이드 — 경쟁작 표"):
                st.markdown("""
- **표본의 정체**: '시장의 전체 앱'이 아니라 '이 키워드로 검색 시 스토어가 상위 노출하는 앱'. 유저가 검색으로 우리 게임을 발견할 때 실제로 마주칠 경쟁 화면이라고 보면 정확함.
- **출시 전 판단**: ① 상위권 평가 수 규모 = 넘어야 할 벽의 높이. 상위 5개가 전부 수십만 평가면 정면승부는 마케팅비 싸움. ② '평가 수는 적은데 평점 높은 최근작' 존재 = 작은 팀도 뚫고 있다는 신호. ③ 인앱결제 O/X 비율 = 이 장르 유저가 익숙한 BM 벤치마크.
- **업데이트/운영**: 같은 검색어에서 노출이 오르는 신작 = 직접 위협. 주기적으로 재조사해 신규 진입자 모니터링.
- **주의**: 설치 구간은 정확값 아님(100만+는 100만~999만 어딘가). 매출·DAU는 여기 없음 — 그 판단엔 유료 데이터 필요.""")

        if not comp.empty:
            st.header("2. 상위 노출 앱의 구조")
            gp_only = comp[comp["스토어"] == "Google Play"].copy()
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("설치 구간 분포")
                if not gp_only.empty and gp_only["설치 구간"].notna().any():
                    gp_only["_n"] = gp_only["설치 구간"].map(
                        lambda s: int(re.sub(r"[^\d]", "", s)) if isinstance(s, str) and re.sub(r"[^\d]", "", s) else None)
                    order = gp_only.dropna(subset=["_n"]).sort_values("_n")
                    st.bar_chart(order.groupby("설치 구간", sort=False).size())
                else:
                    st.caption("데이터 부족")
            with c2:
                st.subheader("상위 노출 앱의 출시 연도 분포")
                yrs = comp["출시일"].astype(str).str.extract(r"(20\d{2})")[0].dropna()
                if not yrs.empty:
                    st.bar_chart(yrs.value_counts().sort_index())
                else:
                    st.caption("데이터 부족")
            with st.expander("📖 실무 해석 가이드 — 이 차트들의 정확한 의미와 한계"):
                st.markdown("""
- **설치 구간 분포**: 100만+ 앱이 많다 = 수요는 검증됐지만 경쟁도 검증된 시장. 전부 10만 이하 = 니치이거나 수요 자체가 약함 (어느 쪽인지는 리뷰·검색 데이터로 교차 확인).
- **출시 연도 분포**: ⚠️ '시장 전체의 연간 출시량'이 아님. 내려간 앱은 안 잡히고(생존 편향), 표본이 검색 노출 순이라 최근작이 부풀어 보임.
  - **읽어도 되는 것**: 최근작이 상위 노출을 따내는 중 = 후발주자가 기존 강자를 뚫을 여지가 있는 구도. 상위가 전부 수년 전 작품 = 굳어진 시장, 정면 진입 난이도 높음.
  - **읽으면 안 되는 것**: "이 시장은 성장/축소 중" 같은 시장 전체 결론.""")

        if region == "kr":
            st.header("3. 수요층 프로파일 — 연령·성별 검색 상대지수")
            naver_section(naver_df, "market")

        st.header("4. 경쟁작 유저 리뷰 (상위 앱)")
        if not reviews_df.empty:
            kw_df, n_low = complaint_keywords(reviews_df)
            t1, t2, t3 = st.tabs([f"전체 ({len(reviews_df)})", f"불만 리뷰 (별점≤2, {n_low})", "불만 키워드 빈도"])
            with t1:
                st.dataframe(reviews_df, use_container_width=True, hide_index=True, height=300)
            with t2:
                st.dataframe(reviews_df[reviews_df["별점"] <= 2], use_container_width=True, hide_index=True, height=300)
            with t3:
                if not kw_df.empty:
                    st.bar_chart(kw_df.set_index("단어"))
                    st.caption("단순 단어 빈도라 경향 파악용. 실제 맥락은 원문으로 확인.")
            with st.expander("📖 실무 해석 가이드 — 리뷰가 이 툴에서 가장 값진 이유"):
                st.markdown("""
- **출시 전 = 차별화 기회 목록**: 상위 경쟁작들에 공통으로 반복되는 불만(광고 과다, 과금 압박, 후반 콘텐츠 부족, 서버 등)이 곧 우리 게임의 셀링 포인트 후보. 기획 리뷰 때 "경쟁작 불만 TOP3를 우리는 어떻게 해결하는가"에 답할 것.
- **BM 설계**: 불만 중 과금 관련 비중이 높으면 그 장르 유저의 과금 피로도가 높다는 뜻 → 초반 과금 압박 설계 주의.
- **한계**: 리뷰 작성자는 극단(매우 만족/매우 불만) 편향. 침묵하는 다수의 의견이 아님. '관련성 높은 리뷰' 우선이라 스토어 알고리즘의 선별도 들어감.""")

        st.header("5. AI 분석 리포트 받기 (무료)")
        data_summary = {"모드": "시장조사", "키워드": list(keywords), "국가": region,
                        "타겟 가설": target or "미지정",
                        "경쟁작": comp.to_dict("records") if not comp.empty else [],
                        "리뷰": reviews_df.head(80).to_dict("records") if not reviews_df.empty else [],
                        "네이버_상대지수(게임=100)": (naver_df.to_dict("records")
                                                      if naver_df is not None and not getattr(naver_df, "empty", True)
                                                      and "오류" not in naver_df.columns else [])}
        prompt = f"""당신은 모바일 게임 퍼블리셔의 시니어 시장분석가입니다. 아래 실데이터만 근거로 분석하고 없는 수치는 지어내지 마세요.
표본 특성: 경쟁작=검색 상위 노출 앱(전수 아님, 생존 편향 있음), 설치 수=구간값, 네이버 지수='게임'=100 상대지수(검색자 기준).
한국어 리포트: ## 요약(3줄) / ## 경쟁 구도(벽의 높이·최근 돌파 사례·빈틈) / ## 경쟁작 불만 TOP5 → 각각을 차별화 기회로 변환
/ ## 타겟 가설 vs 데이터 / ## BM 시사점 / ## 진입 리스크 / ## 포지셔닝 제언(실행 가능하게 3~5개) / ## 데이터 한계
[수집 데이터]
{json.dumps(data_summary, ensure_ascii=False, indent=1)[:50000]}"""
        st.download_button("📋 분석용 텍스트 다운로드 (.txt) → claude.ai에 붙여넣기", prompt, "analysis_prompt.txt")

# ---------- 모드 2: 특정 게임 딥다이브 ----------
else:
    st.caption("경쟁작 하나를 깊게 봅니다: 별점 분포, 최신 리뷰(업데이트 반응), 불만 구조, 관심층 프로파일.")
    c1, c2 = st.columns([3, 1])
    game_name = c1.text_input("게임 이름", placeholder="예: 소울 스트라이크")
    region = c2.selectbox("국가", ["kr", "us", "jp", "tw"], key="dd_region")
    lang = "ko" if region == "kr" else "en"

    if st.button("게임 검색", type="primary", disabled=not game_name.strip()):
        st.session_state.candidates = search_game_candidates(game_name.strip(), region, lang)
        st.session_state.game_name = game_name.strip()

    cands = st.session_state.get("candidates", [])
    if cands:
        pick = st.selectbox("분석할 게임 선택 (개발사·설치 구간으로 정품 확인)",
                            options=range(len(cands)),
                            format_func=lambda i: (f"{cands[i]['title']} — {cands[i]['developer']}"
                                                   f" · 설치 {cands[i]['installs']} · ★{cands[i]['score']}"))
        if st.button("이 게임 분석"):
            with st.spinner("상세 정보·리뷰 수집 중... (1~2분)"):
                info, hist, recent, relevant = deep_dive(
                    cands[pick]["appId"], cands[pick]["country"], cands[pick]["lang"])
            if info is None:
                st.error("이 앱의 상세 페이지를 불러올 수 없습니다 (스토어에서 404 응답). "
                         "사전예약 중이거나 해당 국가 미출시, 또는 최근 스토어에서 내려간 앱일 수 있습니다. "
                         "목록에서 다른 후보를 선택하거나 국가를 바꿔 다시 검색해보세요.")
                st.stop()
            with st.spinner("관심층 데이터 수집 중..."):
                naver_df = collect_naver_anchored((st.session_state.game_name,)) if region == "kr" else None

            st.header(f"🔬 {info['앱 이름']}")
            st.dataframe(pd.DataFrame([info]).T.rename(columns={0: "값"}), use_container_width=True)

            cc1, cc2 = st.columns(2)
            with cc1:
                st.subheader("별점 분포")
                if hist and len(hist) == 5:
                    st.bar_chart(pd.Series(hist, index=["1★", "2★", "3★", "4★", "5★"]))
                    with st.expander("📖 해석 가이드"):
                        st.markdown("""
- **5★ 압도 + 1★ 소수**: 건강한 구조. 1★ 내용만 파면 됨.
- **U자형(5★·1★ 양극)**: 코어는 만족하나 특정 집단이 강하게 이탈 — 1★ 원인이 온보딩인지 과금인지 서버인지가 핵심 질문.
- **3~4★ 두꺼움**: '나쁘진 않지만 특별하지 않다' — 리텐션 취약 신호인 경우 많음.""")
                else:
                    st.caption("별점 분포 데이터 없음")
            with cc2:
                if region == "kr":
                    st.subheader("관심층 프로파일 (네이버)")
                    naver_section(naver_df, "deep")

            st.subheader("리뷰 분석")
            all_rev = (pd.concat([recent, relevant]).drop_duplicates(subset=["리뷰"])
                       if not recent.empty or not relevant.empty else pd.DataFrame())
            if not all_rev.empty:
                kw_df, n_low = complaint_keywords(all_rev)
                t1, t2, t3 = st.tabs([f"최신순 ({len(recent)}) — 업데이트 반응 추적",
                                      f"불만 리뷰 (별점≤2, {n_low})", "불만 키워드 빈도"])
                with t1:
                    st.dataframe(recent, use_container_width=True, hide_index=True, height=320)
                with t2:
                    st.dataframe(all_rev[all_rev["별점"] <= 2], use_container_width=True, hide_index=True, height=320)
                with t3:
                    if not kw_df.empty:
                        st.bar_chart(kw_df.set_index("단어"))
                st.download_button("리뷰 CSV", all_rev.to_csv(index=False).encode("utf-8-sig"), "reviews.csv")
                with st.expander("📖 실무 해석 가이드 — 딥다이브 리뷰 활용"):
                    st.markdown("""
- **최신순 탭 = 라이브 운영 교본**: 최근 업데이트 직후 리뷰가 곧 그 패치에 대한 시장 반응. 경쟁작이 어떤 업데이트로 욕먹고 칭찬받는지가 우리 업데이트 로드맵의 선행 실험 데이터.
- **출시 전**: 이 게임의 불만 TOP 이슈 = 같은 장르로 낼 때 반드시 피해야 할 지뢰 목록이자 차별화 포인트.
- **업데이트 방향**: 우리 게임 자신을 딥다이브하면 유저 불만 백로그의 우선순위 근거가 됨 (빈도 높은 불만부터).
- **한계**: 리뷰어는 극단 편향. 무엇을 고칠지의 단서이지, 유저 전체의 여론조사가 아님.""")

            data_summary = {"모드": "딥다이브", "게임": info, "별점분포(1~5★)": hist,
                            "최신 리뷰": recent.head(60).to_dict("records") if not recent.empty else [],
                            "불만 리뷰": (all_rev[all_rev["별점"] <= 2].head(40).to_dict("records")
                                          if not all_rev.empty else []),
                            "네이버_관심층(게임=100)": (naver_df.to_dict("records")
                                                        if naver_df is not None and not getattr(naver_df, "empty", True)
                                                        and "오류" not in naver_df.columns else [])}
            prompt = f"""당신은 모바일 게임 라이브 운영·기획 분석가입니다. 아래 실데이터만 근거로 분석하고 없는 수치는 지어내지 마세요.
한국어 리포트: ## 요약 / ## 이 게임의 강점(유저가 실제로 칭찬하는 것) / ## 불만 구조 TOP5(빈도·심각도)
/ ## 최근 업데이트에 대한 유저 반응 / ## 관심층 프로파일 해석 / ## 우리가 배울 것·피할 것 / ## (유사 장르 신규 진입 시) 차별화 제언
[수집 데이터]
{json.dumps(data_summary, ensure_ascii=False, indent=1)[:50000]}"""
            st.download_button("📋 분석용 텍스트 다운로드 (.txt) → claude.ai에 붙여넣기", prompt, "deepdive_prompt.txt")
