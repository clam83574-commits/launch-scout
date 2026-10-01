"""
Брошюра A5 (две стороны) под мероприятие: PDF для печати + PNG-превью.
  python make_brochure.py digitalbridge
Логотип партнёра: положите partner_logo.svg или partner_logo.png рядом — он встанет
в шапку; иначе там будет аккуратная надпись с названием мероприятия.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request

import segno

CODE = (sys.argv[1] if len(sys.argv) > 1 else "digitalbridge").lower()
PARTNER = {"digitalbridge": "Digital Bridge"}.get(CODE, CODE)
HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "https://launch-scout-site.pages.dev/?promo=" + CODE
OFF = 0.3
PLANS = [("Pro", 4.99, 1500, True), ("Max", 9.99, 3500, False), ("Pro Max", 19.99, 7000, False)]


def qr_svg(url):
    """QR уровня H с логотипом в центре; квадратные «глазки» — их читает любой сканер."""
    m = [[bool(v) for v in row] for row in segno.make(url, error="h", boost_error=False).matrix]
    n = len(m)
    finder = lambda r, c: any(r0 <= r < r0 + 7 and c0 <= c < c0 + 7 for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)))
    hole = int(n * 0.22) | 1
    h0 = (n - hole) // 2
    dots = "".join(f'<rect x="{c + .03:.2f}" y="{r + .03:.2f}" width=".94" height=".94" rx=".25"/>'
                   for r in range(n) for c in range(n)
                   if m[r][c] and not finder(r, c) and not (h0 - 1 <= r <= h0 + hole and h0 - 1 <= c <= h0 + hole))
    eyes = "".join(f'<rect x="{c0 + .5}" y="{r0 + .5}" width="6" height="6" fill="none" stroke="#0f1d3a" stroke-width="1"/>'
                   f'<rect x="{c0 + 2}" y="{r0 + 2}" width="3" height="3" fill="#1d4fd8"/>' for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)))
    cx = n / 2
    logo = (f'<rect x="{h0 - .4}" y="{h0 - .4}" width="{hole + .8}" height="{hole + .8}" rx="2.2" fill="#fff"/>'
            f'<g transform="translate({cx - hole * .36},{cx - hole * .36}) scale({hole * .72 / 32})">'
            '<circle cx="16" cy="16" r="14" fill="none" stroke="#0f1d3a" stroke-width="2"/>'
            '<path d="M16 16 L27 8.5" stroke="#0f1d3a" stroke-width="2.2" stroke-linecap="round"/>'
            '<circle cx="22.6" cy="11.4" r="2.6" fill="#1d4fd8"/></g>')
    return (f'<svg viewBox="-2 -2 {n + 4} {n + 4}" xmlns="http://www.w3.org/2000/svg"><rect x="-2" y="-2" width="{n + 4}" height="{n + 4}" fill="#fff"/>'
            f'<g fill="#0f1d3a">{dots}</g>{eyes}{logo}</svg>')


def partner_html():
    for name, mime in (("partner_logo.svg", "image/svg+xml"), ("partner_logo.png", "image/png")):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            return f'<img alt="{PARTNER}" src="data:{mime};base64,{base64.b64encode(open(p, "rb").read()).decode()}">'
    return f'<span class="ph">{PARTNER}</span>'


def plans_html():
    cut = lambda v: f"{int(v * (1 - OFF) * 100 + 1e-6) / 100:.2f}"
    out = []
    for name, usd, ls, main in PLANS:
        out.append(f'<div class="plan{" main" if main else ""}"><span class="n">{name}</span><span class="p">${cut(usd)}</span>'
                   f'<s>${usd:.2f}</s><span class="l">{ls:,} LS / мес</span></div>'.replace(",", " "))
    out.append(f'<div class="plan"><span class="n">Free</span><span class="p">$0</span><s style="visibility:hidden">$0.00</s><span class="l">300 LS / мес</span></div>')
    return "".join(out)


pulse = urllib.request.urlopen(urllib.request.Request("https://launch-scout-bot.clam83574.workers.dev/public/pulse",
                                                      headers={"User-Agent": "curl/8"}), timeout=30).read().decode()
html = open(os.path.join(HERE, "brochure.html"), encoding="utf-8").read()
html = (html.replace("__QR__", qr_svg(URL)).replace("__PARTNER__", partner_html()).replace("__PLANS__", plans_html())
        .replace("__CODE__", CODE.upper()).replace("__PULSE__", pulse))
tmp = os.path.join(HERE, f"_brochure_{CODE}.html")
open(tmp, "w", encoding="utf-8").write(html)
url = "file:///" + tmp.replace("\\", "/")
pdf = os.path.join(HERE, f"brochure_{CODE}.pdf")
png = os.path.join(HERE, f"brochure_{CODE}_preview.png")
common = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=8000"]
subprocess.run(common + ["--no-pdf-header-footer", "--print-to-pdf=" + pdf, url], check=True, capture_output=True)
subprocess.run(common + ["--force-device-scale-factor=2", "--window-size=600,1650", "--screenshot=" + png, url], check=True, capture_output=True)
os.remove(tmp)
print(json.dumps({"pdf": pdf, "png": png, "url": URL}))
