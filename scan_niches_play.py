"""
scan_niches_play.py — Scan des apps pro françaises sur Google Play et classement des niches.

Fait remonter les apps qui ont :
  (1) un gros marché        -> nombre de notes et d'installations
  (2) une frustration récente et partagée -> part d'avis 1 à 3 étoiles des 18 derniers mois,
                                pondérée par les votes "utile" (thumbsUp)
  (3) des clients qui paient -> champs réels offersIAP / prix (plus de déduction)

Usage :
    pip install google-play-scraper
    (si la recherche de la librairie plante, le script lit directement la page de recherche Play)
    python scan_niches_play.py              # relançable : fusionne avec le cache
    python scan_niches_play.py --hors-ligne # recalcule le classement depuis cache_play/, sans réseau

Sorties :
    classement_play.csv   -> toutes les apps, triées par score (s'ouvre dans Excel)
    verbatims_play.json   -> avis négatifs récents des 40 premières apps, triés par votes utiles
    cache_play/           -> fiche + avis bruts par app
"""

import json, os, csv, sys, time, math, re, urllib.parse, urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from google_play_scraper import search, app as fiche_app, reviews, Sort

LANG, PAYS = "fr", "fr"
MIN_NOTES = 30
MAX_NOTES = 50000         # écarte les apps géantes grand public (None = pas de plafond)
GENRES_AUTORISES = {      # genreId Google Play retenus (ensemble vide = toutes catégories hors jeux)
    "BUSINESS", "PRODUCTIVITY", "MEDICAL", "EDUCATION", "AUTO_AND_VEHICLES",
    "MAPS_AND_NAVIGATION", "HOUSE_AND_HOME", "FOOD_AND_DRINK", "FINANCE", "PARENTING",
}
AVIS_PAR_APP = 300        # avis les plus récents récupérés par passage
MOIS_RECENTS = 18
MIN_AVIS_RECENTS = 10     # en dessous, pas de score (échantillon trop petit)
THREADS = 3
TOP_VERBATIMS = 40
CACHE = "cache_play"
FICHIER_TERMES = os.path.join(CACHE, "_termes.json")   # mots-clés par app, pour le mode hors ligne

MOTS_CLES = [
    "gestion locative", "propriétaire bailleur", "état des lieux", "syndic copropriété", "agent immobilier", "location saisonnière",
    "kinésithérapeute", "infirmier libéral", "orthophoniste", "ostéopathe", "cabinet médical", "pharmacie gestion", "psychologue cabinet",
    "salon de coiffure", "institut de beauté", "prothésiste ongulaire", "tatoueur", "coach sportif", "salle de sport gestion",
    "caisse restaurant", "food truck", "boulangerie", "commerçant caisse", "inventaire stock", "fidélité client", "click and collect",
    "VTC chauffeur", "taxi", "livreur", "ambulance", "transport routier", "auto-école", "location véhicule",
    "plombier", "électricien", "paysagiste", "menuisier", "nettoyage entreprise", "piscine entretien", "diagnostic immobilier",
    "assistante maternelle", "crèche", "cours particuliers", "école de musique", "professeur planning", "garde d'enfants",
    "photographe", "traducteur", "avocat cabinet", "notaire", "expert comptable", "agence intérim", "planning employés",
    "pointage salariés", "note de frais", "signature électronique", "prise de rendez-vous", "agenda professionnel",
    "vétérinaire", "toiletteur", "pension chevaux", "agriculteur", "élevage", "viticulteur", "club sportif", "association sportive",
    "chambre d'hôtes", "gîte", "camping gestion", "traiteur", "wedding planner", "billetterie",
    "aide à domicile", "EHPAD", "services à la personne", "sécurité incendie", "contrôle technique", "garage automobile", "pressing",
]


def avec_essais(fonction, *args, essais=3, **kwargs):
    for i in range(essais):
        try:
            return fonction(*args, **kwargs)
        except Exception as e:
            derniere = e
            time.sleep(3 * (i + 1))
    raise derniere


