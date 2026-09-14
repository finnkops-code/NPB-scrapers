"""
NPB Scraper — standen (standings) van de Central League en de Pacific League.
Bron: https://npb.jp/bis/eng/{seizoen}/stats/std_c.html (Central League)
      https://npb.jp/bis/eng/{seizoen}/stats/std_p.html (Pacific League)

Heel andere situatie dan de drie LMB-scrapers in dit project: npb.jp heeft
helemaal geen robots.txt (een directe check op /robots.txt geeft een 404),
dus er is hier geen enkele crawl-beperking om rekening mee te houden. En in
tegenstelling tot lmb.com.mx (een Next.js-app die zijn tabellen deels
client-side via een verboden API-endpoint opbouwt) is deze pagina gewoon
kant-en-klare, server-gerenderde HTML: één plain `requests.get()` levert
exact dezelfde tabeldata op als wat een browser laat zien. Er is dus geen
Playwright/headless-browser voor nodig — alleen `requests` + `BeautifulSoup`.

Elke standenpagina bevat twee tabellen (beide met class "tablefix2"):
1) de standen-tabel van de eigen league zelf, met o.a. een "GB"-kolom
   (games behind) en head-to-head-kolommen tegen elk team uit de EIGEN
   league (bv. "vs T", "vs G", ... voor de Central League);
2) de interleague-tabel, zonder "GB"-kolom, met head-to-head-kolommen
   tegen elk team uit de ANDERE league (bv. "vs H", "vs L", ... op de
   Central League-pagina, want dat zijn Pacific League-teams).

We identificeren welke tabel welke is aan de hand van de aanwezigheid van
een "GB"-kolomkop (niet aan de hand van tabelvolgorde), en we lezen de
kolomkoppen zelf uit i.p.v. ze hard te coderen: zo blijft de scraper
werken ongeacht welke teams er in welke league zitten of hoe de site de
head-to-head-kolommen benoemt. De kolomsleutels in de uitvoer-JSON zijn in
het Engels (net als de brontekst zelf, in tegenstelling tot de Spaanse LMB-
bron), bv. "gb", "home", "road", "vs_t", "int".

De jaartal-component in de URL wordt bepaald aan de hand van de datum in
Japan (Asia/Tokyo) — niet UTC — zodat de scraper vanzelf naar het nieuwe
seizoen overschakelt zodra de jaarwisseling in Japan is geweest, ook als de
GitHub Actions-runner (die in UTC draait) dat moment nog niet heeft bereikt.
"""
import json
import re
import time
import datetime as dt
from datetime import timezone

import requests
from bs4 import BeautifulSoup

BASIS_URL = "https://npb.jp/bis/eng/{jaar}/stats/std_{league}.html"
JSON_FILE = "npb_standings.json"
NPB_TZ = "Asia/Tokyo"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# "central" en "pacific" zijn onze eigen, leesbare sleutels in de
# uitvoer-JSON; "c"/"p" zijn de letters die de site zelf in de URL gebruikt.
LEAGUES = [
    ("central", "c"),
    ("pacific", "p"),
]


def haal_pagina_op(url, timeout=20):
    """Haalt de pagina op als platte HTML-tekst. Geen Playwright nodig (zie
    de moduledocstring): dit is gewone, server-gerenderde HTML."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    # requests raadt de encoding soms verkeerd (bv. Latin-1) als de server
    # geen expliciete charset meestuurt; apparent_encoding (chardet-achtige
    # detectie op de echte bytes) is betrouwbaarder voor deze pagina's.
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def slugify(header: str) -> str:
    """Zet een kolomkop uit de site ("vs T", "GB", "Home") om naar een
    nette, stabiele JSON-sleutel ("vs_t", "gb", "home")."""
    return re.sub(r"\s+", "_", header.strip().lower())


def parse_tabel(tabel):
    """Zet één <table> (BeautifulSoup-Tag) om naar een lijst van rij-dicts,
    met de kolomkoppen van de tabel zelf als sleutels (zie slugify())."""
    header_ths = tabel.select("thead th") or tabel.find("tr").find_all("th")
    koppen = [th.get_text(strip=True) for th in header_ths]
    sleutels = [slugify(k) for k in koppen]

    rijen = []
    for tr in tabel.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue  # de header-rij zelf heeft geen <td>'s, alleen <th>'s
        # "<br>" (gebruikt voor een klein rangnummer, bv. "30-29" met
        # daaronder "(1)") wordt met get_text(" ", ...) een spatie i.p.v.
        # dat de twee stukken tekst zonder scheiding aan elkaar plakken.
        waarden = [td.get_text(" ", strip=True) for td in tds]
        rij = dict(zip(sleutels, waarden))

        # G/W/L/T zijn altijd zuivere getallen op deze pagina's; de overige
        # kolommen (PCT, GB, Home/Road-reeksen, head-to-head-records, Int)
        # zijn tekstueel van aard (bv. ".556", "--", "30-29 (1)") en blijven
        # daarom bewust een string.
        for veld in ("g", "w", "l", "t"):
            waarde = rij.get(veld)
            if waarde is not None and waarde.lstrip("-").isdigit():
                rij[veld] = int(waarde)

        rijen.append(rij)
    return rijen


def haal_standen_op(url):
    """Haalt beide tabellen (standen + interleague) van één standenpagina op."""
    html = haal_pagina_op(url)
    soup = BeautifulSoup(html, "html.parser")

    standen, interleague = [], []
    for tabel in soup.select("table.tablefix2"):
        header_ths = tabel.select("thead th") or tabel.find("tr").find_all("th")
        sleutels = [slugify(th.get_text(strip=True)) for th in header_ths]
        rijen = parse_tabel(tabel)
        if "gb" in sleutels:
            standen = rijen
        else:
            interleague = rijen

    return standen, interleague


def main():
    jaar = dt.datetime.now(dt.timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo(NPB_TZ)
    ).year

    pogingen = 3
    laatste_fout = None
    resultaat = {}
    bronnen = {}

    for poging in range(1, pogingen + 1):
        try:
            resultaat = {}
            bronnen = {}
            for naam, letter in LEAGUES:
                url = BASIS_URL.format(jaar=jaar, league=letter)
                bronnen[naam] = url
                print(f"{naam.capitalize()} League ophalen (poging {poging}/{pogingen}): {url}")
                standen, interleague = haal_standen_op(url)
                resultaat[naam] = {"standings": standen, "interleague": interleague}
                print(f"  {naam}: {len(standen)} teams (standings), {len(interleague)} teams (interleague)")
            break
        except Exception as e:
            laatste_fout = e
            print(f"Poging {poging} mislukt: {e}")
            if poging < pogingen:
                time.sleep(5)
    else:
        raise RuntimeError(f"Alle {pogingen} pogingen mislukt: {laatste_fout}")

    output = {
        "bijgewerkt": dt.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bron": bronnen,
        "season": jaar,
        **resultaat,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n{JSON_FILE} geschreven.")


if __name__ == "__main__":
    main()
