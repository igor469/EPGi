# -*- coding: utf-8 -*-
"""Interactive IPTV EPG viewer.

This module implements a console program that allows a user to browse
electronic program guides (EPG) from several providers.  The behaviour
follows the description from :file:`EPGi.txt`:

* URLs of EPG providers are read from ``EPGi.ini``.
* Information about execution is appended to ``EPGi.log``.
* The interface consists of three screens which are navigated with the
  keyboard.

The implementation here is intentionally compact but functional.  It is
not optimised for huge guides, yet it honours the main workflow: EPG data
is loaded only once for every provider, tables are rendered with fixed
column widths and the user can navigate between the three screens using
the keys described in the specification.

The program requires an actual terminal because it relies on the
``curses`` module for the UI.  When executed inside a non-interactive
environment the program will simply terminate.
"""

from __future__ import annotations

import configparser
import curses
import datetime as dt
import gzip
import io
import logging
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

try:  # ``urllib`` is part of the standard library and always available.
    from urllib.request import urlopen
except ImportError:  # pragma: no cover - very unlikely on CPython.
    urlopen = None  # type: ignore


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = "EPGi.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

LOGGER = logging.getLogger("EPGi")
LOGGER.info("Program started")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def read_config(filename: str = "EPGi.ini") -> Dict[int, str]:
    """Read provider URLs from the configuration file.

    Parameters
    ----------
    filename:
        Path to the ``.ini`` file.

    Returns
    -------
    dict
        Mapping ``provider_number`` -> ``url``.
    """

    parser = configparser.ConfigParser()
    parser.read(filename, encoding="utf-8")

    urls: Dict[int, str] = {}
    for key, value in parser["DEFAULT"].items():
        if key.startswith("url"):
            try:
                idx = int(key[3:])
            except ValueError:
                continue
            urls[idx] = value.strip()
    return dict(sorted(urls.items()))


# ---------------------------------------------------------------------------
# EPG loading and parsing
# ---------------------------------------------------------------------------


def _parse_time(value: str) -> dt.datetime:
    """Parse timestamps of the form ``YYYYMMDDHHMMSS``."""

    if not value:
        return dt.datetime.min
    # EPG timestamps often contain timezone information after the main
    # value (``+0000``).  We ignore it as only the relative order matters
    # for the UI.
    value = value[:14]
    return dt.datetime.strptime(value, "%Y%m%d%H%M%S")


def load_epg(url: str) -> Dict[str, Dict[str, List[dict]]]:
    """Download and parse an EPG file.

    The result is a dictionary with two keys:
    ``channels`` – mapping of channel id to channel name and
    ``programmes`` – mapping of channel id to a list of programme
    dictionaries with ``start``, ``stop`` and ``title``.
    """

    if urlopen is None:
        raise RuntimeError("urllib is not available")

    LOGGER.info("Loading EPG: %s", url)
    with urlopen(url) as fh:  # type: ignore[arg-type]
        data = fh.read()
    if url.endswith(".gz"):
        data = gzip.decompress(data)

    import xml.etree.ElementTree as ET

    tree = ET.fromstring(data)

    channels: Dict[str, str] = {}
    for ch in tree.findall("channel"):
        cid = ch.get("id", "")
        name = ch.findtext("display-name", default=cid)
        channels[cid] = name

    programmes: Dict[str, List[dict]] = defaultdict(list)
    for prog in tree.findall("programme"):
        cid = prog.get("channel", "")
        title = prog.findtext("title", default="")
        start = _parse_time(prog.get("start", ""))
        stop = _parse_time(prog.get("stop", ""))
        programmes[cid].append({"start": start, "stop": stop, "title": title})

    for progs in programmes.values():
        progs.sort(key=lambda x: x["start"])

    return {"channels": channels, "programmes": programmes}


# Cache EPG files by provider number -------------------------------------------------

EPG_CACHE: Dict[int, Dict[str, Dict[str, List[dict]]]] = {}


