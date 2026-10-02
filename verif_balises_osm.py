"""
verif_balises_osm.py — Pour chaque interdiction poids lourds de DiaLog, OpenStreetMap porte-t-il la balise ?

Pas de calcul d'itinéraire : on lit directement les balises des routes situées sous le tracé
DiaLog. Plus de cas "non concluant" dus à la méthode, et on peut mesurer des centaines de cas.

Entrée : dialog.xml (déjà téléchargé par mesure_dialog.py)
Usage :
    pip install requests
    python verif_balises_osm.py                  # échantillon aléatoire de 500 interdictions
    python verif_balises_osm.py --echantillon 0  # toutes (long : ~1 requête par seconde)

Sorties :
    verif_osm.csv   -> un verdict par interdiction
    cas_osm.json    -> les tronçons et verdicts, utilisés par test_gps_commerciaux.py
    cache_osm/      -> réponses OSM (relancer le script reprend là où il s'est arrêté)

Verdicts :
    OSM_CONNAIT  -> au moins 60 % du tronçon porte une restriction adaptée
    OSM_PARTIEL  -> une partie seulement
    OSM_IGNORE   -> aucune restriction sur les routes appariées
    NON_APPARIE  -> le tracé DiaLog ne tombe pas sur des routes OSM (décalage de tracé)
"""

import argparse, csv, json, math, os, random, time, xml.etree.ElementTree as ET
from collections import Counter, defaultdict
import requests

FICHIER = "dialog.xml"
CACHE = "cache_osm"
HDRS = {"User-Agent": "verif-dialog-osm/1.0 (etude de marche)"}
XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
SEUIL_APPARIEMENT_M = 15
OVERPASS_ACTIF = True   # passe à False dès qu'aucun serveur Overpass ne répond, pour ne pas attendre à chaque cas

BALISES_POIDS = ("maxweight", "maxweight:hgv", "maxweightrating", "maxweightrating:hgv",
                 "maxweight:conditional", "hgv:conditional")
VALEURS_INTERDIT = {"no", "destination", "delivery", "private", "agricultural", "forestry"}


def local(tag):
    return tag.rsplit("}", 1)[-1]


def nombre(t):
    try:
        return float(t.replace(",", "."))
    except Exception:
        return None


# ---------- géométrie ----------

def proj(p, ref):
    """Projection locale en mètres autour de ref."""
    return ((p[1] - ref[1]) * 111320 * math.cos(math.radians(ref[0])), (p[0] - ref[0]) * 110540)


def dist_point_segment(p, a, b):
    P, A, B = proj(p, p), proj(a, p), proj(b, p)
    dx, dy = B[0] - A[0], B[1] - A[1]
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((P[0] - A[0]) * dx + (P[1] - A[1]) * dy) / L2))
    return math.hypot(A[0] + t * dx - P[0], A[1] + t * dy - P[1])


def dist_point_ligne(p, ligne):
    return min(dist_point_segment(p, ligne[i], ligne[i + 1]) for i in range(len(ligne) - 1)) if len(ligne) > 1 else 1e9


def longueur(ligne):
    return sum(math.hypot(*proj(ligne[i + 1], ligne[i])) for i in range(len(ligne) - 1))


