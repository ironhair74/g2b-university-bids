#!/usr/bin/env python3
"""
나라장터 대학교 발주 용역 입찰공고 자동 검색기

조달청_나라장터 입찰공고정보서비스(공공데이터포털 15129394, BidPublicInfoService)의
'용역 입찰공고 검색조건 조회(getBidPblancListInfoServcPPSSrch)'를 호출해
수요기관/공고기관명에 '대학'이 들어간 공고만 모으고, 컨설팅 적합도를 점수화합니다.

사용 예:
  export G2B_SERVICE_KEY="공공데이터포털_디코딩_인증키"
  python search_bids.py --days 14
  python search_bids.py --days 30 --keyword 발전계획 --min-budget 100000000
  python search_bids.py --sample ../../../examples/sample_response.json   # 키 없이 테스트

표준 라이브러리만 사용합니다 (설치 불필요).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENDPOINT = (
    "http://apis.data.go.kr/1230000/ad/BidPublicInfoService/"
    "getBidPblancListInfoServcPPSSrch"
)
CHUNK_DAYS = 7          # API 조회기간 제한을 피하려고 7일 단위로 나눠 호출
PAGE_SIZE = 100
MAX_PAGES_PER_CHUNK = 20
UNIT_PRICE_PER_MM = 35_000_000   # 1MM 평균단가 (기본값, --unit-price로 변경)
# 수요기관명 검색어: '대학'이 이름에 없는 과학기술원(KAIST·UNIST·DGIST·GIST)도 포함
INSTITUTION_TERMS = ["대학", "과학기술원"]

# ---- 적합도 사전 (references/scoring.md 와 동일하게 유지) -------------------
# 매칭은 공고명에서 띄어쓰기를 지운 뒤 한다 ("방안 수립" = "방안수립").
# 영문 약어는 단어 단위로만 매칭한다 (KAIST 안의 'AI'는 매칭하지 않음).
HIGH = ["중장기", "발전계획", "발전전략", "전략수립", "컨설팅", "ISP", "ISMP", "BPR",
        "조직진단", "경영진단", "수준진단", "타당성", "마스터플랜", "로드맵", "구조개혁",
        "재정", "성과관리", "성과평가", "기관평가", "정책연구", "체계", "방안수립",
        "거버넌스", "중장기계획", "학사구조", "구조개편", "재설계", "특성화"]
HIGH_REGEX = {
    "전략": r"전략(?!산업)",                     # '전략산업'(인력양성 사업명)은 제외
    "개편": r"(?<!홈페이지)(?<!누리집)(?<!사이트)(?<!웹)개편",   # 웹사이트 개편은 제외
    "Redesign": r"(?i)redesign",                  # 영문 'Redesign'(대소문자 무관)
}
MID = ["연구", "학술", "기획", "분석", "진단", "개선방안", "정보화", "플랫폼", "데이터",
       "스마트캠퍼스", "AX", "AI", "DX", "인공지능", "디지털전환", "혁신", "글로컬",
       "RISE", "라이즈", "앵커", "조사"]
# 시스템 구축(SI)형: 핵심 키워드가 없을 때만 감점
SI = ["시스템구축", "구축용역", "고도화", "솔루션", "개발용역"]
EXCLUDE = ["청소", "경비", "미화", "시설관리", "급식", "식당", "공사", "감리", "수선",
           "보수", "유지관리", "유지보수", "구매", "임차", "렌탈", "인쇄", "운송",
           "방역", "소독", "폐기물", "조경", "전기안전", "승강기", "냉난방", "버스", "운행",
           "보험", "대행", "번역", "행사", "축제", "축전", "현장운영", "페스티벌", "Festival", "박람회", "공유회",
           "성과발표회", "컨퍼런스", "세미나", "학술대회", "전시", "부스", "연수", "인턴십",
           "벤치마킹", "캠프", "경진대회", "공모전", "챌린지", "해커톤", "메이커톤", "세탁",
           "린넨", "위탁검사", "검진", "콜센터", "채용", "회계감사", "결산", "정산", "특허",
           "촬영", "영상", "콘텐츠", "프로그램운영", "교육운영", "운영지원", "위탁운영",
           "운영위탁", "합성", "시험법", "부품", "조립", "프로토타입", "인허가", "시설검사",
           "안전점검", "숙식", "환경조성", "실습실"]
# ---- AX·DX 공고 (점수·등급과 별도로 모아 보여줌) --------------------------
AX_TERMS = ["AX", "AI", "DX", "인공지능", "디지털전환", "생성형", "LLM", "에이전트",
            "Agent", "에듀테크", "LXP", "LMS", "빅데이터", "데이터레이크", "DataLake",
            "디지털트윈", "메타버스", "클라우드", "지능형", "스마트캠퍼스", "스마트병원",
            "챗봇", "AID"]
AX_TYPES = [   # 위에서부터 먼저 맞는 유형 하나로 분류
    ("전략·진단", ["전략", "진단", "컨설팅", "로드맵", "ISP", "ISMP", "체계", "방안수립",
                 "마스터플랜", "기본계획", "타당성"]),
    ("인프라·데이터", ["주권AI", "데이터센터", "GPU", "클라우드", "DataLake", "데이터레이크"]),
    ("교육·학사 플랫폼", ["LXP", "LMS", "학습", "학사", "학생", "튜터", "에듀테크", "교육과정",
                     "강좌", "Advisor", "면접", "원격교육", "교과"]),
    ("인프라·데이터", ["데이터", "인프라", "네트워크", "디지털트윈"]),
    ("행정·업무 시스템", ["행정", "업무", "포털", "그룹웨어", "시스템"]),
]
SHORT_EN = {"ISP", "ISMP", "BPR", "AX", "AI", "DX", "RISE", "LLM", "LXP", "LMS", "AID"}
HEALTH_HINT = ["병원", "의료원", "보건"]
# '과학기술원' 검색에 함께 잡히지만 대학이 아닌 기관
NON_UNIV = ["한국해양과학기술원"]


def kst_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def call_api(key: str, begin: datetime, end: datetime, page: int,
             keyword: str | None, inst: str) -> dict:
    params = {
        "serviceKey": key,
        "numOfRows": PAGE_SIZE,
        "pageNo": page,
        "type": "json",
        "inqryDiv": "1",            # 1 = 공고게시일시 기준
        "inqryBgnDt": fmt(begin),
        "inqryEndDt": fmt(end),
        "dminsttNm": inst,           # 수요기관명 부분일치
    }
    if keyword:
        params["bidNtceNm"] = keyword
    key = key.strip().strip('"').strip("\u201c\u201d")   # 복사 시 섞인 공백·둥근따옴표 제거
    params["serviceKey"] = key
    query = urllib.parse.urlencode(params, safe="")
    last_err = None
    auth_like = False
    # http가 막힌 네트워크(일부 와이파이·iOS 앱)를 위해 https로도 시도
    for base in (ENDPOINT, ENDPOINT.replace("http://", "https://", 1)):
        for attempt in range(2):
            req = urllib.request.Request(base + "?" + query,
                                         headers={"User-Agent": "Mozilla/5.0 g2b-university-bids"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read().decode("utf-8", errors="replace")
                if raw.lstrip().startswith("<"):
                    # 인증 오류 등은 XML로 돌아옴
                    code = re.search(r"<returnReasonCode>(\d+)", raw)
                    msg = re.search(r"<returnAuthMsg>([^<]+)", raw)
                    raise RuntimeError(
                        f"API 오류 코드 {code.group(1) if code else '?'}: "
                        f"{msg.group(1) if msg else raw[:200]}")
                return json.loads(raw)
            except RuntimeError:
                raise
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:300]
                auth_like = e.code in (401, 403)
                last_err = f"HTTP {e.code} ({base.split(':')[0]}) 서버 응답: {body or e.reason}"
                break                       # 4xx는 재시도해도 같으므로 다음 주소로
            except Exception as e:          # 네트워크 일시 오류 → 재시도
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(2 * (attempt + 1))
    hint = ""
    if auth_like:
        hint = ("\n→ 인증키를 확인하세요: 공백·따옴표 없이 Decoding 키를 넣었는지, "
                "활용신청이 '승인'인지, 키 길이가 맞는지(echo ${#G2B_SERVICE_KEY}).")
    raise RuntimeError(f"API 호출 실패: {last_err}{hint}")


def extract_items(payload: dict) -> tuple[list[dict], int]:
    body = payload.get("response", {}).get("body", {})
    header = payload.get("response", {}).get("header", {})
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", "03"):
        raise RuntimeError(f"API 결과코드 {code}: {header.get('resultMsg')}")
    items = body.get("items") or []
    if isinstance(items, dict):        # 응답 형태가 {"item": [...]} 인 경우
        items = items.get("item", [])
    if isinstance(items, dict):        # 단건이면 dict로 옴
        items = [items]
    return items, int(body.get("totalCount") or 0)


def fetch(key: str, days: int, keyword: str | None) -> list[dict]:
    end = kst_now()
    start = end - timedelta(days=days)
    rows: list[dict] = []
    for inst in INSTITUTION_TERMS:
        cur = start
        while cur < end:
            chunk_end = min(cur + timedelta(days=CHUNK_DAYS), end)
            page = 1
            while page <= MAX_PAGES_PER_CHUNK:
                items, total = extract_items(
                    call_api(key, cur, chunk_end, page, keyword, inst))
                rows.extend(items)
                if page * PAGE_SIZE >= total or not items:
                    break
                page += 1
                time.sleep(0.3)
            cur = chunk_end
    return rows


def to_int(v) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def is_university(item: dict) -> bool:
    names = f"{item.get('dminsttNm','')} {item.get('ntceInsttNm','')}"
    if any(n in names for n in NON_UNIV):
        return False
    return any(t in names for t in INSTITUTION_TERMS)


def has(word: str, title: str, title_ns: str) -> bool:
    if word in SHORT_EN:
        return re.search(rf"(?<![A-Za-z]){word}(?![A-Za-z])", title) is not None
    return word in title_ns


def score(item: dict) -> tuple[int, list[str], str]:
    title = item.get("bidNtceNm", "")
    ns = re.sub(r"\s+", "", title)
    hits_h = [k for k in HIGH if has(k, title, ns)]
    hits_h += [k for k, rx in HIGH_REGEX.items() if re.search(rx, ns)]
    hits_m = [k for k in MID if has(k, title, ns)]
    ns_x = ns.replace("운영체계", "체계")   # '프로그램 운영 체계 구축'은 운영대행이 아님
    hits_x = [k for k in EXCLUDE if k in ns_x]
    hits_si = [] if hits_h else [k for k in SI if k in ns]
    s = 3 * len(hits_h) + len(hits_m) - 5 * len(hits_x) - 2 * len(hits_si)
    budget = budget_of(item)
    if budget >= 300_000_000:
        s += 2
    elif budget >= 100_000_000:
        s += 1
    names = f"{item.get('dminsttNm','')} {item.get('ntceInsttNm','')}"
    track = "의료(대학병원)" if any(h in names for h in HEALTH_HINT) else (
        "국립대" if ("국립" in names or "서울대" in names or "과학기술원" in names) else "대학")
    return s, (hits_h + hits_m + [f"SI:{x}" for x in hits_si]
               + [f"제외:{x}" for x in hits_x]), track


def budget_of(item: dict) -> int:
    return to_int(item.get("asignBdgtAmt")) or to_int(item.get("presmptPrce"))


def ax_type(item: dict) -> str:
    """AX·DX 관련이면 유형을, 아니면 빈 문자열을 돌려준다.
    제외어(시설·행사·운영대행 등)에 걸린 공고는 AX 단어가 있어도 넣지 않는다."""
    title = item.get("bidNtceNm", "")
    ns = re.sub(r"\s+", "", title)
    if not any(has(t, title, ns) for t in AX_TERMS):
        return ""
    ns_x = ns.replace("운영체계", "체계")
    if any(k in ns_x for k in EXCLUDE):
        return ""
    for label, words in AX_TYPES:   # 같은 라벨이 두 번 나오면 강한 신호가 먼저
        if any(has(w, title, ns) for w in words):
            return label
    return "기타"


def grade(s: int) -> str:
    return "A" if s >= 5 else "B" if s >= 2 else "C"


def dedupe(items: list[dict]) -> list[dict]:
    """1) 같은 공고번호는 차수가 가장 높은 것(정정공고 최신본)만 남긴다.
    2) 재공고처럼 번호가 달라도 공고명·수요기관이 같으면 가장 최근 공고만 남긴다."""
    best: dict[str, dict] = {}
    for it in items:
        no = it.get("bidNtceNo", "")
        if not no:
            continue
        if no not in best or to_int(it.get("bidNtceOrd")) > to_int(best[no].get("bidNtceOrd")):
            best[no] = it
    by_title: dict[tuple, dict] = {}
    for it in best.values():
        name = re.sub(r"\(?재공고\)?|\[재공고\]|\s+", "", it.get("bidNtceNm", ""))
        k = (name, it.get("dminsttNm", ""))
        cur = by_title.get(k)
        if cur is None or (it.get("bidNtceDt", ""), it.get("bidNtceNo", "")) > \
                (cur.get("bidNtceDt", ""), cur.get("bidNtceNo", "")):
            by_title[k] = it
    return list(by_title.values())


def close_of(item: dict) -> str:
    """입찰마감일시. 비어 있으면 개찰일시로 대신하고 표시한다."""
    c = (item.get("bidClseDt") or "").strip()
    if c:
        return c
    o = (item.get("opengDt") or "").strip()
    return f"{o} (개찰)" if o else ""


def _days_left(close: str, now: datetime):
    try:
        return (datetime.strptime(close[:16], "%Y-%m-%d %H:%M") - now).days
    except ValueError:
        return None


def build_rows(items: list[dict], min_budget: int, include_closed: bool,
               unit_price: int) -> list[dict]:
    now = kst_now()
    out = []
    for it in dedupe([i for i in items if is_university(i)]):
        b = budget_of(it)
        if b < min_budget:
            continue
        close = close_of(it)
        try:
            closed = datetime.strptime(close[:16], "%Y-%m-%d %H:%M") < now
        except ValueError:
            closed = False
        if closed and not include_closed:
            continue
        s, hits, track = score(it)
        out.append({
            "등급": grade(s),
            "점수": s,
            "공고명": it.get("bidNtceNm", ""),
            "수요기관": it.get("dminsttNm", ""),
            "공고기관": it.get("ntceInsttNm", ""),
            "구분": track,
            "AX·DX": ax_type(it),
            "예산(원)": b,
            "예산(억원)": round(b / 100_000_000, 2),
            "환산MM": round(b / unit_price, 1) if unit_price else "",
            "계약방법": it.get("cntrctCnclsMthdNm", ""),
            "공고일시": it.get("bidNtceDt", ""),
            "마감일시": close,
            "마감임박": "⚠️" if close and _days_left(close, now) is not None
                        and 0 <= _days_left(close, now) < 7 else "",
            "공고번호": f"{it.get('bidNtceNo','')}-{it.get('bidNtceOrd','')}",
            "매칭키워드": ", ".join(hits),
            "링크": it.get("bidNtceDtlUrl", ""),
        })
    out.sort(key=lambda r: (-r["점수"], r["마감일시"] or "9999"))
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # 엑셀 한글 깨짐 방지
        if not rows:
            f.write("결과 없음\n")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def won(n: int) -> str:
    """예산 표기: 억원 단위, 소수점 둘째 자리 (예: 4,500만원 → 0.45). 0원은 '-'."""
    return f"{n/100_000_000:,.2f}" if n else "-"


def write_md(rows: list[dict], path: Path, days: int, raw_n: int) -> None:
    a = [r for r in rows if r["등급"] == "A"]
    b = [r for r in rows if r["등급"] == "B"]
    lines = [
        f"# 나라장터 대학 발주 용역 브리프 ({kst_now():%Y-%m-%d})",
        "",
        f"- 조회기간: 최근 {days}일 (공고게시일 기준)",
        f"- API 원본 {raw_n}건 → 대학 발주·중복제거·필터 후 **{len(rows)}건** "
        f"(A {len(a)} / B {len(b)} / C {len(rows)-len(a)-len(b)})",
        "",
        "## A등급 — 바로 검토할 공고",
        "",
    ]
    def table(rs):
        if not rs:
            return ["해당 없음", ""]
        t = ["| 공고명 | 수요기관 | 예산(억원) | 환산MM | 마감 | 키워드 |",
             "|---|---|---:|---:|---|---|"]
        for r in rs:
            name = f"[{r['공고명']}]({r['링크']})" if r["링크"] else r["공고명"]
            t.append(f"| {name} | {r['수요기관']} | {won(r['예산(원)'])} | "
                     f"{r['환산MM']} | {r['마감일시'][:10]} {r['마감임박']} | {r['매칭키워드']} |")
        return t + [""]
    lines += table(a)
    lines += ["## B등급 — 관심 공고", ""] + table(b)
    ax = sorted([r for r in rows if r["AX·DX"]], key=lambda r: -r["예산(원)"])
    lines += [f"## AX·DX 관련 공고 ({len(ax)}건, 예산순)", "",
              "등급과 상관없이 AI·DX·데이터·학습플랫폼 관련 공고를 모았습니다. "
              "구축(SI) 사업이 많아 직접 수주 대상은 적지만, 대학들이 AX에 어디에 "
              "얼마를 쓰는지 보여주는 시장 신호입니다.", ""]
    if ax:
        by_type: dict[str, list] = {}
        for r in ax:
            by_type.setdefault(r["AX·DX"], []).append(r)
        lines += ["| 유형 | 건수 | 예산 합계(억원) |", "|---|---:|---:|"]
        for label in dict.fromkeys([t for t, _ in AX_TYPES] + ["기타"]):
            g = by_type.get(label, [])
            if g:
                lines.append(f"| {label} | {len(g)} | {won(sum(r['예산(원)'] for r in g))} |")
        lines += ["", "| 유형 | 등급 | 공고명 | 수요기관 | 예산(억원) | 마감 |",
                  "|---|---|---|---|---:|---|"]
        for r in ax:
            name = f"[{r['공고명']}]({r['링크']})" if r["링크"] else r["공고명"]
            lines.append(f"| {r['AX·DX']} | {r['등급']} | {name} | {r['수요기관']} | "
                         f"{won(r['예산(원)'])} | {r['마감일시'][:10]} {r['마감임박']} |")
        lines.append("")
    else:
        lines += ["해당 없음", ""]
    lines += ["## 참고", "",
              "- C등급(시설·구매·행사·운영대행 등 비컨설팅)은 CSV에만 포함했습니다.",
              "- ⚠️ = 마감 7일 이내. 마감일이 비어 있으면 API에 값이 없는 공고입니다.",
              "- 예산 단위는 억원입니다 (배정예산 우선, 없으면 추정가격). '-'는 API에 금액이 없는 공고입니다.",
              "- 환산MM = 예산 ÷ 1MM 단가. 실제 투입 가능 MM은 경비·부가세를 빼고 다시 보세요.",
              "- 점수는 공고명 키워드 기반 1차 선별입니다. 제안요청서 원문 확인이 필요합니다.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="나라장터 대학 발주 용역 공고 검색")
    p.add_argument("--days", type=int, default=14, help="최근 N일 (기본 14)")
    p.add_argument("--keyword", help="공고명 추가 키워드 (예: 발전계획)")
    p.add_argument("--min-budget", type=int, default=0, help="최소 예산(원)")
    p.add_argument("--include-closed", action="store_true", help="마감 공고 포함")
    p.add_argument("--unit-price", type=int, default=UNIT_PRICE_PER_MM,
                   help="1MM 단가(원), 기본 35,000,000")
    p.add_argument("--sample", help="API 대신 저장된 JSON 응답 파일로 실행")
    p.add_argument("--out", default="output", help="결과 폴더")
    p.add_argument("--institution",
                   help="특정 기관만 조회 (수요기관명 부분일치, 예: 경희대). 과거 이력 조회용")
    a = p.parse_args()

    global INSTITUTION_TERMS
    if a.institution:
        INSTITUTION_TERMS = [a.institution.strip()]

    if a.sample:
        items, _ = extract_items(json.loads(Path(a.sample).read_text(encoding="utf-8")))
    else:
        key = os.environ.get("G2B_SERVICE_KEY")
        if not key:
            print("❌ G2B_SERVICE_KEY 환경변수가 없습니다. README의 '인증키 발급'을 보세요.",
                  file=sys.stderr)
            return 2
        items = fetch(key, a.days, a.keyword)

    rows = build_rows(items, a.min_budget, a.include_closed or bool(a.sample), a.unit_price)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = kst_now().strftime("%Y%m%d")
    prefix = f"history_{a.institution.strip()}_{a.days}d" if a.institution else "univ_bids"
    csv_p, md_p = out / f"{prefix}_{stamp}.csv", out / f"{prefix}_{stamp}.md"
    write_csv(rows, csv_p)
    write_md(rows, md_p, a.days, len(items))

    summary = {"raw": len(items), "kept": len(rows),
               "A": sum(r["등급"] == "A" for r in rows),
               "B": sum(r["등급"] == "B" for r in rows),
               "AX_DX": sum(bool(r["AX·DX"]) for r in rows),
               "csv": str(csv_p), "md": str(md_p)}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