def get_epg(provider_no: int, url: str):
    if provider_no not in EPG_CACHE:
        try:
            EPG_CACHE[provider_no] = load_epg(url)
        except Exception as exc:  # pragma: no cover - network errors
            LOGGER.error("Failed to load EPG %s: %s", url, exc)
            EPG_CACHE[provider_no] = {"channels": {}, "programmes": {}}
    return EPG_CACHE[provider_no]


# ---------------------------------------------------------------------------
# Helper utilities for rendering
# ---------------------------------------------------------------------------


def _render_table(
    stdscr: "curses._CursesWindow",
    rows: List[str],
    selected: int,
    top: int,
):
    """Render a list of strings starting at ``top``."""

    height, width = stdscr.getmaxyx()
    stdscr.erase()

    for idx in range(height):
        row_idx = top + idx
        if row_idx >= len(rows):
            break
        line = rows[row_idx]
        attr = curses.A_REVERSE if row_idx == selected else curses.A_NORMAL
        stdscr.addnstr(idx, 0, line, width - 1, attr)
    stdscr.refresh()


# ---------------------------------------------------------------------------
# Screen 1 – provider list
# ---------------------------------------------------------------------------


def screen1(
    stdscr: "curses._CursesWindow",
    providers: List[Tuple[int, str]],
    selected: int = 0,
) -> Tuple[int, int]:
    """Provider selection screen."""

    rows = [f"{num} {url}" for num, url in providers]

    top = max(0, selected)
    while True:
        _render_table(stdscr, rows, selected, 0)
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(providers)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(providers)
        elif key in (curses.KEY_RIGHT, ord("\n")):
            return 2, selected
        elif key in (curses.KEY_LEFT, 27):  # ESC
            return 0, selected


# ---------------------------------------------------------------------------
# Screen 2 – channels and current programmes
# ---------------------------------------------------------------------------


def _channel_rows(epg: dict) -> List[Tuple[str, dict]]:
    now = dt.datetime.now()
    rows: List[Tuple[str, dict]] = []
    channels = epg["channels"]
    progs = epg["programmes"]
    for cid, name in channels.items():
        plist = progs.get(cid, [])
        for idx, prog in enumerate(plist):
            if prog["start"] <= now < prog["stop"]:
                rows.append((cid, {"name": name, "prog": prog}))
                break
    rows.sort(key=lambda x: x[1]["name"].lower())
    return rows


