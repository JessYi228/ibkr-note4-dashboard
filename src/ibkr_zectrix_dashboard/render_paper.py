"""Paper-account screen (400x300, one bit): one section per book, one row per strategy.

Each section bar names the book, its start date and the SPY/QQQ buy-and-hold
returns over the same period, so every strategy row can be read against them.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from . import drawing as d
from .paper import PaperAccount, PaperBoard

TOP = 51
FOOTER_Y = 286
BAR_HEIGHT = 14
MAX_ROW = 19
MIN_ROW = 13
COLUMNS = {"spark": (104, 160), "equity": 220, "total": 274, "vs": 330, "day": 390}


def _columns_header(draw, fonts: dict) -> None:
    font = fonts["bold10"]
    draw.text((10, 37), "ACCOUNT", font=font, fill=0)
    spark_left, spark_right = COLUMNS["spark"]
    label = "SINCE START"
    draw.text(((spark_left + spark_right - draw.textlength(label, font=font)) / 2, 37), label, font=font, fill=0)
    for key, label in (("equity", "EQUITY"), ("total", "TOTAL"), ("vs", "VS SPY"), ("day", "DAY")):
        draw.text((COLUMNS[key] - draw.textlength(label, font=font), 37), label, font=font, fill=0)
    draw.line((6, 49, 394, 49), fill=0)


def _bar(draw, y: int, book, fonts: dict) -> None:
    draw.rectangle((4, y, 395, y + BAR_HEIGHT - 2), fill=0)
    since = f" · SINCE {book.start:%m/%d}" if book.start else ""
    d.text(draw, (9, y + 1), f"{book.key}{since}", fonts["bold10"], 255)
    if book.problem:
        status = f"! {book.problem}"
    else:
        status = "  ".join(f"{name} {d.pct(value)}" for name, value in sorted(book.benchmarks.items(), reverse=True))
    d.right(draw, 390, y + 1, d.fit(draw, status, fonts["r10"], 230), fonts["r10"], 255)


def _row(draw, y: int, height: int, account: PaperAccount, spy: float | None, fonts: dict, last: bool) -> None:
    name_font = fonts["r12"] if height >= 16 else fonts["r10"]
    text_y = y + (height - (13 if name_font is fonts["r12"] else 11)) // 2
    num_y = y + (height - 11) // 2
    draw.text((10, text_y), d.fit(draw, account.label, name_font, 90), font=name_font, fill=0)
    spark_left, spark_right = COLUMNS["spark"]
    d.sparkline(draw, account.closes, (spark_left, y + 3, spark_right, y + height - 4), baseline=account.initial)
    d.right(draw, COLUMNS["equity"], num_y, f"{account.equity:,.0f}", fonts["r10"])
    d.right(draw, COLUMNS["total"], num_y, d.pct(account.total_pct), fonts["r10"])
    if spy is not None:
        d.right(draw, COLUMNS["vs"], num_y, f"{account.total_pct - spy:+.2f}", fonts["r10"])
    if account.halted:
        width = draw.textlength("HALTED", font=fonts["bold10"])
        draw.rectangle((COLUMNS["day"] - width - 4, y + 2, COLUMNS["day"] + 1, y + height - 3), fill=0)
        draw.text((COLUMNS["day"] - width - 1, num_y - 1), "HALTED", font=fonts["bold10"], fill=255)
    else:
        d.arrow_right(draw, COLUMNS["day"], num_y, account.day_pct, d.pct(account.day_pct) or "N/A", fonts["r10"])
    if not last:
        for x in range(10, 391, 2):
            draw.point((x, y + height - 1), fill=0)


def render_paper_png(board: PaperBoard, *, tz: ZoneInfo, font_path: str = "") -> bytes:
    fonts = d.load_fonts(font_path)
    image, draw = d.new_canvas()
    problems = board.problems
    if problems:
        d.alert_header(draw, f"! CHECK PAPER RUNNERS ({len(problems)})", " · ".join(problems), fonts)
    else:
        d.header(draw, "PAPER ACCOUNTS", board.data_session, board.fetched_at, tz, fonts)
    _columns_header(draw, fonts)

    rows = sum(max(len(book.accounts), 1) for book in board.books)
    available = FOOTER_Y - 2 - TOP - BAR_HEIGHT * len(board.books)
    height = max(MIN_ROW, min(MAX_ROW, available // max(rows, 1)))
    y = TOP
    for book in board.books:
        if y + BAR_HEIGHT + height > FOOTER_Y:
            break
        _bar(draw, y, book, fonts)
        y += BAR_HEIGHT
        if not book.accounts:
            draw.text((10, y + 2), "NO ACCOUNTS", font=fonts["r10"], fill=0)
            y += height
            continue
        for index, account in enumerate(book.accounts):
            if y + height > FOOTER_Y - 2:
                break
            _row(draw, y, height, account, book.benchmarks.get("SPY"), fonts, index == len(book.accounts) - 1)
            y += height

    draw.line((6, FOOTER_Y - 2, 394, FOOTER_Y - 2), fill=0)
    initials = {account.initial for book in board.books for account in book.accounts}
    start = f" · {initials.pop() / 1000:,.0f}K START EACH" if len(initials) == 1 else ""
    d.text(draw, (10, FOOTER_Y), f"SIMULATED PAPER MONEY, NOT REAL TRADES{start}", fonts["r10"])
    return d.to_png(image)
