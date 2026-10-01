"""
QR-экран для показа с телефона на мероприятии: 1080×1920, стиль сайта.
Ссылка ведёт на сайт с промокодом — там плашка и кнопки в бот уже с кодом.
Запуск: python make_qr.py digitalbridge  → qr_digitalbridge.png
"""
import json
import os
import subprocess
import sys

import segno

CODE = (sys.argv[1] if len(sys.argv) > 1 else "digitalbridge").lower()
URL = "https://launch-scout-site.pages.dev/?promo=" + CODE
HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Уровень H — 30% запаса: в центр можно положить логотип, код всё равно читается.
qr = segno.make(URL, error="h", boost_error=False)
m = [[bool(v) for v in row] for row in qr.matrix]
n = len(m)


def in_finder(r, c):
    return any(r0 <= r < r0 + 7 and c0 <= c < c0 + 7 for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)))


# центр под логотип: пустое окно ~ 22% стороны (в пределах запаса уровня H)
DOT = float(os.environ.get("QR_DOT", "0.94")); RX = float(os.environ.get("QR_RX", "0.25")); EYE_RX = float(os.environ.get("QR_EYE", "0")); HOLE = float(os.environ.get("QR_HOLE", "0.22"))
hole = int(n * HOLE) | 1
h0 = (n - hole) // 2

parts = []
for r in range(n):
    for c in range(n):
        if not m[r][c] or in_finder(r, c):
            continue
        if h0 - 1 <= r <= h0 + hole and h0 - 1 <= c <= h0 + hole:
            continue
        off = (1 - DOT) / 2
        parts.append(f'<rect x="{c + off:.2f}" y="{r + off:.2f}" width="{DOT}" height="{DOT}" rx="{RX}"/>')
finders = []
for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
    finders.append(f'<rect x="{c0 + .5}" y="{r0 + .5}" width="6" height="6" rx="{EYE_RX}" fill="none" stroke="#0f1d3a" stroke-width="1"/>'
                   f'<rect x="{c0 + 2}" y="{r0 + 2}" width="3" height="3" rx="{EYE_RX / 2}" fill="#1d4fd8"/>')
cx = n / 2
logo = (f'<rect x="{h0 - .4}" y="{h0 - .4}" width="{hole + .8}" height="{hole + .8}" rx="2.2" fill="#fff"/>'
        f'<g transform="translate({cx - hole * .36},{cx - hole * .36}) scale({hole * .72 / 32})">'
        '<circle cx="16" cy="16" r="14" fill="none" stroke="#0f1d3a" stroke-width="2"/>'
        '<circle cx="16" cy="16" r="8.5" fill="none" stroke="#0f1d3a" stroke-width="1.4" opacity=".5"/>'
        '<path d="M16 16 L27 8.5" stroke="#0f1d3a" stroke-width="2.2" stroke-linecap="round"/>'
        '<circle cx="22.6" cy="11.4" r="2.6" fill="#1d4fd8"/></g>')
qr_svg = (f'<svg viewBox="-4 -4 {n + 8} {n + 8}" xmlns="http://www.w3.org/2000/svg">'
          f'<rect x="-4" y="-4" width="{n + 8}" height="{n + 8}" fill="#fff"/>'
          f'<g fill="#0f1d3a">{"".join(parts)}</g>{"".join(finders)}{logo}</svg>')

html = open(os.path.join(HERE, "qr_card.html"), encoding="utf-8").read()
html = html.replace("__QR__", qr_svg).replace("__CODE__", CODE.upper())
tmp = os.path.join(HERE, "_card.html")
open(tmp, "w", encoding="utf-8").write(html)
out = os.path.join(HERE, f"qr_{CODE}.png")
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                "--virtual-time-budget=6000", "--window-size=1080,1920", "--screenshot=" + out, "file:///" + tmp.replace("\\", "/")],
               check=True, capture_output=True)
# чистый QR отдельно — для печати/слайда
qr.save(os.path.join(HERE, f"qr_{CODE}_plain.png"), scale=20, border=4, dark="#0f1d3a")
print(json.dumps({"url": URL, "modules": n, "card": out}))
