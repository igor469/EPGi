#!/usr/bin/env python3
"""Simple EPG interactive viewer.

This program reads configuration from EPGi.ini, downloads an EPG
XML file (optionally gzipped), parses it and presents an
interactive interface for browsing currently running TV shows.
"""

import configparser
import curses
import datetime as dt
import gzip
import io
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

try:
    import requests
except ImportError:  # pragma: no cover - requests may not be installed
    requests = None

CONFIG_FILE = Path(__file__).with_name("EPGi.ini")
LOG_FILE = Path(__file__).with_name("EPGi.log")


def setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%Y-%m-%d %H:%M:%S: %(message)s",
    )
    logging.info("Program started")


def load_config() -> Dict[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    params = {}
    if cfg.has_section("Main"):
        params.update(cfg["Main"])
    return params


def download_epg(url: str) -> bytes:
    if requests is None:
        raise RuntimeError("requests module is required to download EPG")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    content = r.content
    if url.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
            return gz.read()
    return content


def parse_epg(xml_bytes: bytes) -> Dict[str, List[Dict[str, str]]]:
    tree = ET.fromstring(xml_bytes)
    programmes: Dict[str, List[Dict[str, str]]] = {}
    for programme in tree.findall("programme"):
        channel = programme.get("channel")
        start = programme.get("start")
        stop = programme.get("stop")
        title_el = programme.find("title")
        title = title_el.text if title_el is not None else ""
        programmes.setdefault(channel, []).append(
            {"start": start, "stop": stop, "title": title}
        )
    for channel in programmes:
        programmes[channel].sort(key=lambda x: x["start"])
    return programmes


def current_show(programmes: List[Dict[str, str]], now: dt.datetime) -> Dict[str, str]:
    for prog in programmes:
        start = parse_time(prog["start"])
        stop = parse_time(prog["stop"])
        if start <= now < stop:
            return prog
    return {}


def parse_time(t: str) -> dt.datetime:
    # EPG time format example: 20250727T120000 +0000 or without space
    digits = ''.join(ch for ch in t if ch.isdigit())
    return dt.datetime.strptime(digits, "%Y%m%d%H%M%S")


def draw_main(stdscr, channels, programmes, page_size):
    curses.curs_set(0)
    idx = 0
    page = 0
    while True:
        stdscr.clear()
        start = page * page_size
        end = start + page_size
        now = dt.datetime.now()
        for i, ch in enumerate(channels[start:end], start=start):
            prog = current_show(programmes.get(ch, []), now)
            title = prog.get("title", "")
            start_time = parse_time(prog.get("start", now.strftime("%Y%m%d%H%M%S")))
            stop_time = parse_time(prog.get("stop", now.strftime("%Y%m%d%H%M%S")))
            dur = (stop_time - start_time).total_seconds() or 1
            elapsed = (now - start_time).total_seconds()
            pct = min(max(int(elapsed / dur * 100), 0), 100)
            bar_len = 20
            fill = int(bar_len * pct / 100)
            bar = "[" + "#" * fill + "-" * (bar_len - fill) + "]"
            marker = "->" if i == idx else "  "
            stdscr.addstr(i - start, 0, f"{marker} {ch:20} {title:40} {bar} {pct:3d}%")
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')):
            if idx > 0:
                idx -= 1
                if idx < start:
                    page -= 1
        elif key in (curses.KEY_DOWN, ord('j')):
            if idx < len(channels) - 1:
                idx += 1
                if idx >= end:
                    page += 1
        elif key in (curses.KEY_NPAGE,):  # Page Down
            if end < len(channels):
                page += 1
                idx = page * page_size
        elif key in (curses.KEY_PPAGE,):  # Page Up
            if start > 0:
                page -= 1
                idx = page * page_size
        elif key in (ord('\n'), curses.KEY_ENTER):
            draw_channel(stdscr, channels[idx], programmes.get(channels[idx], []))
        elif key in (27, ord('q')):  # Esc or q
            break


def draw_channel(stdscr, channel, progs):
    idx = 0
    while True:
        stdscr.clear()
        stdscr.addstr(0, 0, f"Channel: {channel}")
        for i, prog in enumerate(progs):
            start = parse_time(prog["start"]).strftime("%Y-%m-%d %H:%M")
            stop = parse_time(prog["stop"]).strftime("%H:%M")
            marker = "->" if i == idx else "  "
            stdscr.addstr(i + 1, 0, f"{marker} {start} - {stop} {prog['title']}")
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')):
            if idx > 0:
                idx -= 1
        elif key in (curses.KEY_DOWN, ord('j')):
            if idx < len(progs) - 1:
                idx += 1
        elif key in (27, ord('q')):
            break


def main(stdscr):
    params = load_config()
    url = params.get("url", "")
    page_size = int(params.get("n", params.get("page", 20)))
    try:
        xml_bytes = download_epg(url)
        programmes = parse_epg(xml_bytes)
    except Exception as e:  # pragma: no cover - network/parse errors
        logging.exception("Failed to load EPG: %s", e)
        stdscr.addstr(0, 0, f"Error: {e}")
        stdscr.getch()
        return
    channels = sorted(programmes.keys())
    draw_main(stdscr, channels, programmes, page_size)


if __name__ == "__main__":
    setup_logging()
    curses.wrapper(main)

