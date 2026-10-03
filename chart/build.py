#!/usr/bin/python3
"""Build the "NEC edition by state" PDF from chart/nec-by-state.json.

    /usr/bin/python3 chart/build.py            # JSON -> HTML -> PDF
    /usr/bin/python3 chart/build.py --no-pdf   # JSON -> HTML only

Python standard library only. The PDF is printed by Brave in headless mode (through its DevTools port).
Every state fact comes from the JSON file; nothing about a state is typed here.
The JSON holds only the fields printed in the PDF (plus code); the full review data
stays outside this public repo.
"""
import argparse
import base64
import html
import json
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "nec-by-state.json"
HTML_OUT = HERE / "nec-edition-by-state.html"
PDF_OUT = HERE / "nec-edition-by-state.pdf"
BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
SITE = "https://suleymanbdn.github.io/wirenut-site/"

# status -> (label in the table, what it means, dot colour)
CONFIDENCE = {
    "confirmed": (
        "State source",
        "We read the state’s own rule or an official public-agency page.",
        "#2f7d4f",
    ),
    "official-unopened": (
        "State source (indirect)",
        "We found the state’s official page but could only read it through a search summary "
        "— likely right, not yet confirmed.",
        "#2b6f8a",
    ),
    "two-roundups-agree": (
        "Two roundups agree",
        "Two commercial adoption lists agreed when we checked; not yet confirmed against the "
        "state’s own rule. The link goes to the list the date came from.",
        "#c98322",
    ),
    "unresolved": (
        "Unresolved",
        "We could not pin it down. We never guess — ask your AHJ.",
        "#b23b3b",
    ),
}
REQUIRED = (
    "state", "edition_in_effect", "effective_date", "statewide",
    "adopted_next", "source_urls", "verified_on", "status",
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")
NOT_CONFIRMED = "Not confirmed — check with your AHJ"

esc = html.escape


def fail(message):
    sys.exit("build.py: " + message)


# ---------------------------------------------------------------- data

def load(path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        fail(path.name + " must be a non-empty list of state rows")
    for r in rows:
        missing = [k for k in REQUIRED if k not in r]
        if missing:
            fail("%s is missing %s" % (r.get("state", "a row"), ", ".join(missing)))
        if r["status"] not in CONFIDENCE:
            fail("%s has an unknown status %r" % (r["state"], r["status"]))
        if not r["source_urls"]:
            fail("%s has no source URL" % r["state"])
        for u in r["source_urls"]:
            if urlparse(u).scheme not in ("http", "https"):
                fail("%s has a non-http source URL: %s" % (r["state"], u))
        if r["adopted_next"] is not None and not r["adopted_next"].get("edition"):
            fail("%s has adopted_next without an edition" % r["state"])
    return sorted(rows, key=lambda r: r["state"])


def parse_date(iso):
    """'2026-12-31' -> (2026, 12, 31). Anything else is a hard error."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not m:
        fail("unexpected date format: %r" % (iso,))
    y, mo, d = (int(x) for x in m.groups())
    if not 1 <= mo <= 12:
        fail("unexpected date: %r" % (iso,))
    return y, mo, d


def short_date(iso):
    if not iso:
        return "—"
    y, mo, d = parse_date(iso)
    return "%s %d, %d" % (MONTHS[mo - 1], d, y)


def long_date(iso):
    y, mo, d = parse_date(iso)
    return "%s %d, %d" % (MONTHS_LONG[mo - 1], d, y)


def classify(r):
    """'unconfirmed' | 'local' | 'edition' -- decides what the Edition cell says."""
    if r["status"] == "unresolved":
        return "unconfirmed"
    if r["statewide"] is False:
        return "local"
    if not r["edition_in_effect"]:
        return "unconfirmed"
    return "edition"


def edition_year(label):
    m = re.search(r"\d{4}", label)
    return int(m.group()) if m else 0


# ---------------------------------------------------------------- summary

def summary(rows):
    """List of (count, caption). Sums to len(rows)."""
    by_edition = {}
    local = unconfirmed = 0
    for r in rows:
        kind = classify(r)
        if kind == "local":
            local += 1
        elif kind == "unconfirmed":
            unconfirmed += 1
        else:
            by_edition[r["edition_in_effect"]] = by_edition.get(r["edition_in_effect"], 0) + 1
    editions = sorted(by_edition, key=edition_year, reverse=True)
    tiles = [(by_edition[e], "on " + e) for e in editions[:3]]
    older = editions[3:]
    if older:
        tiles.append((sum(by_edition[e] for e in older),
                      "on %s or older" % older[0]))
    tiles.append((local, "local adoption"))
    tiles.append((unconfirmed, "not confirmed"))
    if sum(n for n, _ in tiles) != len(rows):
        fail("summary does not add up to the number of rows")
    return tiles


def has_next(r):
    """A newer edition counts only when it is adopted or approved, never for unresolved rows."""
    return bool(r["adopted_next"]) and r["status"] != "unresolved"


def changing_soon(rows):
    items = [r for r in rows if has_next(r)]

    def key(r):
        d = r["adopted_next"].get("effective_date")
        return (d is None, d or "", r["state"])
    return sorted(items, key=key)


# ---------------------------------------------------------------- html parts

def domain(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def source_cell(r):
    seen, links = set(), []
    for u in r["source_urls"]:
        d = domain(u)
        if d in seen:
            continue
        seen.add(d)
        links.append('<a href="%s">%s</a>' % (esc(u, quote=True), esc(d)))
    return " ".join(links)


def next_text(r):
    n = r["adopted_next"]
    when = short_date(n.get("effective_date")) if n.get("effective_date") else "date not set"
    return "Next: %s · %s" % (n["edition"], when)


def label_html(label, colour):
    """Dot + label kept on one line; a trailing '(...)' may drop to the next line."""
    head, sep, tail = label.partition(" (")
    return ('<span class="lb"><span class="dot" style="background:%s"></span>%s</span>%s'
            % (colour, esc(head), " (" + esc(tail) if sep else ""))


def row_html(r):
    kind = classify(r)
    label, _, colour = CONFIDENCE[r["status"]]
    subs = []
    if kind == "unconfirmed":
        main = '<td class="ed muted" colspan="2">%s' % esc(NOT_CONFIRMED)
        since = ""
    elif kind == "local":
        main = '<td class="ed muted">Local adoption'
        since = '<td class="dt">—</td>'
    else:
        main = '<td class="ed"><span class="mono">%s</span>' % esc(r["edition_in_effect"])
        since = '<td class="dt mono">%s</td>' % esc(short_date(r["effective_date"]))
        if r["statewide"] is None:
            subs.append("statewide status disputed")
    if has_next(r):
        subs.append(next_text(r))
    sub_html = "".join('<div class="sub">%s</div>' % esc(s) for s in subs)
    out = [
        '<tbody class="s"><tr>',
        '<th scope="row" class="st">%s</th>' % esc(r["state"]),
        main + sub_html + "</td>",
        since,
        '<td class="cf">%s<div class="sub">checked %s</div></td>'
        % (label_html(label, colour), esc(short_date(r["verified_on"]))),
        '<td class="src">%s</td></tr>' % source_cell(r),
    ]
    if r.get("pdf_note"):
        out.append('<tr class="pn"><td colspan="5">%s</td></tr>' % esc(r["pdf_note"]))
    out.append("</tbody>")
    return "".join(out)


def soon_html(rows):
    items = changing_soon(rows)
    if not items:
        return ""
    lines = []
    for r in items:
        n = r["adopted_next"]
        kind = classify(r)
        now = {"unconfirmed": "not confirmed", "local": "local adoption"}.get(
            kind, r["edition_in_effect"])
        when = short_date(n["effective_date"]) if n.get("effective_date") else "date not set"
        lines.append(
            '<tr><th scope="row">%s</th><td class="mono">%s</td>'
            '<td class="mono">%s</td><td class="mono">%s</td></tr>'
            % (esc(r["state"]), esc(now), esc(n["edition"]), esc(when)))
    return (
        '<section class="soon"><h2>Changing soon</h2>'
        "<p>Newer editions adopted or approved but not in force yet.</p>"
        '<table><thead><tr><th>State</th><th>In effect now</th><th>Coming</th>'
        "<th>Expected</th></tr></thead><tbody>%s</tbody></table></section>"
        % "".join(lines))


def howto_html(rows):
    used = [s for s in CONFIDENCE if any(r["status"] == s for r in rows)]
    defs = "".join(
        '<dt><span class="dot" style="background:%s"></span>%s</dt><dd>%s</dd>'
        % (CONFIDENCE[s][2], esc(CONFIDENCE[s][0]), esc(CONFIDENCE[s][1])) for s in used)
    if any(classify(r) == "local" for r in rows):
        defs += ("<dt>Local adoption</dt><dd>No single statewide edition. Cities or "
                 "counties choose, so ask the local inspector.</dd>")
    if any(classify(r) == "edition" and r["statewide"] is None for r in rows):
        defs += ("<dt>Statewide status disputed</dt><dd>The sources agree on the edition "
                 "but disagree on whether it applies statewide.</dd>")
    return (
        '<section class="how"><h2>How to read this</h2><dl>%s</dl>'
        "<p>States amend the NEC, and cities and counties can adopt differently. Your AHJ "
        "(authority having jurisdiction) has the final say. Not legal advice.</p></section>"
        % defs)


def confidence_line(rows):
    parts = []
    for status, (label, _, colour) in CONFIDENCE.items():
        n = sum(1 for r in rows if r["status"] == status)
        if n:
            parts.append('<span class="dot" style="background:%s"></span>'
                         "<b>%d</b> %s" % (colour, n, esc(label)))
    return " ".join('<span class="cfi">%s</span>' % p for p in parts)


def scope_text(rows):
    names = {r["state"] for r in rows}
    if len(rows) == 51 and "District of Columbia" in names:
        return "50 states and D.C."
    return "%d jurisdictions" % len(rows)


CSS = """
@font-face{font-family:"Space Grotesk";font-style:normal;font-weight:500 700;src:url(../fonts/space-grotesk-latin.woff2) format("woff2")}
@font-face{font-family:"Inter";font-style:normal;font-weight:400 500;src:url(../fonts/inter-latin.woff2) format("woff2")}
@font-face{font-family:"JetBrains Mono";font-style:normal;font-weight:400 500;src:url(../fonts/jetbrains-mono-latin.woff2) format("woff2")}
@page{size:Letter;margin:.45in .5in .6in;
  @bottom-left{content:"Wirenut \u00b7 NEC edition by state";font:400 9pt Inter,sans-serif;color:#5a6368}
  @bottom-right{content:"Page " counter(page) " of " counter(pages);font:400 9pt Inter,sans-serif;color:#5a6368}}
:root{--ink:#14181a;--dim:#5a6368;--line:#d9dde0;--band:#f5f6f7;--amber:#e89b2c;--amber-ink:#8a4f00;--tint:#fff6e5}
*{box-sizing:border-box}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{margin:0;background:#fff;color:var(--ink);font:400 10pt/1.45 Inter,sans-serif}
a{color:var(--amber-ink);text-decoration:underline;text-decoration-thickness:.5pt;text-underline-offset:1.5pt}
.mono{font-family:"JetBrains Mono",monospace;font-variant-numeric:tabular-nums}
.top{display:flex;justify-content:space-between;align-items:baseline;border-top:3pt solid var(--amber);padding-top:7pt}
.brand{font:700 13pt "Space Grotesk",sans-serif;letter-spacing:-.01em}
.url{font:400 9.5pt "JetBrains Mono",monospace}
h1{font:700 31pt/1.05 "Space Grotesk",sans-serif;letter-spacing:-.025em;margin:12pt 0 5pt}
.lede{margin:0;font-size:11.5pt;color:var(--dim)}
.tiles{display:flex;gap:7pt;margin:12pt 0 6pt}
.tile{flex:1;border:.75pt solid var(--line);border-radius:4pt;padding:7pt 8pt 6pt}
.tile b{display:block;font:700 22pt/1 "Space Grotesk",sans-serif;letter-spacing:-.02em}
.tile span{display:block;margin-top:3pt;font-size:9pt;line-height:1.25;color:var(--dim)}
.cfl{margin:0 0 11pt;font-size:9.5pt;color:var(--dim)}
.cfi{display:inline-block;margin-right:12pt;white-space:nowrap}
.cfi b{font-weight:500;color:var(--ink)}
.dot{display:inline-block;width:6pt;height:6pt;border-radius:50%;margin-right:5pt;vertical-align:.5pt}
h2{font:700 14pt/1.2 "Space Grotesk",sans-serif;letter-spacing:-.015em;margin:0 0 3pt}
.soon{background:var(--tint);border-left:3pt solid var(--amber);border-radius:0 4pt 4pt 0;padding:8pt 12pt 7pt;margin:0 0 12pt;break-inside:avoid}
.soon p{margin:0 0 6pt;font-size:10pt;color:var(--dim)}
.soon table{width:100%;border-collapse:collapse;font-size:10pt}
.soon th,.soon td{text-align:left;padding:2.5pt 8pt 2.5pt 0;font-weight:400}
.soon thead th{font:500 9pt "JetBrains Mono",monospace;text-transform:uppercase;letter-spacing:.06em;color:var(--amber-ink);border-bottom:.75pt solid #e3c88f}
.soon tbody th{font-weight:500}
.soon tbody tr+tr>*{border-top:.5pt solid #efdcb4}
.listh{margin:0 0 6pt}
table.main{width:100%;border-collapse:collapse;table-layout:fixed;font-size:10pt;line-height:1.28}
table.main thead{display:table-header-group}
table.main thead th{font:500 9pt "JetBrains Mono",monospace;text-transform:uppercase;letter-spacing:.02em;color:var(--dim);text-align:left;padding:0 6pt 5pt 0;border-bottom:1.25pt solid var(--ink)}
table.main td,table.main th.st{padding:2.2pt 4pt 2.2pt 0;vertical-align:top;text-align:left;border-bottom:.5pt solid var(--line)}
table.main tbody.s{break-inside:avoid}
table.main tbody.s:nth-of-type(even)>tr>*{background:var(--band)}
table.main tbody.s>tr>*:first-child{padding-left:3pt}
.st{font-weight:500;font-size:9.5pt}
.ed{font-size:10pt}
.ed.muted,.muted{color:var(--dim)}
.sub{font-size:9pt;color:var(--dim);line-height:1.25}
.dt{font-size:9pt;white-space:nowrap}
.cf{font-size:9pt}
.lb{white-space:nowrap}
.cf .sub{padding-left:11pt;white-space:nowrap}
table.main td.cf{padding-left:3pt}
.src{font-size:9pt;overflow-wrap:anywhere}
.src a{margin-right:7pt;white-space:nowrap}
tr.pn td{font-size:9pt;color:var(--dim);padding:0 6pt 4pt 3pt;border-bottom:.5pt solid var(--line);font-style:normal}
tbody.s>tr:has(+tr.pn)>*{border-bottom:0}
.how{margin-top:10pt;break-inside:avoid;border-top:1.25pt solid var(--ink);padding-top:8pt}
.how h2{margin-bottom:5pt}
.how dl{margin:0 0 6pt;display:grid;grid-template-columns:125pt 1fr;gap:3pt 10pt}
.how dt{font-weight:500;font-size:10pt}
.how dd{margin:0;font-size:10pt;color:var(--dim)}
.how p{margin:0 0 4pt;font-size:10pt}
.about{margin-top:8pt;padding-top:6pt;border-top:.5pt solid var(--line);font-size:9.5pt;color:var(--dim);break-inside:avoid}
.about p{margin:0 0 4pt}
.about .tm{font-size:9pt}
"""


def build_html(rows):
    as_of = long_date(max(r["verified_on"] for r in rows))
    tiles = "".join('<div class="tile"><b>%d</b><span>%s</span></div>' % (n, esc(c))
                    for n, c in summary(rows))
    body = "".join(row_html(r) for r in rows)
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex">
<title>NEC edition by state — Wirenut</title>
<style>%(css)s</style></head><body>
<header><div class="top"><span class="brand">Wirenut</span><a class="url" href="%(site)s">%(site)s</a></div>
<h1>NEC edition by state</h1>
<p class="lede">Which National Electrical Code edition each state enforces. Updated %(as_of)s — each row shows when we last checked it.</p></header>
<div class="tiles">%(tiles)s</div>
<p class="cfl">%(scope)s \u00b7 %(conf)s</p>
%(soon)s
<table class="main"><colgroup><col style="width:15%%"><col style="width:25.5%%"><col style="width:13%%"><col style="width:20.5%%"><col style="width:26%%"></colgroup>
<thead><tr><th>State</th><th>Edition in effect</th><th>Since</th><th>Source confidence</th><th>Source</th></tr></thead>
%(body)s</table>
%(how)s
<footer class="about"><p>Wirenut is a plain-language index to the National Electrical Code. It’s an index, not the code — your code book has the final word. <a href="%(site)s">%(site)s</a></p>
<p class="tm">NEC® and National Electrical Code® are registered trademarks of the National Fire Protection Association. Wirenut is an independent reference tool and is not affiliated with, endorsed by, or sponsored by the NFPA. It does not reproduce code text.</p></footer>
</body></html>
""" % {
        "css": CSS, "site": SITE, "as_of": esc(as_of), "tiles": tiles,
        "scope": scope_text(rows), "conf": confidence_line(rows),
        "soon": soon_html(rows), "body": body, "how": howto_html(rows),
    }


# ---------------------------------------------------------------- pdf
#
# Brave's one-shot "--print-to-pdf" flag hangs on this Mac (even for about:blank),
# so the page is printed through the DevTools protocol instead: start Brave
# headless, open the HTML, ask for Page.printToPDF. Same engine, same result.

class DevTools:
    """Just enough of a WebSocket client (RFC 6455) to talk to Brave's DevTools."""

    def __init__(self, port, path):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, port, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                fail("DevTools closed the connection during the handshake")
            buf += chunk
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            fail("DevTools refused the WebSocket: " + head[:80].decode("latin-1"))
        self.last_id = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                fail("DevTools connection dropped")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def _message(self):
        message = b""
        while True:
            b1, b2 = self._read(2)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)  # frames from the server are not masked
            opcode = b1 & 0x0F
            if opcode == 0x8:
                fail("DevTools closed the connection")
            if opcode == 0x9:  # ping -> pong
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if b1 & 0x80:
                return message

    def call(self, method, **params):
        self.last_id += 1
        self._send(0x1, json.dumps(
            {"id": self.last_id, "method": method, "params": params}).encode())
        while True:
            msg = json.loads(self._message())
            if msg.get("id") == self.last_id:
                if "error" in msg:
                    fail("DevTools %s failed: %s" % (method, msg["error"]))
                return msg["result"]  # anything else is an event we do not need

    def close(self):
        self.sock.close()


def wait_for(path, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.1)
    fail("Brave did not start (no %s after %ds)" % (path.name, seconds))


def print_pdf(html_path, pdf_path):
    if not pathlib.Path(BRAVE).exists():
        fail("Brave not found at " + BRAVE)
    # A throwaway profile keeps this run apart from any Brave window that is open.
    profile = pathlib.Path(tempfile.mkdtemp(prefix="wirenut-pdf-"))
    proc = subprocess.Popen(
        [BRAVE, "--headless", "--disable-gpu", "--no-first-run", "--disable-extensions",
         "--disable-sync", "--disable-background-networking", "--disable-component-update",
         "--user-data-dir=" + str(profile), "--remote-debugging-port=0", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tools = None
    try:
        wait_for(profile / "DevToolsActivePort", 30)
        port = int((profile / "DevToolsActivePort").read_text().split()[0])
        pages = []
        for _ in range(50):  # the first page target can lag the port file
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=10) as r:
                pages = [t for t in json.load(r) if t.get("type") == "page"]
            if pages:
                break
            time.sleep(0.1)
        if not pages:
            fail("Brave opened no page")
        tools = DevTools(port, urlparse(pages[0]["webSocketDebuggerUrl"]).path)
        tools.call("Page.enable")
        tools.call("Page.navigate", url=html_path.as_uri())
        ready = "(async()=>{await document.fonts.ready;return location.protocol+'|'+document.readyState+'|'+" \
                "[...document.fonts].filter(f=>f.status==='error').map(f=>f.family).join(',')})()"
        for _ in range(100):
            value = tools.call("Runtime.evaluate", expression=ready, awaitPromise=True,
                               returnByValue=True)["result"].get("value", "")
            protocol, _, rest = value.partition("|")
            state, _, bad = rest.partition("|")
            if protocol == "file:" and state == "complete":
                break
            time.sleep(0.1)
        else:
            fail("the page did not finish loading")
        if bad:
            fail("fonts failed to load: " + bad)
        result = tools.call("Page.printToPDF", preferCSSPageSize=True, printBackground=True,
                            displayHeaderFooter=False, transferMode="ReturnAsBase64")
        pdf_path.write_bytes(base64.b64decode(result["data"]))
        try:
            tools.call("Browser.close")
        except SystemExit:
            pass  # the browser may drop the socket while closing
    finally:
        if tools:
            tools.close()
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        fail("no PDF was written")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-pdf", action="store_true", help="write the HTML only")
    args = ap.parse_args()
    rows = load(DATA)
    HTML_OUT.write_text(build_html(rows), encoding="utf-8")
    print("wrote %s (%d states)" % (HTML_OUT.relative_to(HERE.parent), len(rows)))
    if not args.no_pdf:
        print_pdf(HTML_OUT, PDF_OUT)
        print("wrote %s (%d KB)" % (PDF_OUT.relative_to(HERE.parent), PDF_OUT.stat().st_size // 1024))


if __name__ == "__main__":
    main()
