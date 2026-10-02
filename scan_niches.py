"""
scan_niches.py — Scan large des apps pro françaises et classement des niches.

Idée : faire remonter les apps qui ont (1) beaucoup d'utilisateurs,
(2) une forte part d'avis négatifs RÉCENTS, (3) un modèle payant.

Usage :
    pip install requests
    python scan_niches.py            # 1er passage
    python scan_niches.py            # relance : fusionne avec le cache (contre les flux vides d'Apple)

Sorties :
    classement.csv   -> le tableau trié par score
    verbatims.json   -> les avis 1 à 3 étoiles, avec date, pour les 40 premières apps
    cache/           -> avis bruts par app (réutilisés et fusionnés à chaque relance)
"""

import requests, json, time, math, os, csv, re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

PAYS = "fr"
MIN_NOTES = 30           # ignore les apps avec trop peu de notes
PAGES_AVIS = 4           # 4 pages x 50 = jusqu'à 200 avis récents par app
THREADS = 4
MOIS_RECENTS = 18        # fenêtre "récent" pour le score
TOP_VERBATIMS = 40
CACHE = "cache"
HDRS = {"User-Agent": "Mozilla/5.0"}

MOTS_CLES = [
    # Immobilier / logement
    "gestion locative", "propriétaire bailleur", "état des lieux", "syndic copropriété", "agent immobilier", "location saisonnière",
    # Santé / paramédical
    "kinésithérapeute", "infirmier libéral", "orthophoniste", "ostéopathe", "cabinet médical", "pharmacie gestion", "psychologue cabinet",
    # Beauté / bien-être
    "salon de coiffure", "institut de beauté", "prothésiste ongulaire", "tatoueur", "coach sportif", "salle de sport gestion",
    # Restauration / commerce
    "caisse restaurant", "food truck", "boulangerie", "commerçant caisse", "inventaire stock", "fidélité client", "click and collect",
    # Transport / logistique
    "VTC chauffeur", "taxi", "livreur", "ambulance", "transport routier", "auto-école", "location véhicule",
    # BTP / artisans
    "plombier", "électricien", "paysagiste", "menuisier", "nettoyage entreprise", "piscine entretien", "diagnostic immobilier",
    # Éducation / enfance
    "assistante maternelle", "crèche", "cours particuliers", "école de musique", "professeur planning", "garde d'enfants",
    # Services / indépendants
    "photographe", "traducteur", "avocat cabinet", "notaire", "expert comptable", "agence intérim", "planning employés",
    "pointage salariés", "note de frais", "signature électronique", "prise de rendez-vous", "agenda professionnel",
    # Animaux / agriculture / loisirs
    "vétérinaire", "toiletteur", "pension chevaux", "agriculteur", "élevage", "viticulteur", "club sportif", "association sportive",
    # Événementiel / tourisme
    "chambre d'hôtes", "gîte", "camping gestion", "traiteur", "wedding planner", "billetterie",
    # Divers pro
    "aide à domicile", "EHPAD", "services à la personne", "sécurité incendie", "contrôle technique", "garage automobile", "pressing",
]

EXCLURE = {"Games"}      # genres exclus (primaryGenreName)
MOTS_PAYANT = re.compile(r"\b(abonnement|premium|essai gratuit|version pro|par mois|/mois|€)", re.I)