def chercher_ids_page(terme):
    # Repli : google-play-scraper 1.2.7 plante sur la recherche (page Play modifiée),
    # on lit directement les liens /store/apps/details?id=... de la page de résultats.
    url = "https://play.google.com/store/search?" + urllib.parse.urlencode(
        {"q": terme, "c": "apps", "hl": LANG, "gl": PAYS})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    return list(dict.fromkeys(re.findall(r'/store/apps/details\?id=([\w.]+)', html)))


def chercher_ids(terme):
    try:
        res = avec_essais(search, terme, lang=LANG, country=PAYS, n_hits=30, essais=1)
        ids = [r["appId"] for r in res if r.get("appId")]
        if ids:
            return ids
    except Exception:
        pass
    try:
        return avec_essais(chercher_ids_page, terme)
    except Exception:
        return []


def charger_cache(app_id):
    chemin = os.path.join(CACHE, f"{app_id}.json")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    return {"fiche": None, "avis": {}}


def sauver_cache(app_id, data):
    with open(os.path.join(CACHE, f"{app_id}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def recuperer(app_id):
    """Fiche + avis, fusionnés avec le cache. Statut : OK / CACHE / FLUX_VIDE / INTROUVABLE."""
    data = charger_cache(app_id)
    try:
        f = avec_essais(fiche_app, app_id, lang=LANG, country=PAYS)
        data["fiche"] = {k: f.get(k) for k in (
            "title", "developer", "genre", "genreId", "score", "ratings", "realInstalls", "installs",
            "free", "price", "offersIAP", "inAppProductPrice", "updated")}
    except Exception:
        if not data["fiche"]:
            return data, "INTROUVABLE"

    recus = 0
    try:
        liste, _ = avec_essais(reviews, app_id, lang=LANG, country=PAYS, sort=Sort.NEWEST, count=AVIS_PAR_APP)
        for r in liste:
            recus += 1
            data["avis"][r["reviewId"]] = {
                "note": r["score"],
                "texte": r.get("content") or "",
                "date": r["at"].replace(tzinfo=timezone.utc).isoformat() if r.get("at") else "",
                "votes_utiles": r.get("thumbsUpCount") or 0,
                "version": r.get("appVersion") or "",
                "reponse_dev": bool(r.get("replyContent")),
            }
    except Exception:
        pass

    sauver_cache(app_id, data)
    if not data["avis"]:
        statut = "FLUX_VIDE"
    elif recus == 0:
        statut = "CACHE"
    else:
        statut = "OK"
    return data, statut


def mois_depuis(date_iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(date_iso)).days / 30.4
    except Exception:
        return None


def poids(avis):
    # Un avis approuvé par beaucoup de gens pèse plus, mais sans écraser les autres
    return 1 + math.log1p(avis["votes_utiles"])


def analyser(app_id):
    data, statut = recuperer(app_id)
    return calculer(app_id, data, statut)


def calculer(app_id, data, statut):
    f = data["fiche"]
    if not f:
        return None
    genre_id = f.get("genreId") or ""
    nb_notes = f.get("ratings") or 0
    if genre_id.startswith("GAME") or (GENRES_AUTORISES and genre_id not in GENRES_AUTORISES):
        return None
    if nb_notes < MIN_NOTES or (MAX_NOTES is not None and nb_notes > MAX_NOTES):
        return None
    avis = list(data["avis"].values())
    recents = [a for a in avis if (m := mois_depuis(a["date"])) is not None and m <= MOIS_RECENTS]
    neg_rec = [a for a in recents if a["note"] <= 3]

    pct_brut = pct_pond = None
    if len(recents) >= MIN_AVIS_RECENTS:
        pct_brut = len(neg_rec) / len(recents)
        pct_pond = sum(poids(a) for a in neg_rec) / sum(poids(a) for a in recents)

    payant = bool(f.get("offersIAP")) or not f.get("free", True)
    rep_dev = [a for a in neg_rec if a["reponse_dev"]]
    score = 0.0
    if pct_brut is not None:
        score = math.log10(nb_notes + 1) * pct_brut * (1.0 if payant else 0.5)

    return {
        "app": f.get("title"), "id": app_id, "editeur": f.get("developer"), "genre": f.get("genre"),
        "genre_id": genre_id,
        "termes": ", ".join(TERMES.get(app_id, [])),
        "nb_notes": f.get("ratings"), "installations": f.get("realInstalls") or f.get("installs"),
        "note_moy": round(f["score"], 2) if f.get("score") else "",
        "achats_integres": bool(f.get("offersIAP")), "prix_achats": f.get("inAppProductPrice") or "",
        "app_payante": not f.get("free", True),
        "avis_recuperes": len(avis), "avis_recents": len(recents), "neg_recents": len(neg_rec),
        "pct_neg_recents": round(pct_brut * 100, 1) if pct_brut is not None else "",
        "pct_neg_pondere": round(pct_pond * 100, 1) if pct_pond is not None else "",
        "pct_reponse_dev": round(len(rep_dev) / len(neg_rec) * 100) if neg_rec else "",
        "derniere_maj": datetime.fromtimestamp(f["updated"]).date().isoformat() if f.get("updated") else "",
        "statut_flux": statut, "score": round(score, 3),
        "_verbatims": sorted(neg_rec, key=lambda a: (a["votes_utiles"], a["date"]), reverse=True)[:30],
    }


TERMES = {}


def scanner():
    """Recherche + téléchargement des fiches et avis (réseau)."""
    for i, terme in enumerate(MOTS_CLES, 1):
        for app_id in chercher_ids(terme):
            TERMES.setdefault(app_id, []).append(terme)
        print(f"[{i}/{len(MOTS_CLES)}] {terme:28} -> {len(TERMES)} apps uniques")
        time.sleep(1)
    with open(FICHIER_TERMES, "w", encoding="utf-8") as fh:
        json.dump(TERMES, fh, ensure_ascii=False)

    print(f"\nRécupération des fiches et avis de {len(TERMES)} apps...")
    with ThreadPoolExecutor(THREADS) as ex:
        return [r for r in ex.map(analyser, list(TERMES)) if r]


def relire_cache():
    """Mode hors ligne : recalcule tout depuis cache_play/, sans aucune requête."""
    if os.path.exists(FICHIER_TERMES):
        with open(FICHIER_TERMES, encoding="utf-8") as fh:
            TERMES.update(json.load(fh))
    res = []
    for nom in sorted(os.listdir(CACHE)):
        if not nom.endswith(".json") or nom.startswith("_"):
            continue
        app_id = nom[:-5]
        data = charger_cache(app_id)
        r = calculer(app_id, data, "CACHE" if data["avis"] else "FLUX_VIDE")
        if r:
            res.append(r)
    print(f"Hors ligne : {len(res)} apps retenues depuis {CACHE}/")
    return res


def main():
    os.makedirs(CACHE, exist_ok=True)
    res = relire_cache() if "--hors-ligne" in sys.argv[1:] else scanner()
    res.sort(key=lambda r: r["score"], reverse=True)
    if not res:
        print("Aucune app exploitable. Vérifie la connexion ou relance plus tard.")
        return

    champs = [k for k in res[0] if not k.startswith("_")]
    with open("classement_play.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=champs, delimiter=";")
        w.writeheader()
        for r in res:
            w.writerow({k: r[k] for k in champs})

    with open("verbatims_play.json", "w", encoding="utf-8") as fh:
        json.dump([{k: r[k] for k in ("app", "score", "nb_notes", "achats_integres", "prix_achats",
                                      "pct_neg_recents", "pct_neg_pondere", "pct_reponse_dev")}
                   | {"avis": r["_verbatims"]} for r in res[:TOP_VERBATIMS]],
                  fh, ensure_ascii=False, indent=2)

    statuts = {}
    for r in res:
        statuts[r["statut_flux"]] = statuts.get(r["statut_flux"], 0) + 1
    print("\nTOP 25")
    for r in res[:25]:
        print(f'{r["score"]:6} | {str(r["pct_neg_recents"]):>5}% neg récents | {str(r["nb_notes"]):>7} notes | '
              f'{"€" if r["achats_integres"] or r["app_payante"] else " "} | {r["app"][:40]}')
    print(f"\n{len(res)} apps retenues. Statuts : {statuts}")


if __name__ == "__main__":
    main()
