"""
NPB Scraper — spelersstatistieken (batting & pitching) per team.
Bron: https://npb.jp/bis/eng/{seizoen}/stats/ (indexpagina met, per team, een
      link naar zijn Batting- en Pitching-pagina)
      https://npb.jp/bis/eng/{seizoen}/stats/idb1_{teamcode}.html (batting)
      https://npb.jp/bis/eng/{seizoen}/stats/idp1_{teamcode}.html (pitching)

Zelfde situatie als npb_standings_scraper.py en npb_schedule_scraper.py: geen
robots.txt, gewoon server-gerenderde HTML, dus `requests` + `BeautifulSoup`,
geen Playwright nodig.

De indexpagina bevat een "Player Stats by Team"-sectie met twee
<div class="league_unit">-blokken (Central League eerst, Pacific League
tweede — live geverifieerd; er staat geen expliciet league-label in de HTML
zelf, dus dit is een positie-conventie i.p.v. een uit de DOM af te lezen
gegeven) met daarin, per team, een <dl> met de teamnaam (dt > img[title]) en
twee links (dd > a): "B" (batting) en "P" (pitching). We lezen deze sectie
dynamisch uit i.p.v. de 12 teamcodes/URL's hard te coderen — zo blijft de
scraper werken ook als de team-URL's ooit veranderen.

Elke batting-/pitching-pagina bevat één simpele <table class="tablefix2">
(dezelfde tabelclass als de standenpagina's), met de kolomkoppen letterlijk
uitgelezen (net als bij npb_standings_scraper.py) i.p.v. hardgecodeerd — zo
blijft de scraper ook werken als NPB een kolom toevoegt of hernoemt. Eén
uitzondering: de eerste kolom heet op de site zelf "Player" op de
batting-pagina maar "Pitcher" op de pitching-pagina — voor een uniforme vorm
tussen beide lijsten krijgt die kolom hier altijd de vaste sleutel "name".

Twee bijzonderheden in die naamcel:
- Een linkshandige batter/werper krijgt een "<sup>*</sup>"-teken vóór zijn
  naam en de klasse "left-hand" op de cel; een schakelslagman (alleen bij
  batting) krijgt "<sup>+</sup>" en de klasse "switch-hitter" (site-legenda:
  "* Lefthanded batter. + Switch-hitter." resp. "* Throws lefthanded.").
  Rechtshandig is de stille standaard (geen klasse, geen teken). We knippen
  het <sup>-teken uit de cel voordat we de naam uitlezen (anders komt de
  "*"/"+" vast aan de naam te staan) en leiden de handedness af uit de
  CSS-klasse i.p.v. uit het teken zelf.
- Op de pitching-pagina staat de "IP" (innings pitched)-waarde soms verdeeld
  over twee losse <span>'s: <span class="integer">10</span><span
  class="decimal">.2</span> (10.2 innings) — of alleen een
  <span class="integer">43</span> zonder decimal-span als het precies 43.0
  innings is. get_text(strip=True) op de hele cel plakt dit vanzelf correct
  aan elkaar ("10.2" resp. "43"), dus daar is geen aparte parsing voor nodig.

Net als bij de standen-scraper blijven kolommen met een "."-teken (AVG, SLG,
OBP, PCT, ERA) bewust een string (geen float/afronding); pure cijferkolommen
(G, PA, AB, ..., W, L, SV, ...) worden wél naar int omgezet. Dat gebeurt hier
generiek (elke celwaarde die volledig uit cijfers bestaat wordt int) i.p.v.
per veld hard te coderen, want zo goed als elke niet-decimale kolom in deze
tabellen is een geheel getal. "IP" is hierdoor bewust een uitzonderingsgeval
met een wisselend type: bij een gebroken innings-aantal (bv. "10.2") blijft
het een string (er zit een punt in), maar bij precies een heel aantal
innings (geen decimal-span, dus gewoon "43" als celtekst) wordt het, net als
elke andere pure-cijferkolom, een int. De widget die deze data toont moet
dus met beide types rekening houden.

Twee velden zijn dus altijd zelf toegevoegd (niet uit de tabel zelf
afkomstig): "team" (de volledige officiële teamnaam, exact zoals ook
npb_standings.json en npb_schedule.json die gebruiken) en "league"
("central"/"pacific"). Beide worden voor élke speler uit dezelfde
indexpagina-bron gehaald in plaats van apart per batting-/pitching-pagina
uitgelezen — dat voorkomt bij ontwerp precies het soort teamnaam-mismatch
dat eerder een echte bug was bij de LMB-widget (waar de batting- en
pitching-pagina's elk een net iets andere naamvariant voor hetzelfde team
teruggaven).

Curiositeit, puur ter documentatie: de pitching-pagina hergebruikt de
kolomkop "HP" voor een heel ander begrip ("Relief Wins+Holds", per de
site's eigen kolom-legenda) dan op de batting-pagina ("Hit by Pitch").
Functioneel geen probleem — batting- en pitching-rijen staan in gescheiden
lijsten met hun eigen sleutels — maar goed om te weten mocht "hp" ooit
tussen de twee lijsten vergeleken worden.
"""
import json
import re
import time
import datetime as dt
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

