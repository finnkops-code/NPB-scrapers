"""
NPB Scraper — schedule & results (programma en uitslagen) van de Central
League en de Pacific League.
Bron: https://npb.jp/bis/eng/{seizoen}/games/gm{JJJJMMDD}.html

Exact zoals de MLB-referentie (mlb_schedule_scraper.py) qua opzet — één
"uitslagen"-lijst (gisteren, alleen gespeelde wedstrijden) en één
"programma"-lijst (vandaag, ongefilterd) — maar de manier van scrapen wijkt
noodzakelijk af omdat NPB, in tegenstelling tot MLB, geen officiële JSON-API
aanbiedt. Net als bij npb_standings_scraper.py geldt echter dat npb.jp geen
robots.txt heeft (een directe check op /robots.txt geeft een 404, dus geen
enkele crawl-beperking) en dat deze pagina's gewoon kant-en-klare,
server-gerenderde HTML zijn — geen Playwright/headless-browser nodig, alleen
`requests` + `BeautifulSoup`. Dat maakt dit qua principe (de schoonste,
robuustste manier gebruiken die de site toelaat) net zo goed "zoals de
MLB-scraper" als de LMB-schedulescraper dat is met zijn Playwright-aanpak
voor een site die dat wél nodig heeft.

Paginastructuur (live geverifieerd via de browser vóórdat dit geschreven
is):
- De URL is netjes datum-geparametriseerd: gm{JJJJMMDD}.html. Een datum
  zonder wedstrijden (bv. buiten het seizoen) geeft een echte HTTP 404 terug
  — geen soft-404 met status 200 — dus dat behandelen we als "geen
  wedstrijden op deze dag", niet als fout om te retryen.
- Per league is er een apart blok, #central_info / #pacific_info, dat
  volledig kan ONTBREKEN als die league die dag geen wedstrijden heeft (bv.
  een rustdag voor één van de twee leagues).
- Een GESPEELDE wedstrijd is een <a class="link_box" href=".../s....html">
  (een link naar de boxscore-pagina); een NOG TE SPELEN wedstrijd is een
  <span class="link_box"> (geen href, want er is nog geen boxscore). Beide
  bevatten dezelfde team_left/team_right-structuur.
- De "round"-div bevat twee door <br> gescheiden tekstdelen, maar in een
  VERSCHILLENDE volgorde per toestand: "Game N<br>Locatie" voor een
  gespeelde wedstrijd, maar "Locatie<br>Tijd" (dus omgekeerd!) voor een nog
  te spelen wedstrijd. We herkennen daarom elk deel onafhankelijk met een
  eigen patroon (^Game\\s+\\d+$ resp. ^\\d{1,2}:\\d{2}$) i.p.v. te vertrouwen
  op een vaste positie.
- Score-cellen (.score_text) bevatten bij een nog te spelen wedstrijd de
  HTML-entity "&nbsp;" i.p.v. een cijfer — BeautifulSoup's get_text(strip=True)
  zet dat correct om in een lege string (geverifieerd), dus we herkennen een
  "echte" score simpelweg met .isdigit().
- Teamlogo's staan, anders dan op de standen-pagina, gewoon als <img> op
  déze pagina zelf — dus geen aparte hardgecodeerde logo-lijst nodig zoals
  bij de standen-widget.
- Er staat op deze pagina geen enkele pitcher-informatie (bevestigd via een
  brede CSS-selectorzoektocht) — een bewuste, door de bron veroorzaakte
  afwijking t.o.v. de MLB-referentie, die wél probablePitcher-data heeft.
  werper_thuis/werper_uit-achtige velden zijn hier dus niet aanwezig.
- Teamnamen op déze pagina zijn KORTE bijnamen ("Yakult", "Hanshin", "DeNA",
  ...), terwijl de standen-pagina VOLLEDIGE officiële namen gebruikt
  ("Tokyo Yakult Swallows", "Hanshin Tigers", "YOKOHAMA DeNA BAYSTARS", ...)
  — een echte inconsistentie tussen NPB's eigen pagina's. Om dezelfde
  teamidentiteit te tonen als de standen-widget, zetten we de korte naam via
  KORTE_NAAM_NAAR_VOLLEDIG om naar de volledige naam (met de korte naam als
  terugval voor een onbekend/toekomstig team).

De uitvoer-JSON-sleutels van de losse wedstrijd-dicts zijn bewust in het
Engels (league, date, away_team, home_score, played, ...), voor
zelf-consistentie met npb_standings_scraper.py/npb_standings.json (dat om
dezelfde reden Engelse kolomsleutels gebruikt). De algemene, generieke
sleutels rond het scrape-resultaat zelf (bijgewerkt, bron, season) blijven
Nederlands, exact zoals bij npb_standings_scraper.py.
"""
import json
import re
import time
import datetime as dt
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SPEELDAG_URL = "https://npb.jp/bis/eng/{jaar}/games/gm{datum}.html"
NPB_BASIS = "https://npb.jp"
JSON_FILE = "npb_schedule.json"
NPB_TZ = "Asia/Tokyo"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LEAGUE_CONTAINERS = [
    ("central", "central_info"),
    ("pacific", "pacific_info"),
]

