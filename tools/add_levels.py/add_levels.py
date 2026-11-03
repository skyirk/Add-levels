#!/usr/bin/env python3
"""TUF 맵 번호만 적으면 levels.json 에 자동으로 추가해 주는 도구.

사용법 (저장소 폴더에서):
    python tools/add_levels.py tools/new_levels.txt
    python tools/add_levels.py tools/new_levels.txt --dry     # 파일은 건드리지 않고 결과만 출력

입력 파일은 한 줄에 맵 하나, 형식은 다음과 같습니다.
    맵번호(또는 포럼 링크)  PGU난이도  키개수  패턴1,패턴2,...
    7684 U12 16 체력,질주,복합
    6206 U13 10제한 체력,질주,기믹
    https://tuforums.com/levels/87 U11 16 질주,복합
- 키 개수 뒤에 "제한"을 붙이면(예: 10제한) 키 제한 맵으로 기록됩니다. 16키를 넘는 맵은 16+ 라고 적습니다.
- 패턴은 쉼표로 이어 쓰고 띄어쓰기는 쓰지 않습니다. 사용 가능: 질주 복합 체력 개박 동타 마법진 기믹 판정제한
- 이름, 제작자, 채보자는 포럼 맵 페이지의 제목/설명 태그에서 자동으로 읽어옵니다.
- 레거시 난이도는 TUF 난이도표(U1~U16)로 자동 계산합니다. P, G 는 표가 없어서 비워 둡니다.
- 이미 있는 맵 번호는 새 값으로 덮어씁니다. 실행 전에 levels.json.bak 으로 백업합니다.
- 포럼에 부담을 주지 않도록 맵 사이에 1초씩 쉽니다. 한 번에 수십 개 정도까지만 쓰세요.
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://tuforums.com/levels/"
PATTERNS = ["질주", "복합", "체력", "개박", "동타", "마법진", "기믹", "판정제한"]
U_TO_LEGACY = {
    1: "21.0", 2: "21.0", 3: "21.0+", 4: "21.0+", 5: "21.1", 6: "21.1", 7: "21.1+", 8: "21.1+",
    9: "21.2", 10: "21.2", 11: "21.2+", 12: "21.2+", 13: "21.3", 14: "21.3", 15: "21.3+", 16: "21.3+",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ADOFAI-ANALYZER/1.0 (personal level list tool)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def meta(page, key):
    """<meta property|name="key" content="..."> 값을 읽는다 (속성 순서, 따옴표 종류 상관없음)."""
    k = re.escape(key)
    patterns = (
        (rf'<meta\b[^>]*?(?:property|name)=(["\'])({k})\1[^>]*?content=(["\'])(.*?)\3', 4),
        (rf'<meta\b[^>]*?content=(["\'])(.*?)\1[^>]*?(?:property|name)=(["\'])({k})\3', 2),
    )
    for pat, group in patterns:
        m = re.search(pat, page, re.I | re.S)
        if m:
            return html.unescape(m.group(group)).strip()
    return ""


def parse_page(page):
    """페이지에서 (이름, 제작자, 채보자 문자열)을 뽑는다. 못 찾은 값은 빈 문자열."""
    title = meta(page, "og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = html.unescape(m.group(1)).strip() if m else ""
    title = re.sub(r"\s*[|\-–]\s*The Universal Forums.*$", "", title).strip()
    name, artist = title, ""
    if " - " in title:
        name, artist = title.rsplit(" - ", 1)
    desc = meta(page, "og:description") or meta(page, "description")
    charters = ""
    m = re.search(r"charted by\s*(.+)", desc, re.I)
    if m:
        txt = m.group(1).strip()
        txt = re.sub(r"\s*&\s*(\d+)\s*more", r" 외 \1명", txt)   # "A & 3 more" -> "A 외 3명"
        charters = re.sub(r"\s*\|\s*", ", ", txt)                 # "A | B" -> "A, B"
    return name.strip(), artist.strip(), charters.strip()


def parse_line(line):
    """입력 한 줄을 (id, pgu, keys, limited, patterns)로 바꾼다. 형식이 틀리면 ValueError."""
    t = line.split()
    if len(t) < 4:
        raise ValueError("형식: 맵번호 난이도 키개수 패턴1,패턴2  (예: 7684 U12 16 체력,질주)")
    m = re.search(r"(\d+)\s*$", t[0].rstrip("/"))
    if not m:
        raise ValueError(f"맵 번호를 못 찾았습니다: {t[0]}")
    tuf_id = int(m.group(1))
    pgu = t[1].upper()
    if not re.fullmatch(r"[PGU]\d{1,2}", pgu):
        raise ValueError(f"난이도 형식이 이상합니다: {t[1]} (예: U12)")
    km = re.match(r"(\d+)(\+?)", t[2])
    if not km:
        raise ValueError(f"키 개수를 못 읽었습니다: {t[2]}")
    keys = int(km.group(1)) + (1 if km.group(2) else 0)   # "16+" 는 17 로 저장 (앱에서 16+키로 표시)
    limited = "제한" in t[2]
    pats = [p for p in re.split(r"[,，\s]+", " ".join(t[3:])) if p]   # 쉼표·띄어쓰기 모두 구분자로 인정
    bad = [p for p in pats if p not in PATTERNS]
    if bad:
        raise ValueError(f"모르는 패턴: {', '.join(bad)} (가능: {' '.join(PATTERNS)})")
    return tuf_id, pgu, keys, limited, pats


def legacy_for(pgu):
    if pgu.startswith("U"):
        return U_TO_LEGACY.get(int(pgu[1:]))
    return None


def build_entry(tuf_id, pgu, keys, limited, pats, base):
    name, artist, charters, note = f"TUF #{tuf_id} (이름 미확인)", "제작자 미확인", "", ""
    try:
        name_, artist_, charters = parse_page(fetch(f"{base}{tuf_id}"))
        if name_ and name_.lower() != "level":
            name = name_
            artist = artist_ or artist
        else:
            note = "포럼 페이지에서 이름을 읽지 못했습니다."
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        note = f"포럼 접속 실패: {e}"
    desc = f"채보: {charters}." if charters else ""
    entry = {
        "id": f"tuf-{tuf_id}", "name": name, "creator": artist, "pgu": pgu, "legacy": legacy_for(pgu),
        "keys": keys, "keyLimit": limited, "bpm": None, "patterns": pats, "reference": False, "desc": desc,
        "tufId": tuf_id, "videoUrl": "",
    }
    return entry, note


def main():
    ap = argparse.ArgumentParser(description="TUF 맵을 levels.json 에 자동 추가")
    ap.add_argument("input", help="맵 목록 텍스트 파일")
    ap.add_argument("--levels", default=str(Path(__file__).resolve().parent.parent / "levels.json"))
    ap.add_argument("--base", default=BASE, help="맵 페이지 주소 앞부분 (테스트용)")
    ap.add_argument("--delay", type=float, default=1.0, help="맵 사이 대기 시간(초)")
    ap.add_argument("--dry", action="store_true", help="저장하지 않고 결과만 출력")
    args = ap.parse_args()

    lv_path = Path(args.levels)
    levels = json.loads(lv_path.read_text(encoding="utf-8")) if lv_path.exists() else []
    added, problems = [], 0
    lines = [l.strip() for l in Path(args.input).read_text(encoding="utf-8").splitlines()]
    for no, line in enumerate(lines, 1):
        if not line or line.startswith("#"):
            continue
        try:
            tuf_id, pgu, keys, limited, pats = parse_line(line)
        except ValueError as e:
            print(f"[{no}줄] 건너뜀: {e}\n        {line}")
            problems += 1
            continue
        entry, note = build_entry(tuf_id, pgu, keys, limited, pats, args.base)
        if note:
            print(f"[{no}줄] 주의 #{tuf_id}: {note}")
            problems += 1
        lv = entry["legacy"] or "-"
        print(f"  #{tuf_id:<6} {entry['name']}  /  {entry['creator']}  /  {pgu}({lv})  {keys}키  {','.join(pats)}")
        levels = [x for x in levels if x.get("id") != entry["id"]] + [entry]
        added.append(entry)
        time.sleep(args.delay)

    print(f"\n읽은 맵 {len(added)}개, 확인 필요 {problems}건")
    if args.dry or not added:
        print("(저장하지 않았습니다)" if args.dry else "")
        return 0
    if lv_path.exists():
        lv_path.with_name(lv_path.name + ".bak").write_text(lv_path.read_text(encoding="utf-8"), encoding="utf-8")
    lv_path.write_text(json.dumps(levels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{lv_path} 에 저장했습니다. (총 {len(levels)}개, 백업: {lv_path.name}.bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