INDEX_URL = "https://npb.jp/bis/eng/{jaar}/stats/"
JSON_FILE = "npb_player_stats.json"
NPB_TZ = "Asia/Tokyo"
NPB_BASIS = "https://npb.jp"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def haal_pagina_op(url, timeout=20):
    """Haalt de pagina op als platte HTML-tekst. Geen Playwright nodig (zie
    de moduledocstring): dit is gewone, server-gerenderde HTML."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def slugify(header: str) -> str:
    """Zet een kolomkop uit de site ("2B", "AVG") om naar een nette, stabiele
    JSON-sleutel ("2b", "avg")."""
    return re.sub(r"\s+", "_", header.strip().lower())


def haal_teams_op(jaar):
    """Leest de "Player Stats by Team"-sectie van de indexpagina en geeft een
    lijst terug van {team, league, batting_url, pitching_url}, dynamisch
    (geen hardgecodeerde teamcodes/URL's — zie moduledocstring)."""
    html = haal_pagina_op(INDEX_URL.format(jaar=jaar))
    soup = BeautifulSoup(html, "html.parser")

    heading = soup.find(
        lambda tag: tag.name in ("h1", "h2", "h3", "h4", "h5")
        and "Player Stats by Team" in tag.get_text()
    )
    sectie = heading.find_parent("section") if heading else None
    if sectie is None:
        raise RuntimeError("'Player Stats by Team'-sectie niet gevonden op de indexpagina")

    teams = []
    league_units = sectie.select(".league_unit")
    for unit_idx, unit in enumerate(league_units):
        # Positie-conventie i.p.v. een DOM-gegeven: de eerste .league_unit is
        # altijd de Central League, de tweede altijd de Pacific League (zie
        # moduledocstring).
        league = "central" if unit_idx == 0 else "pacific"
        for dl in unit.find_all("dl"):
            img = dl.find("img")
            naam = img.get("title") if img else None
            links = {a.get_text(strip=True): a.get("href") for a in dl.select("dd a")}
            batting_href = links.get("B")
            pitching_href = links.get("P")
            if not naam or not batting_href or not pitching_href:
                continue  # onverwachte/onvolledige rij: overslaan i.p.v. crashen
            teams.append({
                "team": naam,
                "league": league,
                "batting_url": urljoin(NPB_BASIS, batting_href),
                "pitching_url": urljoin(NPB_BASIS, pitching_href),
            })
    return teams


def parse_naam_en_hand(td):
    """Haalt de spelersnaam en de handedness (L/R/S) uit de eerste
    kolomcel — zie moduledocstring voor de <sup>-markering/CSS-klasse."""
    sup = td.find("sup")
    if sup is not None:
        sup.extract()
    naam = td.get_text(strip=True)

    klassen = td.get("class") or []
    if "left-hand" in klassen:
        hand = "L"
    elif "switch-hitter" in klassen:
        hand = "S"
    else:
        hand = "R"
    return naam, hand


def parse_stats_tabel(tabel):
    """Zet één <table class="tablefix2"> (batting óf pitching) om naar een
    lijst van rij-dicts. De kolomsleutels komen uit de tabel zelf (zie
    slugify()), behalve de eerste kolom, die altijd de vaste sleutel "name"
    krijgt (zie moduledocstring: "Player" vs "Pitcher")."""
    header_ths = tabel.select("thead th") or tabel.find("tr").find_all("th")
    sleutels = [slugify(th.get_text(strip=True)) for th in header_ths]

    rijen = []
    for tr in tabel.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue  # de header-rij zelf heeft geen <td>'s, alleen <th>'s

        naam, hand = parse_naam_en_hand(tds[0])
        rij = {"name": naam, "hand": hand}

        for sleutel, td in zip(sleutels[1:], tds[1:]):
            waarde = td.get_text(strip=True)
            if waarde.lstrip("-").isdigit():
                waarde = int(waarde)
            rij[sleutel] = waarde

        rijen.append(rij)
    return rijen


def haal_team_stats_op(url):
    """Haalt + ontleedt de spelerstabel van één batting- of pitching-pagina."""
    html = haal_pagina_op(url)
    soup = BeautifulSoup(html, "html.parser")
    tabel = soup.select_one("table.tablefix2")
    if tabel is None:
        return []
    return parse_stats_tabel(tabel)


def main():
    jaar = dt.datetime.now(ZoneInfo(NPB_TZ)).year

    pogingen = 3
    laatste_fout = None
    batting = []
    pitching = []
    teams_meta = []

    for poging in range(1, pogingen + 1):
        try:
            batting = []
            pitching = []
            teams_meta = []

            teams = haal_teams_op(jaar)
            print(f"{len(teams)} teams gevonden op de indexpagina (poging {poging}/{pogingen})")

            for team in teams:
                teams_meta.append({"team": team["team"], "league": team["league"]})

                print(f"  {team['team']} — batting: {team['batting_url']}")
                for rij in haal_team_stats_op(team["batting_url"]):
                    batting.append({"team": team["team"], "league": team["league"], **rij})

                print(f"  {team['team']} — pitching: {team['pitching_url']}")
                for rij in haal_team_stats_op(team["pitching_url"]):
                    pitching.append({"team": team["team"], "league": team["league"], **rij})
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
        "bron": INDEX_URL.format(jaar=jaar),
        "season": jaar,
        "teams": teams_meta,
        "batting": batting,
        "pitching": pitching,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n{JSON_FILE} geschreven. {len(batting)} batting-rijen, {len(pitching)} pitching-rijen.")


if __name__ == "__main__":
    main()