# Korte bijnaam (zoals gebruikt op de schedule/games-pagina) -> volledige
# officiële naam (zoals gebruikt op de standen-pagina en dus ook in
# npb_standings.json). Een onbekende/toekomstige korte naam valt terug op
# zichzelf (zie parse_team()) i.p.v. te crashen.
KORTE_NAAM_NAAR_VOLLEDIG = {
    "Yakult": "Tokyo Yakult Swallows",
    "Hanshin": "Hanshin Tigers",
    "DeNA": "YOKOHAMA DeNA BAYSTARS",
    "Seibu": "Saitama Seibu Lions",
    "SoftBank": "Fukuoka SoftBank Hawks",
    "Nippon-Ham": "Hokkaido Nippon-Ham Fighters",
    "Lotte": "Chiba Lotte Marines",
    "Chunichi": "Chunichi Dragons",
    "Hiroshima": "Hiroshima Toyo Carp",
    "Yomiuri": "Yomiuri Giants",
    "ORIX": "ORIX Buffaloes",
    "Rakuten": "Tohoku Rakuten Golden Eagles",
}


def haal_pagina_op(url, timeout=20):
    """Haalt de pagina op als platte HTML-tekst, of None bij een HTTP 404.

    Een 404 op deze speeldag-URL betekent op npb.jp gewoon: geen
    wedstrijden op deze datum (bv. buiten het seizoen, of de pagina voor
    vandaag is nog niet gepubliceerd) — dat is geen fout en wordt dus ook
    niet opnieuw geprobeerd. Andere fouten (timeouts, connectiefouten,
    5xx-statussen) geven wél een uitzondering door, zodat de aanroeper dat
    via de normale retry-lus kan afhandelen."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def parse_team(kant_div):
    """Leest teamnaam + logo uit één .team_left/.team_right-blok."""
    if kant_div is None:
        return None, None
    info = kant_div.find(class_="team_info")
    img = info.find("img") if info else None
    logo = urljoin(NPB_BASIS, img["src"]) if img and img.get("src") else None
    naam_el = info.find(class_="team_name") if info else None
    kort = naam_el.get_text(strip=True) if naam_el else None
    volledig = KORTE_NAAM_NAAR_VOLLEDIG.get(kort, kort) if kort else None
    return volledig, logo


def parse_score(score_el):
    """Geeft de score als int terug, of None bij een lege/"&nbsp;"-cel
    (d.w.z. een nog niet gespeelde wedstrijd)."""
    tekst = score_el.get_text(strip=True) if score_el else ""
    return int(tekst) if tekst.isdigit() else None


def parse_round(round_div):
    """Ontleedt de twee <br>-gescheiden tekstdelen van .round, onafhankelijk
    van hun volgorde (zie moduledocstring: die volgorde verschilt tussen een
    gespeelde en een nog te spelen wedstrijd)."""
    delen = [d.strip() for d in round_div.get_text("\n").split("\n") if d.strip()]

    wedstrijdnummer = None
    starttijd = None
    locatie = None
    for deel in delen:
        match_nummer = re.match(r"^Game\s+(\d+)$", deel, re.IGNORECASE)
        match_tijd = re.match(r"^\d{1,2}:\d{2}$", deel)
        if match_nummer:
            wedstrijdnummer = int(match_nummer.group(1))
        elif match_tijd:
            starttijd = deel
        else:
            locatie = deel
    return wedstrijdnummer, starttijd, locatie


def parse_wedstrijd(box, league, datum_str):
    """Zet één .link_box-element (<a> = gespeeld, <span> = nog te spelen)
    om naar één wedstrijd-dict."""
    gespeeld = box.name == "a"

    weg_naam, weg_logo = parse_team(box.find(class_="team_left"))
    thuis_naam, thuis_logo = parse_team(box.find(class_="team_right"))

    weg_score = parse_score(box.select_one(".team_left .score_text"))
    thuis_score = parse_score(box.select_one(".team_right .score_text"))

    round_div = box.find(class_="round")
    if round_div is not None:
        wedstrijdnummer, starttijd, locatie = parse_round(round_div)
    else:
        wedstrijdnummer, starttijd, locatie = None, None, None

    boxscore_url = urljoin(NPB_BASIS, box["href"]) if gespeeld and box.get("href") else None

    return {
        "league": league,
        "date": datum_str,
        "game_number": wedstrijdnummer,
        "venue": locatie,
        "start_time": starttijd,
        "away_team": weg_naam,
        "away_logo": weg_logo,
        "away_score": weg_score,
        "home_team": thuis_naam,
        "home_logo": thuis_logo,
        "home_score": thuis_score,
        "played": gespeeld,
        "boxscore_url": boxscore_url,
    }


def parse_speeldag(html, datum_str):
    """Ontleedt een volledige gm{JJJJMMDD}.html-pagina naar een lijst van
    wedstrijd-dicts (beide leagues samen, elk gemarkeerd met zijn eigen
    "league"-sleutel)."""
    soup = BeautifulSoup(html, "html.parser")

    wedstrijden = []
    for league, container_id in LEAGUE_CONTAINERS:
        container = soup.find(id=container_id)
        if container is None:
            continue  # deze league had die dag geen wedstrijden (bv. rustdag)
        for box in container.select(".link_box"):
            wedstrijden.append(parse_wedstrijd(box, league, datum_str))
    return wedstrijden


def haal_wedstrijden_voor_datum(jaar, datum):
    """Haalt + ontleedt de wedstrijden voor één kalenderdatum. Geeft een
    lege lijst terug (geen fout) als de pagina niet bestaat (404 = geen
    wedstrijden die dag)."""
    url = SPEELDAG_URL.format(jaar=jaar, datum=datum.strftime("%Y%m%d"))
    html = haal_pagina_op(url)
    if html is None:
        return url, []
    return url, parse_speeldag(html, datum.strftime("%Y-%m-%d"))


def main():
    tz = ZoneInfo(NPB_TZ)
    nu = dt.datetime.now(tz)
    vandaag = nu.date()
    gisteren = vandaag - dt.timedelta(days=1)
    jaar = vandaag.year

    pogingen = 3
    laatste_fout = None
    resultaten = []
    programma = []
    bronnen = {}

    for poging in range(1, pogingen + 1):
        try:
            bronnen = {}

            url_gisteren, wedstrijden_gisteren = haal_wedstrijden_voor_datum(jaar, gisteren)
            bronnen["yesterday"] = url_gisteren
            # "results" toont alleen de wedstrijden van gisteren die ook
            # echt gespeeld zijn — exact zoals de MLB-referentie zijn
            # "uitslagen"-lijst filtert op een niet-lege score. Een
            # afgelaste/uitgestelde wedstrijd van gisteren hoort hier dus
            # niet tussen.
            resultaten = [w for w in wedstrijden_gisteren if w["played"]]
            print(f"Gisteren ({gisteren}, poging {poging}/{pogingen}): "
                  f"{len(wedstrijden_gisteren)} wedstrijden gevonden, {len(resultaten)} gespeeld")

            url_vandaag, wedstrijden_vandaag = haal_wedstrijden_voor_datum(jaar, vandaag)
            bronnen["today"] = url_vandaag
            # "schedule" toont alle wedstrijden van vandaag, ongefilterd —
            # ook hier weer exact zoals de MLB-referentie zijn "programma".
            programma = wedstrijden_vandaag
            print(f"Vandaag ({vandaag}, poging {poging}/{pogingen}): "
                  f"{len(programma)} wedstrijden gevonden")
            break
        except Exception as e:
            laatste_fout = e
            print(f"Poging {poging}/{pogingen} mislukt: {e}")
            if poging < pogingen:
                time.sleep(5)
    else:
        raise RuntimeError(f"Alle {pogingen} pogingen mislukt: {laatste_fout}")

    output = {
        "bijgewerkt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bron": bronnen,
        "season": jaar,
        "results": resultaten,
        "schedule": programma,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n{JSON_FILE} geschreven.")


if __name__ == "__main__":
    main()