def get_json(url, params=None, essais=3):
    for i in range(essais):
        try:
            r = requests.get(url, params=params, headers=HDRS, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def chercher(terme):
    d = get_json("https://itunes.apple.com/search",
                 {"term": terme, "country": PAYS, "entity": "software", "limit": 50})
    return (d or {}).get("results", [])


def avis_app(app_id):
    """Récupère les avis, les fusionne avec le cache (par id d'avis). Retourne (liste, statut)."""
    os.makedirs(CACHE, exist_ok=True)
    chemin = os.path.join(CACHE, f"{app_id}.json")
    stock = {}
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            stock = json.load(f)
    nouveaux = 0
    recus = 0
    for page in range(1, PAGES_AVIS + 1):
        url = f"https://itunes.apple.com/{PAYS}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
        d = get_json(url)
        entries = (d or {}).get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            break
        for e in entries:
            if "im:rating" not in e:
                continue
            recus += 1
            rid = e["id"]["label"]
            if rid not in stock:
                nouveaux += 1
            stock[rid] = {
                "note": int(e["im:rating"]["label"]),
                "titre": e["title"]["label"],
                "texte": e["content"]["label"],
                "date": e.get("updated", {}).get("label", ""),
                "version": e.get("im:version", {}).get("label", ""),
            }
        time.sleep(0.5)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(stock, f, ensure_ascii=False)
    if not stock:
        statut = "FLUX_VIDE"
    elif recus == 0:
        statut = "CACHE"
    else:
        statut = "OK"
    return list(stock.values()), statut


def mois_depuis(date_iso):
    try:
        d = datetime.fromisoformat(date_iso)
        return (datetime.now(timezone.utc) - d).days / 30.4
    except Exception:
        return None


def analyser_app(app):
    avis, statut = avis_app(app["trackId"])
    recents = [a for a in avis if (m := mois_depuis(a["date"])) is not None and m <= MOIS_RECENTS]
    neg = [a for a in avis if a["note"] <= 3]
    neg_rec = [a for a in recents if a["note"] <= 3]
    desc = app.get("description", "")
    payant = (app.get("price") or 0) > 0 or bool(MOTS_PAYANT.search(desc))
    pct_rec = len(neg_rec) / len(recents) if len(recents) >= 10 else None
    # Score : taille du marché (log des notes) x frustration récente x bonus payant
    score = 0.0
    if pct_rec is not None:
        score = math.log10(app.get("userRatingCount", 1) + 1) * pct_rec * (1.0 if payant else 0.5)
    return {
        "app": app["trackName"], "id": app["trackId"], "editeur": app.get("sellerName", ""),
        "genre": app.get("primaryGenreName", ""), "termes": app["_termes"],
        "nb_notes": app.get("userRatingCount", 0), "note_moy": app.get("averageUserRating"),
        "prix": app.get("formattedPrice", ""), "payant_probable": payant,
        "avis_ecrits": len(avis), "avis_recents": len(recents),
        "neg_total": len(neg), "neg_recents": len(neg_rec),
        "pct_neg_recents": round(pct_rec * 100, 1) if pct_rec is not None else "",
        "statut_flux": statut, "score": round(score, 3),
        "_verbatims": sorted(neg_rec or neg, key=lambda a: a["date"], reverse=True)[:30],
    }


def main():
    apps = {}
    for i, terme in enumerate(MOTS_CLES, 1):
        for a in chercher(terme):
            if a.get("primaryGenreName") in EXCLURE or a.get("userRatingCount", 0) < MIN_NOTES:
                continue
            apps.setdefault(a["trackId"], {**a, "_termes": []})["_termes"].append(terme)
        print(f"[{i}/{len(MOTS_CLES)}] {terme:28} -> {len(apps)} apps uniques")
        time.sleep(0.4)

    print(f"\nAnalyse des avis de {len(apps)} apps...")
    with ThreadPoolExecutor(THREADS) as ex:
        res = list(ex.map(analyser_app, apps.values()))
    res.sort(key=lambda r: r["score"], reverse=True)

    champs = [k for k in res[0] if not k.startswith("_")]
    with open("classement.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";")
        w.writeheader()
        for r in res:
            w.writerow({k: (", ".join(v) if isinstance(v, list) else v) for k, v in r.items() if k in champs})

    with open("verbatims.json", "w", encoding="utf-8") as f:
        json.dump([{"app": r["app"], "score": r["score"], "pct_neg_recents": r["pct_neg_recents"],
                    "nb_notes": r["nb_notes"], "avis": r["_verbatims"]} for r in res[:TOP_VERBATIMS]],
                  f, ensure_ascii=False, indent=2)

    vides = [r["app"] for r in res if r["statut_flux"] == "FLUX_VIDE"]
    print("\nTOP 25")
    for r in res[:25]:
        print(f'{r["score"]:6} | {str(r["pct_neg_recents"]):>5}% neg récents | {r["nb_notes"]:6} notes | '
              f'{"€" if r["payant_probable"] else " "} | {r["app"][:40]}')
    print(f"\n{len(vides)} apps en FLUX_VIDE (relance le script plus tard pour les récupérer).")


if __name__ == "__main__":
    main()
