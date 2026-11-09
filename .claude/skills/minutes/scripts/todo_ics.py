#!/usr/bin/env python3
"""Build and verify todo.ics from the ToDo table of a minutes file.

    todo_ics.py build minutes/2026-10-01.md --ics todo.ics
    todo_ics.py check minutes/2026-10-01.md --ics todo.ics

The minutes file must be named YYYY-MM-DD.md. Only standard library is used.
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

DOMAIN = "mihon-kyoshitsu.example"
NO_DATE = {"発言なし", "【不明】"}
TOP_LEVEL = {"VERSION", "PRODID", "CALSCALE", "METHOD"}
CALENDAR_HEADER = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//cc-practice//mail-minutes todo//JA",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
]


class IcsError(Exception):
    pass


def meeting_date(minutes):
    try:
        return dt.date.fromisoformat(minutes.stem)
    except ValueError:
        raise IcsError(f"議事録のファイル名が YYYY-MM-DD.md ではありません: {minutes}")


def split_row(line):
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [cell.strip().replace("\\|", "|") for cell in cells]


def parse_todos(minutes):
    """Return (dated, undated) rows; dated rows are (date, content, owner)."""
    lines = minutes.read_text(encoding="utf-8-sig").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## ToDo") + 1
    except StopIteration:
        raise IcsError("議事録に「## ToDo」の節がありません")

    rows = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.strip().startswith("|"):
            rows.append(split_row(line))
    if not rows or rows[0] != ["内容", "担当", "期限"]:
        raise IcsError("ToDo の表の見出しは「内容 | 担当 | 期限」にしてください")

    dated, undated = [], []
    for cells in rows[1:]:
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if len(cells) != 3:
            raise IcsError(f"ToDo の行の列数が 3 ではありません: {cells}")
        content, owner, due = cells
        if due in NO_DATE:
            undated.append((content, owner, due))
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise IcsError(f"期限の形式が YYYY-MM-DD ではありません: {due}({content})")
        try:
            dated.append((dt.date.fromisoformat(due), content, owner))
        except ValueError:
            raise IcsError(f"存在しない日付です: {due}({content})")
    return dated, undated


def escape(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line):
    """Fold at 75 octets without splitting a UTF-8 character (RFC 5545 3.1)."""
    parts, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        limit = 74 if parts else 75  # continuation lines start with a space
        if size + width > limit:
            parts.append(current)
            current, size = "", 0
        current += char
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def read_calendar(path):
    """Return (top-level lines, events as lists of unfolded lines)."""
    data = path.read_bytes().removeprefix(b"\xef\xbb\xbf")
    try:
        text = data.replace(b"\r\n ", b"").replace(b"\r\n\t", b"").decode("utf-8")
    except UnicodeDecodeError:
        raise IcsError(f"{path} が UTF-8 ではありません")
    lines = text.split("\r\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise IcsError(f"{path} が BEGIN:VCALENDAR で始まり END:VCALENDAR で終わっていません")

    top, events, current = [], [], None
    for line in lines[1:-1]:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise IcsError("VEVENT が閉じられないまま次の VEVENT が始まっています")
            current = [line]
        elif line == "END:VEVENT":
            if current is None:
                raise IcsError("BEGIN:VEVENT のない END:VEVENT があります")
            current.append(line)
            events.append(current)
            current = None
        elif current is not None:
            current.append(line)
        else:
            if line.split(":", 1)[0].split(";", 1)[0] not in TOP_LEVEL:
                raise IcsError(f"このスクリプトが扱わない行があります: {line}")
            top.append(line)
    if current is not None:
        raise IcsError("END:VEVENT がありません")
    return top, events


def properties(event):
    """Return {name: (parameters, value)}; repeated names keep the last value."""
    props = {}
    for line in event[1:-1]:
        head, _, value = line.partition(":")
        name, *params = head.split(";")
        props[name] = (params, value)
    return props


def uid_prefix(day):
    return f"meeting-{day:%Y%m%d}-todo-"


def uid_for(day, number):
    return f"{uid_prefix(day)}{number:02d}@{DOMAIN}"


def summary_for(content, owner):
    return escape(f"[期限] {content}({owner})")


def make_event(meeting, number, due, content, owner, minutes, stamp):
    description = (
        f"{escape('担当: ' + owner)}\\n期限: {due}\\n"
        f"{escape('元資料: ' + minutes.as_posix())}"
    )
    return [
        "BEGIN:VEVENT",
        f"UID:{uid_for(meeting, number)}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
        f"DTEND;VALUE=DATE:{due + dt.timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{summary_for(content, owner)}",
        f"DESCRIPTION:{description}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


def build(minutes, ics):
    meeting = meeting_date(minutes)
    dated, undated = parse_todos(minutes)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    kept = []
    if ics.exists():
        _, existing = read_calendar(ics)
        prefix = uid_prefix(meeting)
        kept = [
            e for e in existing
            if not properties(e).get("UID", ([], ""))[1].startswith(prefix)
        ]

    new = [
        make_event(meeting, n, due, content, owner, minutes, stamp)
        for n, (due, content, owner) in enumerate(dated, 1)
    ]
    events = sorted(
        kept + new,
        key=lambda e: (properties(e).get("DTSTART", ([], ""))[1], properties(e).get("UID", ([], ""))[1]),
    )
    lines = CALENDAR_HEADER + [l for e in events for l in e] + ["END:VCALENDAR"]
    ics.write_bytes(("\r\n".join(fold(l) for l in lines) + "\r\n").encode("utf-8"))

    print(f"書き込み: {ics}(この会議 {len(new)} 件、他の会議 {len(kept)} 件)")
    report_undated(undated)
    return 0


def check(minutes, ics):
    meeting = meeting_date(minutes)
    dated, undated = parse_todos(minutes)
    errors = []

    raw = ics.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("先頭に BOM があります")
    if not raw.endswith(b"\r\n"):
        errors.append("最終行が CRLF で終わっていません")
    for number, line in enumerate(raw.split(b"\r\n"), 1):
        if b"\n" in line or b"\r" in line:
            errors.append(f"{number} 行目: 改行が CRLF ではありません")
        if len(line) > 75:
            errors.append(f"{number} 行目: {len(line)} オクテットで 75 を超えています")

    top, events = read_calendar(ics)
    if "VERSION:2.0" not in top:
        errors.append("VERSION:2.0 がありません")
    if not any(l.startswith("PRODID:") for l in top):
        errors.append("PRODID がありません")

    seen = {}
    for event in events:
        props = properties(event)
        uid = props.get("UID", ([], ""))[1]
        label = uid or "(UID なし)"
        missing = {"UID", "DTSTAMP", "DTSTART", "SUMMARY"} - props.keys()
        if missing:
            errors.append(f"{label}: 必須項目がありません: {sorted(missing)}")
            continue
        if uid in seen:
            errors.append(f"UID が重複しています: {uid}")
        seen[uid] = props
        if not re.fullmatch(r"\d{8}T\d{6}Z", props["DTSTAMP"][1]):
            errors.append(f"{label}: DTSTAMP の形式が不正です")
        try:
            start = dt.datetime.strptime(props["DTSTART"][1], "%Y%m%d").date()
            if "VALUE=DATE" not in props["DTSTART"][0]:
                errors.append(f"{label}: DTSTART が VALUE=DATE ではありません")
            if "DTEND" in props:
                end = dt.datetime.strptime(props["DTEND"][1], "%Y%m%d").date()
                if end != start + dt.timedelta(days=1):
                    errors.append(f"{label}: DTEND が DTSTART の翌日ではありません")
        except ValueError:
            errors.append(f"{label}: 日付が不正です")

    prefix = uid_prefix(meeting)
    actual = {uid for uid in seen if uid.startswith(prefix)}
    expected = {}
    for number, (due, content, owner) in enumerate(dated, 1):
        expected[uid_for(meeting, number)] = (due, summary_for(content, owner), content)
    for uid in sorted(actual - expected.keys()):
        errors.append(f"議事録にない予定があります: {uid}")
    for uid, (due, summary, content) in expected.items():
        if uid not in seen:
            errors.append(f"予定がありません: {due} {content}")
            continue
        if seen[uid]["DTSTART"][1] != f"{due:%Y%m%d}":
            errors.append(f"{uid}: 日付が議事録と違います({due})")
        if seen[uid]["SUMMARY"][1] != summary:
            errors.append(f"{uid}: SUMMARY が議事録と違います({content})")

    if errors:
        print(f"NG: {ics}({len(errors)} 件)")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"OK: {ics}(全 {len(events)} 件、この会議 {len(expected)} 件が議事録と一致)")
    report_undated(undated)
    return 0


def report_undated(undated):
    if undated:
        print("todo.ics に入れなかった ToDo:")
        for content, owner, due in undated:
            print(f"  - {content}(担当: {owner}、期限: {due})")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["build", "check"])
    parser.add_argument("minutes", type=Path)
    parser.add_argument("--ics", type=Path, default=Path("todo.ics"))
    args = parser.parse_args()
    try:
        return {"build": build, "check": check}[args.command](args.minutes, args.ics)
    except (IcsError, OSError) as error:
        print(f"エラー: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