def points_echantillon(ligne, n=10):
    """Points répartis le long de la ligne, en évitant les 10 % d'extrémités (carrefours)."""
    L = longueur(ligne)
    if L < 30:
        return [ligne[len(ligne) // 2]]
    cibles = [L * (0.1 + 0.8 * i / (n - 1)) for i in range(n)]
    pts, cumul, j = [], 0.0, 0
    for i in range(len(ligne) - 1):
        seg = math.hypot(*proj(ligne[i + 1], ligne[i]))
        while j < len(cibles) and cumul + seg >= cibles[j]:
            t = (cibles[j] - cumul) / seg if seg else 0
            pts.append((ligne[i][0] + t * (ligne[i + 1][0] - ligne[i][0]),
                        ligne[i][1] + t * (ligne[i + 1][1] - ligne[i][1])))
            j += 1
        cumul += seg
    return pts


def lignes_geojson(txt):
    try:
        g = json.loads(txt)
    except Exception:
        return []
    t = g.get("type")
    if t == "LineString":
        brutes = [g["coordinates"]]
    elif t == "MultiLineString":
        brutes = g["coordinates"]
    elif t == "GeometryCollection":
        return [l for sous in g.get("geometries", []) for l in lignes_geojson(json.dumps(sous))]
    else:
        brutes = []
    return [[(c[1], c[0]) for c in l] for l in brutes if len(l) >= 2]  # GeoJSON = lon, lat


# ---------- lecture DiaLog ----------

def lire_interdictions():
    cas = []
    for _, elem in ET.iterparse(FICHIER, events=("end",)):
        if local(elem.tag) != "trafficRegulationOrder":
            continue
        autorite = desc = ""
        for d in elem.iter():
            ln = local(d.tag)
            if ln in ("issuingAuthority", "issuingAuthorityName") and not autorite:
                autorite = " ".join(t.strip() for t in d.itertext() if t.strip())[:80]
            if ln == "description" and not desc:
                desc = " ".join(t.strip() for t in d.itertext() if t.strip())[:150]
        geo_ordre = [l for d in elem.iter() if local(d.tag) == "geoJsonGeometry" and d.text
                     for l in lignes_geojson(d.text)]
        regs = [d for d in elem.iter() if local(d.tag) == "trafficRegulation"] or [elem]
        for k, reg in enumerate(regs):
            types = {d.get(XSI, "").split(":")[-1] for d in reg.iter() if d.get(XSI)}
            types |= {(d.text or "").strip() for d in reg.iter() if local(d.tag) == "typeOfRegulation"}
            if not any("accessrestriction" in t.lower() for t in types):
                continue
            poids = [nombre(d.text) for d in reg.iter() if "Weight" in local(d.tag) and d.text and nombre(d.text)]
            hauteurs = [nombre(d.text) for d in reg.iter() if "Height" in local(d.tag) and d.text and nombre(d.text)]
            if not poids and not hauteurs:
                continue
            lignes = [l for d in reg.iter() if local(d.tag) == "geoJsonGeometry" and d.text
                      for l in lignes_geojson(d.text)] or geo_ordre
            if not lignes:
                continue
            ligne = max(lignes, key=longueur)
            if len(ligne) > 200:
                pas = len(ligne) // 200 + 1
                ligne = ligne[::pas] + [ligne[-1]]
            cas.append({"id": f'{elem.get("id", "?")}#{k}', "autorite": autorite, "description": desc,
                        "poids_max_t": min(poids) if poids else None,
                        "hauteur_max_m": min(hauteurs) if hauteurs else None,
                        "longueur_m": round(longueur(ligne)), "ligne": ligne})
        elem.clear()
    return cas


# ---------- lecture OSM ----------

def overpass(points):
    global OVERPASS_ACTIF
    if not OVERPASS_ACTIF:
        return None
    corps = "".join(f'way(around:{SEUIL_APPARIEMENT_M},{p[0]:.6f},{p[1]:.6f})["highway"];' for p in points)
    q = f"[out:json][timeout:60];({corps});out tags geom;"
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": q}, headers=HDRS, timeout=(10, 90))
            if r.status_code == 200:
                return [{"tags": w.get("tags", {}), "geom": [(g["lat"], g["lon"]) for g in w.get("geometry", [])]}
                        for w in r.json().get("elements", []) if w.get("type") == "way"]
        except Exception:
            pass
    OVERPASS_ACTIF = False
    print("  Aucun serveur Overpass ne répond : bascule sur l'API OSM pour la suite.")
    return None


def api_osm(points):
    """Repli : API OSM sur une petite boîte autour des points."""
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    m = 0.0004
    bbox = f"{min(lons) - m},{min(lats) - m},{max(lons) + m},{max(lats) + m}"
    r = requests.get(f"https://api.openstreetmap.org/api/0.6/map?bbox={bbox}", headers=HDRS, timeout=90)
    if r.status_code != 200:
        return None
    racine = ET.fromstring(r.content)
    noeuds = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in racine.iter("node")}
    ways = []
    for w in racine.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
        if "highway" in tags:
            ways.append({"tags": tags, "geom": [noeuds[n.get("ref")] for n in w.iter("nd") if n.get("ref") in noeuds]})
    return ways


def ways_pour(cas, points):
    os.makedirs(CACHE, exist_ok=True)
    chemin = os.path.join(CACHE, cas["id"].replace("#", "_").replace("/", "_") + ".json")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            return json.load(f), "cache"
    ways = overpass(points)
    source = "overpass"
    if ways is None:
        ways, source = api_osm(points), "api_osm"
        time.sleep(1)
    if ways is not None:
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(ways, f)
    return ways, source


def restriction_presente(tags, cas):
    if cas["poids_max_t"] is not None:
        if any(k in tags for k in BALISES_POIDS):
            return True
        if any(tags.get(k) in VALEURS_INTERDIT for k in ("hgv", "goods", "motor_vehicle", "access", "vehicle")):
            return True
    if cas["hauteur_max_m"] is not None:
        if "maxheight" in tags or "maxheight:physical" in tags:
            return True
    return False


def verdict(cas):
    points = points_echantillon(cas["ligne"])
    ways, source = ways_pour(cas, points)
    if ways is None:
        return "ERREUR", source, {}
    apparies, avec_restriction, details = 0, 0, Counter()
    for p in points:
        proche = min(((dist_point_ligne(p, w["geom"]), w) for w in ways if len(w["geom"]) > 1),
                     key=lambda x: x[0], default=(1e9, None))
        if proche[0] > SEUIL_APPARIEMENT_M:
            continue
        apparies += 1
        tags = proche[1]["tags"]
        details[tags.get("highway", "?")] += 1
        if restriction_presente(tags, cas):
            avec_restriction += 1
            for k in BALISES_POIDS + ("hgv", "goods", "maxheight"):
                if k in tags:
                    details[f"{k}={tags[k]}"] += 1
    if apparies < max(1, len(points) // 2):
        return "NON_APPARIE", source, details
    part = avec_restriction / apparies
    v = "OSM_CONNAIT" if part >= 0.6 else ("OSM_PARTIEL" if part > 0 else "OSM_IGNORE")
    return v, source, details


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0, c - h), min(1, c + h))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--echantillon", type=int, default=500, help="0 = toutes les interdictions")
    args = ap.parse_args()

    tous = lire_interdictions()
    print(f"{len(tous)} interdictions d'accès poids lourds avec limite chiffrée et tracé dans DiaLog.")
    random.seed(42)
    cas = tous if args.echantillon == 0 else random.sample(tous, min(args.echantillon, len(tous)))
    print(f"Vérification de {len(cas)} interdictions dans OSM...\n")

    for i, c in enumerate(cas, 1):
        v, source, details = verdict(c)
        c["verdict"], c["source"] = v, source
        c["details"] = ", ".join(f"{k}:{n}" for k, n in details.most_common(4))
        if i % 25 == 0 or i == len(cas):
            print(f"  {i}/{len(cas)}  {dict(Counter(x['verdict'] for x in cas[:i]))}")
        if source != "cache":
            time.sleep(1)

    b = Counter(c["verdict"] for c in cas)
    concl = b["OSM_CONNAIT"] + b["OSM_PARTIEL"] + b["OSM_IGNORE"]
    print("\n=== RÉSULTAT ===")
    for k in ("OSM_CONNAIT", "OSM_PARTIEL", "OSM_IGNORE", "NON_APPARIE", "ERREUR"):
        print(f"  {k:12} {b[k]}")
    if concl:
        lo, hi = wilson(b["OSM_IGNORE"], concl)
        print(f"\nOSM ignore {b['OSM_IGNORE']}/{concl} interdictions = {b['OSM_IGNORE'] / concl:.0%} "
              f"(intervalle de confiance 95 % : {lo:.0%} à {hi:.0%})")

    print("\nPar autorité (cas concluants) :")
    par = defaultdict(Counter)
    for c in cas:
        par[c["autorite"] or "(non renseignée)"][c["verdict"]] += 1
    for aut, cc in sorted(par.items(), key=lambda x: -sum(x[1].values()))[:12]:
        n = cc["OSM_CONNAIT"] + cc["OSM_PARTIEL"] + cc["OSM_IGNORE"]
        if n:
            print(f"  {aut[:40]:40} {n:4} cas | OSM ignore {cc['OSM_IGNORE'] / n:4.0%}")

    for nom, filtre in (("limite de poids", lambda c: c["poids_max_t"] is not None),
                        ("limite de hauteur", lambda c: c["hauteur_max_m"] is not None)):
        sous = [c for c in cas if filtre(c) and c["verdict"] in ("OSM_CONNAIT", "OSM_PARTIEL", "OSM_IGNORE")]
        if sous:
            ign = sum(1 for c in sous if c["verdict"] == "OSM_IGNORE")
            print(f"  {nom:18} : OSM ignore {ign}/{len(sous)} ({ign / len(sous):.0%})")

    champs = ["id", "autorite", "description", "poids_max_t", "hauteur_max_m", "longueur_m", "verdict", "source", "details"]
    with open("verif_osm.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";")
        w.writeheader()
        for c in cas:
            w.writerow({k: c[k] for k in champs})
    with open("cas_osm.json", "w", encoding="utf-8") as f:
        json.dump(cas, f, ensure_ascii=False)
    print("\nDétail : verif_osm.csv   |   Tronçons pour le test GPS : cas_osm.json")


if __name__ == "__main__":
    main()