def screen2(
    stdscr: "curses._CursesWindow",
    epg: dict,
    selected: int = 0,
) -> Tuple[int, int]:
    """Channel list for a provider."""

    channels = _channel_rows(epg)
    if not channels:
        stdscr.addstr(0, 0, "No data")
        stdscr.getch()
        return 1, 0

    top = 0
    while True:
        height, width = stdscr.getmaxyx()
        name_w = 20
        perc_w = 3
        bar_w = 10
        # use the remaining width for programme titles so the table spans
        # the whole screen width
        title_w = max(1, width - name_w - perc_w - bar_w - 3)

        rows = []
        now = dt.datetime.now()
        for cid, info in channels:
            prog = info["prog"]
            name = info["name"][:name_w]
            title = prog["title"][:title_w]
            start, stop = prog["start"], prog["stop"]
            if stop <= start:
                percent = 0
            else:
                percent = int(100 * (now - start).total_seconds() / (stop - start).total_seconds())
                percent = max(0, min(100, percent))
            bar = ("#" * (percent // 10)).ljust(bar_w)
            row = f"{name:<{name_w}} {title:<{title_w}} {percent:>{perc_w}} {bar}"
            rows.append(row)

        _render_table(stdscr, rows, selected, top)
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            if selected > 0:
                selected -= 1
            if selected < top:
                top = selected
        elif key in (curses.KEY_DOWN, ord("j")):
            if selected < len(rows) - 1:
                selected += 1
            if selected >= top + height:
                top += 1
        elif key == curses.KEY_NPAGE:  # PageDown
            step = height
            max_top = max(0, len(rows) - step)
            top = min(max_top, top + step)
            selected = min(len(rows) - 1, top + step - 1)
        elif key == curses.KEY_PPAGE:  # PageUp
            step = height
            top = max(0, top - step)
            selected = top
        elif key == curses.KEY_HOME:
            selected, top = 0, 0
        elif key == curses.KEY_END:
            selected = len(rows) - 1
            top = max(0, len(rows) - height)
        elif key in (curses.KEY_RIGHT, ord("\n")):
            return 3, selected
        elif key in (curses.KEY_LEFT, 27):
            return 1, selected


# ---------------------------------------------------------------------------
# Screen 3 – programme list for a channel
# ---------------------------------------------------------------------------


def screen3(
    stdscr: "curses._CursesWindow",
    epg: dict,
    channel_idx: int,
    selected: int = 0,
) -> Tuple[int, int]:
    channels = _channel_rows(epg)
    if channel_idx >= len(channels):
        return 2, 0
    cid = channels[channel_idx][0]
    ch_name = channels[channel_idx][1]["name"]
    plist = epg["programmes"].get(cid, [])
    if not plist:
        stdscr.addstr(0, 0, "No programmes")
        stdscr.getch()
        return 2, channel_idx

    # find current index
    now = dt.datetime.now()
    current = 0
    for i, prog in enumerate(plist):
        if prog["start"] <= now < prog["stop"]:
            current = i
            break
        if prog["start"] > now:
            current = max(0, i - 1)
            break

    selected = current
    top = max(0, current - 1)

    while True:
        height, width = stdscr.getmaxyx()
        date_w = 5
        time_w = 5
        title_w = max(1, width - date_w - time_w - 3)

        rows = []
        for prog in plist:
            date = prog["start"].strftime("%d:%m")
            time = prog["start"].strftime("%H:%M")
            title = prog["title"]
            rows.append(f"{date} {time} {title[:title_w]}")

        stdscr.erase()
        for idx in range(height):
            row_idx = top + idx
            if row_idx >= len(rows):
                break
            attr = curses.A_REVERSE if row_idx == selected else curses.A_NORMAL
            prog = plist[row_idx]
            if prog["stop"] <= now:
                attr |= curses.color_pair(1)
            stdscr.addnstr(idx, 0, rows[row_idx], width - 1, attr)

        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            if selected > 0:
                selected -= 1
            if selected < top:
                top = selected
        elif key in (curses.KEY_DOWN, ord("j")):
            if selected < len(rows) - 1:
                selected += 1
            if selected >= top + height:
                top += 1
        elif key == curses.KEY_NPAGE:
            step = height
            selected = min(len(rows) - 1, selected + step)
            top = min(len(rows) - step, top + step)
        elif key == curses.KEY_PPAGE:
            step = height
            selected = max(0, selected - step)
            top = max(0, top - step)
        elif key == curses.KEY_HOME:
            selected, top = 0, 0
        elif key == curses.KEY_END:
            selected = len(rows) - 1
            top = max(0, len(rows) - height)
        elif key in (curses.KEY_LEFT, 27):
            return 2, channel_idx
        elif key in (curses.KEY_RIGHT, ord("\n")):
            pass  # no action


# ---------------------------------------------------------------------------
# Application driver
# ---------------------------------------------------------------------------


def run(stdscr: "curses._CursesWindow") -> None:
    if not sys.stdout.isatty():  # not an interactive terminal
        return

    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.init_pair(1, curses.COLOR_YELLOW, curses.COLOR_BLACK)

    urls = read_config()
    providers = list(urls.items())
    if not providers:
        stdscr.addstr(0, 0, "No providers configured")
        stdscr.refresh()
        stdscr.getch()
        return

    state = 1
    selected = 0
    channel_idx = 0

    while True:
        if state == 1:  # provider list
            state, selected = screen1(stdscr, providers, selected)
            if state == 0:
                break
        elif state == 2:  # channel list
            prov_no, url = providers[selected]
            epg = get_epg(prov_no, url)
            state, channel_idx = screen2(stdscr, epg, channel_idx)
        elif state == 3:  # programmes
            prov_no, url = providers[selected]
            epg = get_epg(prov_no, url)
            state, channel_idx = screen3(stdscr, epg, channel_idx)


def main() -> None:
    try:
        curses.wrapper(run)
    except Exception as exc:  # pragma: no cover - for robustness
        LOGGER.error("Unhandled exception: %s", exc)


if __name__ == "__main__":
    main()

