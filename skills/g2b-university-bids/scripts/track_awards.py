#!/usr/bin/env python3
"""
나라장터 낙찰 결과 추적기

브리프(reports/univ_bids_*.csv)의 A·B등급과 AX·DX 전략·진단 공고를 추적 목록
(reports/award_tracking.csv)에 쌓아 두고, 입찰 마감이 지난 공고의 낙찰 결과를
조달청_나라장터 낙찰정보서비스(공공데이터포털 15129397, ScsbidInfoService)의
'낙찰된 목록 현황 용역조회(getScsbidListSttusServc)'로 공고번호별 조회한다.

결과:
  reports/award_tracking.csv          누적 추적표 (낙찰업체·금액·낙찰률 등)
  reports/awards_YYYYMMDD.md          이번 주 확정된 낙찰 결과 + 누적 경쟁사 집계
  --append-to <브리프.md>              위 내용을 브리프 끝에 붙임 (메일에 포함)

사용 예:
  export G2B_SERVICE_KEY=...
  python track_awards.py --reports ../../../reports --append-to ../../../reports/univ_bids_20261109.md

필요: 공공데이터포털에서 '조달청_나라장터 낙찰정보서비스' 활용신청 (키는 입찰공고와 같음)
표준 라이브러리만 사용한다.
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

ENDPOINT = ("https://apis.data.go.kr/1230000/as/ScsbidInfoService/"
            "getScsbidListSttusServc")
TRACK_GRADES = {"A", "B"}
TRACK_AX_TYPES = {"전략·진단"}
WAIT_AFTER_CLOSE_DAYS = 3     # 마감 후 이 기간은 개찰·평가 중이므로 조회하지 않음
GIVE_UP_DAYS = 120            # 마감 후 이 기간이 지나도 결과가 없으면 '확인불가'
MAX_CALLS = 200               # 1회 실행당 API 호출 상한 (일일 트래픽 보호)

FIELDS = ["공고번호", "공고명", "수요기관", "등급", "구분", "예산(억원)", "마감일시",
          "상태", "낙찰업체", "낙찰금액(억원)", "낙찰률(%)", "예산대비(%)",
          "참가업체수", "개찰일시", "낙찰일자", "최초등록일", "확정일", "최종조회일", "링크"]


def kst_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9)


def parse_dt(s: str) -> datetime | None:
    s = (s or "").replace(" (개찰)", "").strip()
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10),
                   ("%Y%m%d%H%M", 12), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def to_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


# ---- 추적 목록 ----------------------------------------------------------------

def load_tracking(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as f:
        return {r["공고번호"]: r for r in csv.DictReader(f)}


def save_tracking(path: Path, rows: dict[str, dict]) -> None:
    ordered = sorted(rows.values(), key=lambda r: (r.get("마감일시") or ""), reverse=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)


def ingest_briefs(reports: Path, tracking: dict[str, dict]) -> int:
    """브리프 CSV에서 추적 대상 공고를 새로 추가한다. 이미 있으면 건너뛴다."""
    added = 0
    today = kst_now().strftime("%Y-%m-%d")
    for p in sorted(reports.glob("univ_bids_*.csv")):
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not (r.get("등급") in TRACK_GRADES or r.get("AX·DX") in TRACK_AX_TYPES):
                    continue
                key = r["공고번호"].rsplit("-", 1)[0]   # 차수와 무관하게 공고번호로 추적
                if key in tracking:
                    continue
                budget = r.get("예산(억원)") or (
                    f"{to_float(r.get('예산(원)')) / 1e8:.2f}" if r.get("예산(원)") else "")
                tracking[key] = {
                    "공고번호": key, "공고명": r["공고명"], "수요기관": r["수요기관"],
                    "등급": r["등급"], "구분": r.get("구분", ""), "예산(억원)": budget,
                    "마감일시": r.get("마감일시", ""), "상태": "대기",
                    "최초등록일": today, "링크": r.get("링크", ""),
                }
                added += 1
    return added


# ---- API ----------------------------------------------------------------------

def call_api(key: str, bid_no: str, with_dates: bool) -> list[dict]:
    params = {"serviceKey": key, "pageNo": 1, "numOfRows": 50, "type": "json",
              "inqryDiv": "4", "bidNtceNo": bid_no}
    if with_dates:   # 일부 환경에서 기간 파라미터를 요구하는 경우 대비
        end = kst_now()
        params["inqryBgnDt"] = (end - timedelta(days=200)).strftime("%Y%m%d0000")
        params["inqryEndDt"] = end.strftime("%Y%m%d2359")
    url = ENDPOINT + "?" + urllib.parse.urlencode(params, safe="")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 g2b-university-bids"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        hint = ""
        if e.code in (401, 403):
            hint = (" → 공공데이터포털에서 '조달청_나라장터 낙찰정보서비스' 활용신청이 "
                    "승인됐는지 확인하세요.")
        raise RuntimeError(f"낙찰정보 API HTTP {e.code}: {body or e.reason}{hint}")
    if raw.lstrip().startswith("<"):
        code = re.search(r"<returnReasonCode>(\d+)", raw)
        msg = re.search(r"<returnAuthMsg>([^<]+)", raw)
        raise RuntimeError(f"낙찰정보 API 오류 {code.group(1) if code else '?'}: "
                           f"{msg.group(1) if msg else raw[:200]}")
    payload = json.loads(raw)
    header = payload.get("response", {}).get("header", {})
    code = str(header.get("resultCode", "00"))
    if code == "03":
        return []
    if code not in ("00", "0"):
        raise RuntimeError(f"낙찰정보 API 결과코드 {code}: {header.get('resultMsg')}")
    items = payload.get("response", {}).get("body", {}).get("items") or []
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    return items


def lookup(key: str, bid_no: str) -> list[dict]:
    try:
        return call_api(key, bid_no, with_dates=False)
    except RuntimeError as e:
        if "HTTP 4" in str(e) and ("401" in str(e) or "403" in str(e)):
            raise
        return call_api(key, bid_no, with_dates=True)


def pick_award(items: list[dict]) -> dict | None:
    """같은 공고의 여러 차수·분류 중 낙찰자가 있는 최신 건."""
    won = [i for i in items if (i.get("bidwinnrNm") or "").strip()]
    if not won:
        return None
    return max(won, key=lambda i: (str(i.get("fnlSucsfDate") or ""),
                                   str(i.get("bidNtceOrd") or "")))


def apply_award(row: dict, it: dict, today: str) -> None:
    amt = to_float(it.get("sucsfbidAmt"))
    budget = to_float(row.get("예산(억원)")) * 1e8
    row.update({
        "상태": "낙찰",
        "낙찰업체": (it.get("bidwinnrNm") or "").strip(),
        "낙찰금액(억원)": f"{amt / 1e8:.2f}" if amt else "",
        "낙찰률(%)": f"{to_float(it.get('sucsfbidRate')):.2f}" if it.get("sucsfbidRate") else "",
        "예산대비(%)": f"{amt / budget * 100:.1f}" if amt and budget else "",
        "참가업체수": str(it.get("prtcptCnum") or ""),
        "개찰일시": str(it.get("rlOpengDt") or ""),
        "낙찰일자": str(it.get("fnlSucsfDate") or ""),
        "확정일": today,
        "최종조회일": today,
    })


def close_of(row: dict, now: datetime) -> datetime:
    """입찰 마감일시. 비어 있으면 최초 등록 후 2주를 마감으로 간주."""
    close = parse_dt(row.get("마감일시", ""))
    if close is None:
        first = parse_dt(row.get("최초등록일", "")) or now
        close = first + timedelta(days=14)
    return close


def is_closed(row: dict, now: datetime) -> bool:
    """마감일이 지난 공고만 낙찰 분석 대상."""
    return now >= close_of(row, now)


def refresh(tracking: dict[str, dict], key: str, fetch=lookup) -> dict:
    now = kst_now()
    today = now.strftime("%Y-%m-%d")
    stats = {"조회": 0, "신규낙찰": [], "확인불가": 0, "오류": None}
    for row in tracking.values():
        if row.get("상태") not in ("대기", "결과대기"):
            continue
        close = close_of(row, now)
        if now.date() < (close + timedelta(days=WAIT_AFTER_CLOSE_DAYS)).date():   # 날짜 기준
            continue
        if now > close + timedelta(days=GIVE_UP_DAYS):
            row["상태"] = "확인불가"
            stats["확인불가"] += 1
            continue
        if stats["조회"] >= MAX_CALLS:
            break
        try:
            items = fetch(key, row["공고번호"])
        except RuntimeError as e:
            stats["오류"] = str(e)
            break      # 키·권한 문제면 나머지도 같으므로 중단
        stats["조회"] += 1
        award = pick_award(items)
        if award:
            apply_award(row, award, today)
            stats["신규낙찰"].append(row)
        else:
            row["상태"] = "결과대기"
            row["최종조회일"] = today
        time.sleep(0.2)
    return stats


# ---- 리포트 ---------------------------------------------------------------------

def report_md(tracking: dict[str, dict], stats: dict) -> str:
    """마감일이 지난 공고만 분석한다. 마감 전 공고는 집계·표에서 모두 제외."""
    now = kst_now()
    rows = [r for r in tracking.values() if is_closed(r, now)]
    upcoming = len(tracking) - len(rows)
    awarded = [r for r in rows if r.get("상태") == "낙찰"]
    waiting = [r for r in rows if r.get("상태") in ("대기", "결과대기")]
    gave_up = [r for r in rows if r.get("상태") == "확인불가"]
    lines = ["## 낙찰 결과 (마감 지난 추적 공고)", ""]
    if stats.get("오류"):
        lines += [f"> ⚠️ 낙찰정보 조회 중 오류로 일부만 확인했습니다: {stats['오류']}", ""]
    lines += [f"마감 지난 공고 **{len(rows)}건** 기준 · 낙찰 확정 {len(awarded)}건 · "
              f"결과 대기 {len(waiting)}건 · 확인불가 {len(gave_up)}건 "
              f"(마감 전 {upcoming}건은 분석 제외)", ""]

    new = sorted(stats.get("신규낙찰", []), key=lambda r: -to_float(r.get("예산(억원)")))
    lines += [f"### 이번에 확정된 낙찰 ({len(new)}건)", ""]
    if new:
        lines += ["| 공고명 | 수요기관 | 등급 | 예산(억원) | 낙찰업체 | 낙찰금액(억원) | 예산대비 | 참가 |",
                  "|---|---|---|---:|---|---:|---:|---:|"]
        for r in new:
            name = f"[{r['공고명']}]({r['링크']})" if r.get("링크") else r["공고명"]
            ratio = f"{r['예산대비(%)']}%" if r.get("예산대비(%)") else "-"
            lines.append(f"| {name} | {r['수요기관']} | {r['등급']} | {r.get('예산(억원)') or '-'} | "
                         f"{r['낙찰업체']} | {r.get('낙찰금액(억원)') or '-'} | {ratio} | "
                         f"{r.get('참가업체수') or '-'} |")
    else:
        lines.append("이번 조회에서 새로 확정된 낙찰은 없습니다.")
    lines.append("")

    if waiting:
        lines += [f"### 결과 대기 ({len(waiting)}건, 마감 경과일순)", "",
                  "| 공고명 | 수요기관 | 등급 | 예산(억원) | 마감 | 경과 |",
                  "|---|---|---|---:|---|---:|"]
        for r in sorted(waiting, key=lambda r: close_of(r, now)):
            name = f"[{r['공고명'].strip()}]({r['링크']})" if r.get("링크") else r["공고명"].strip()
            days = (now - close_of(r, now)).days
            lines.append(f"| {name} | {r['수요기관']} | {r['등급']} | "
                         f"{r.get('예산(억원)') or '-'} | {(r.get('마감일시') or '-')[:10]} | {days}일 |")
        lines.append("")

    if awarded:
        tally: dict[str, list] = {}
        for r in awarded:
            tally.setdefault(r["낙찰업체"], []).append(r)
        top = sorted(tally.items(), key=lambda kv: (-len(kv[1]),
                     -sum(to_float(x.get("낙찰금액(억원)")) for x in kv[1])))[:10]
        lines += ["### 누적 낙찰업체 (추적 공고 기준, 상위 10)", "",
                  "| 낙찰업체 | 건수 | 낙찰금액 합계(억원) | 평균 예산대비 | 주요 발주처 |",
                  "|---|---:|---:|---:|---|"]
        for name, rs in top:
            total = sum(to_float(x.get("낙찰금액(억원)")) for x in rs)
            ratios = [to_float(x["예산대비(%)"]) for x in rs if x.get("예산대비(%)")]
            avg = f"{sum(ratios) / len(ratios):.1f}%" if ratios else "-"
            orgs = ", ".join(dict.fromkeys(x["수요기관"] for x in rs))[:60]
            lines.append(f"| {name} | {len(rs)} | {total:.2f} | {avg} | {orgs} |")
        lines.append("")

    lines += ["- 예산대비 = 낙찰금액 ÷ 공고 예산. 협상계약은 기술평가 후 가격협상으로 정해져 "
              "100%에 가까울 수 있습니다.",
              "- 입찰 마감일이 지난 공고만 분석합니다. 마감 전 공고는 추적 목록에만 있고 이 섹션에는 나오지 않습니다.",
              f"- 마감 후 {WAIT_AFTER_CLOSE_DAYS}일부터 조회하고, {GIVE_UP_DAYS}일이 지나도 "
              "결과가 없으면(유찰·취소 등) '확인불가'로 둡니다.", ""]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="추적 공고 낙찰 결과 조회")
    p.add_argument("--reports", default="reports", help="브리프가 쌓이는 폴더")
    p.add_argument("--append-to", help="결과 섹션을 붙일 브리프 .md")
    p.add_argument("--ingest-only", action="store_true", help="추적 목록만 갱신, API 조회 안 함")
    p.add_argument("--mock", help="테스트용: {공고번호: [item,...]} JSON 파일")
    a = p.parse_args()

    reports = Path(a.reports)
    reports.mkdir(parents=True, exist_ok=True)
    track_path = reports / "award_tracking.csv"
    tracking = load_tracking(track_path)
    added = ingest_briefs(reports, tracking)

    stats = {"조회": 0, "신규낙찰": [], "확인불가": 0, "오류": None}
    if not a.ingest_only:
        if a.mock:
            data = json.loads(Path(a.mock).read_text(encoding="utf-8"))
            def fetch(_k, no):
                v = data.get(no)
                if isinstance(v, str):
                    raise RuntimeError(v)
                return v or []
            stats = refresh(tracking, "mock", fetch)
        else:
            key = (os.environ.get("G2B_SERVICE_KEY") or "").strip().strip('"')
            if not key:
                print("❌ G2B_SERVICE_KEY 환경변수가 없습니다.", file=sys.stderr)
                return 2
            stats = refresh(tracking, key)

    save_tracking(track_path, tracking)
    md = report_md(tracking, stats)
    stamp = kst_now().strftime("%Y%m%d")
    (reports / f"awards_{stamp}.md").write_text(
        f"# 낙찰 결과 추적 ({kst_now():%Y-%m-%d})\n\n" + md, encoding="utf-8")
    if a.append_to and Path(a.append_to).exists():
        target = Path(a.append_to)
        text = target.read_text(encoding="utf-8")
        if "## 낙찰 결과 (" not in text:
            target.write_text(text.rstrip("\n") + "\n\n" + md, encoding="utf-8")

    print(json.dumps({"tracked": len(tracking), "added": added, "queried": stats["조회"],
                      "new_awards": len(stats["신규낙찰"]), "error": stats["오류"]},
                     ensure_ascii=False))
    return 1 if stats["오류"] and stats["조회"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
