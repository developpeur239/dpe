"""
mesure_dialog.py — Que contient DiaLog, et OpenStreetMap connaît-il déjà ces interdictions ?

Étape 1 (couverture) : télécharge la base nationale DiaLog (DATEX II, ~31 Mo, data.gouv.fr)
  et compte les arrêtés : combien, qui les publie, lesquels visent les poids lourds,
  permanents ou temporaires, combien autour de Calais.

Étape 2 (écart OSM) : pour un échantillon d'interdictions poids lourds permanentes,
  demande à Valhalla un itinéraire qui emprunte naturellement le tronçon en voiture,
  puis le même trajet en camion au-dessus de la limite. Si le camion passe quand même,
  OSM ignore l'interdiction : c'est l'écart que DiaLog pourrait combler.

Usage :
    pip install requests
    python mesure_dialog.py                 # étape 1 seulement
    python mesure_dialog.py --croiser 40    # étapes 1 + 2 sur 40 interdictions tirées au hasard

Le format DATEX II de DiaLog n'est pas documenté en détail ici : le script lit le XML
sans dépendre des espaces de noms et affiche un exemple brut d'arrêté pour qu'on puisse
ajuster l'extraction si un champ est mal lu.
"""

import argparse, csv, json, math, os, random, time, xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
import requests

URLS = [
    "https://www.data.gouv.fr/api/1/datasets/r/1b9c1379-79ff-437b-91bf-149179e3bca2",
    "https://dialog.beta.gouv.fr/api/regulations/datex",   # export direct de DiaLog, si data.gouv.fr est injoignable
]
FICHIER = "dialog.xml"
CALAIS = (50.9513, 1.8587)
RAYON_KM = 50
HDRS = {"User-Agent": "mesure-dialog/1.0 (etude de marche)"}
XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"


def local(tag):
    return tag.rsplit("}", 1)[-1]


def telecharger():
    if os.path.exists(FICHIER) and time.time() - os.path.getmtime(FICHIER) < 86400:
        print(f"Fichier {FICHIER} déjà présent (moins de 24 h), réutilisé.")
        return
    print("Téléchargement de DiaLog...")
    for url in URLS:
        try:
            with requests.get(url, headers=HDRS, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(FICHIER + ".part", "wb") as f:
                    for bloc in r.iter_content(1 << 16):
                        f.write(bloc)
            os.replace(FICHIER + ".part", FICHIER)
            break
        except Exception as e:
            print(f"  échec sur {url} ({type(e).__name__}), source suivante...")
    else:
        raise SystemExit("Impossible de télécharger DiaLog.")
    print(f"  {os.path.getsize(FICHIER) / 1e6:.1f} Mo téléchargés.")


def nombre(t):
    try:
        return float(t.replace(",", "."))
    except Exception:
        return None


def texte_de(elem, nom):
    for d in elem.iter():
        if local(d.tag) == nom:
            return " ".join(t.strip() for t in d.itertext() if t.strip())[:200]
    return ""


def lignes_geojson(texte):
    """DiaLog met le tracé en GeoJSON (ordre lon, lat) dans <geoJsonGeometry>."""
    try:
        g = json.loads(texte)
    except Exception:
        return []
    t, c = g.get("type"), g.get("coordinates") or []
    if t == "Point":
        c = [[c]]
    elif t in ("LineString", "MultiPoint"):
        c = [c]
    elif t == "Polygon":
        pass
    elif t != "MultiLineString":
        return []
    return [[(p[1], p[0]) for p in ligne if len(p) >= 2] for ligne in c]


def lire_coords(elem):
    """Retourne (tous les points, liste des tronçons continus)."""
    pts, lignes = [], []
    lats, lons = [], []
    for d in elem.iter():
        ln = local(d.tag)
        if ln == "geoJsonGeometry" and d.text:
            for ligne in lignes_geojson(d.text):
                lignes.append(ligne)
                pts += ligne
        elif ln == "posList" and d.text:
            v = [nombre(x) for x in d.text.split()]
            v = [x for x in v if x is not None]
            paires = list(zip(v[0::2], v[1::2]))
            # En France, la latitude est entre 41 et 52 : on détecte l'ordre lat/lon
            if paires and not (41 <= paires[0][0] <= 52):
                paires = [(b, a) for a, b in paires]
            pts += paires
            lignes.append(paires)
        elif ln == "latitude" and d.text:
            lats.append(nombre(d.text))
        elif ln == "longitude" and d.text:
            lons.append(nombre(d.text))
    pts += [(a, b) for a, b in zip(lats, lons) if a is not None and b is not None]
    return pts, lignes


def analyser_arrete(elem):
    poids, hauteurs, longueurs, vehicules, types, dates_fin, dates_debut = [], [], [], set(), set(), [], []
    danger = False
    for d in elem.iter():
        ln, t = local(d.tag), (d.text or "").strip()
        if d.get(XSI):
            types.add(d.get(XSI).split(":")[-1])
        if ln == "typeOfRegulation" and t:
            types.add(t)
        if t and nombre(t) is not None:
            if "Weight" in ln: poids.append(nombre(t))
            elif "Height" in ln: hauteurs.append(nombre(t))
            elif "Length" in ln: longueurs.append(nombre(t))
        if ln in ("vehicleType", "vehicleUsage", "loadType", "fuelType") and t:
            vehicules.add(t)
        if "angerous" in ln or "azardous" in t or "angerous" in t:
            danger = True
        if ln in ("overallEndTime", "endTime", "validityEndTime") and t:
            dates_fin.append(t)
        if ln in ("overallStartTime", "startTime") and t:
            dates_debut.append(t)

    lourds = any(k in v.lower() for v in vehicules for k in ("lorry", "heavy", "goods", "truck", "articulated"))
    vise_pl = bool(poids or hauteurs or longueurs or lourds or danger)
    fin = max(dates_fin) if dates_fin else ""
    coords, lignes = lire_coords(elem)
    plus_longue = max(lignes, key=len) if lignes else []
    centre = (sum(p[0] for p in coords) / len(coords), sum(p[1] for p in coords) / len(coords)) if coords else None
    return {
        "id": elem.get("id", ""),
        "autorite": texte_de(elem, "issuingAuthority") or texte_de(elem, "issuingAuthorityName"),
        "description": texte_de(elem, "description"),
        "types": ", ".join(sorted(types)),
        "vise_pl": vise_pl,
        "poids_max_t": min(poids) if poids else "",
        "hauteur_max_m": min(hauteurs) if hauteurs else "",
        "longueur_max_m": min(longueurs) if longueurs else "",
        "vehicules": ", ".join(sorted(vehicules)),
        "matieres_dangereuses": danger,
        "debut": min(dates_debut) if dates_debut else "",
        "fin": fin,
        "permanent": not fin,
        "nb_points": len(coords),
        "lat": round(centre[0], 5) if centre else "",
        "lon": round(centre[1], 5) if centre else "",
        "nb_troncons": len(lignes),
        "_troncon": plus_longue,
    }


def dist_km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    x = math.radians(b[1] - a[1]) * math.cos((p1 + p2) / 2)
    return 6371 * math.hypot(x, p2 - p1)


def etape1():
    telecharger()
    noms = Counter()
    arretes, exemple = [], None
    for _, elem in ET.iterparse(FICHIER, events=("end",)):
        ln = local(elem.tag)
        noms[ln] += 1
        if ln == "trafficRegulationOrder":
            if exemple is None:
                exemple = [(local(d.tag), (d.text or "").strip()[:80], d.get(XSI, ""))
                           for d in elem.iter() if (d.text or "").strip() or d.get(XSI)][:60]
            arretes.append(analyser_arrete(elem))
            elem.clear()

    if not arretes:
        print("\nAucun élément 'trafficRegulationOrder' trouvé. Balises les plus fréquentes :")
        for n, c in noms.most_common(40):
            print(f"   {c:7}  {n}")
        print("Envoie-moi cette liste pour adapter l'extraction.")
        return []

    print("\nExemple brut du premier arrêté (pour vérifier l'extraction) :")
    for ln, t, typ in exemple:
        print(f"   {ln:35} {t:60} {typ}")

    maintenant = datetime.now(timezone.utc).isoformat()
    pl = [a for a in arretes if a["vise_pl"]]
    actifs = [a for a in arretes if a["permanent"] or a["fin"] >= maintenant[:10]]
    pl_perm = [a for a in pl if a["permanent"]]
    geo = [a for a in arretes if a["lat"] != ""]
    calais = [a for a in geo if dist_km((a["lat"], a["lon"]), CALAIS) <= RAYON_KM]

    print(f"\n=== COUVERTURE DIALOG ===")
    print(f"Arrêtés au total               : {len(arretes)}")
    print(f"  dont encore en vigueur       : {len(actifs)}")
    print(f"  dont visant les poids lourds : {len(pl)}  (permanents : {len(pl_perm)})")
    print(f"     avec limite de poids      : {sum(1 for a in pl if a['poids_max_t'] != '')}")
    print(f"     avec limite de hauteur    : {sum(1 for a in pl if a['hauteur_max_m'] != '')}")
    print(f"     matières dangereuses      : {sum(1 for a in pl if a['matieres_dangereuses'])}")
    print(f"  avec géométrie exploitable   : {len(geo)}")
    print(f"  à moins de {RAYON_KM} km de Calais     : {len(calais)} (dont poids lourds : {sum(1 for a in calais if a['vise_pl'])})")
    print("\nAutorités qui publient le plus :")
    for aut, n in Counter(a["autorite"] or "(non renseignée)" for a in arretes).most_common(20):
        print(f"   {n:6}  {aut[:70]}")
    print("\nTypes de réglementation :")
    for t, n in Counter(t for a in arretes for t in a["types"].split(", ") if t).most_common(15):
        print(f"   {n:6}  {t}")

    champs = [k for k in arretes[0] if not k.startswith("_")]
    with open("dialog_couverture.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";")
        w.writeheader()
        for a in arretes:
            w.writerow({k: a[k] for k in champs})
    print("\nDétail complet : dialog_couverture.csv")
    return arretes


# ---------- Étape 2 : écart avec OpenStreetMap ----------

def decode_polyline(s, precision=6):
    coords, idx, lat, lon, f = [], 0, 0, 0, 10 ** precision
    while idx < len(s):
        for i in range(2):
            shift = res = 0
            while True:
                b = ord(s[idx]) - 63; idx += 1
                res |= (b & 0x1f) << shift; shift += 5
                if b < 0x20:
                    break
            d = ~(res >> 1) if res & 1 else res >> 1
            if i == 0: lat += d
            else: lon += d
        coords.append((lat / f, lon / f))
    return coords


def decaler(p, q, metres):
    """Point situé à `metres` au-delà de p, dans la direction q -> p."""
    dlat, dlon = p[0] - q[0], p[1] - q[1]
    n = math.hypot(dlat, dlon * math.cos(math.radians(p[0]))) or 1e-9
    k = metres / 111000 / n
    return (p[0] + dlat * k, p[1] + dlon * k)


def route(a, b, camion):
    corps = {"locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
             "costing": "truck" if camion else "auto"}
    if camion:
        corps["costing_options"] = {"truck": camion}
    r = requests.post("https://valhalla1.openstreetmap.de/route", json=corps, headers=HDRS, timeout=60)
    return decode_polyline(r.json()["trip"]["legs"][0]["shape"])


def emprunte(trace, troncon, seuil_m=20):
    if not troncon:
        return False
    proches = sum(1 for t in troncon if any(dist_km(t, p) * 1000 < seuil_m for p in trace))
    return proches / len(troncon) >= 0.6


def etape2(arretes, n):
    # Seulement les interdictions d'accès : une limitation de vitesse PL ne doit pas faire dévier le camion
    candidats = [a for a in arretes if a["vise_pl"] and a["permanent"] and len(a["_troncon"]) >= 2
                 and "AccessRestriction" in a["types"]
                 and (a["poids_max_t"] != "" or a["hauteur_max_m"] != "")]
    print(f"\n=== ÉCART DIALOG / OPENSTREETMAP ===")
    print(f"{len(candidats)} interdictions permanentes avec limite chiffrée et tracé. Échantillon : {min(n, len(candidats))}")
    random.seed(42)
    echantillon = random.sample(candidats, min(n, len(candidats)))
    resultats, bilan = [], Counter()
    for a in echantillon:
        pts = a["_troncon"]   # le plus long tronçon continu de l'arrêté
        depart = decaler(pts[0], pts[1], 150)
        arrivee = decaler(pts[-1], pts[-2], 150)
        camion = {"weight": 19.0, "height": 3.5, "length": 12.0}
        if a["poids_max_t"] != "":
            camion["weight"] = min(44.0, max(a["poids_max_t"] * 1.5, a["poids_max_t"] + 5))
        if a["hauteur_max_m"] != "":
            camion["height"] = a["hauteur_max_m"] + 0.3
        try:
            voiture_ok = emprunte(route(depart, arrivee, None), pts)
            time.sleep(1.2)
            camion_passe = emprunte(route(depart, arrivee, camion), pts) if voiture_ok else None
            time.sleep(1.2)
        except Exception as e:
            verdict = f"ERREUR ({str(e)[:40]})"
        else:
            if not voiture_ok:
                verdict = "NON_CONCLUANT"     # même une voiture ne passe pas par là
            elif camion_passe:
                verdict = "OSM_IGNORE"        # le camion passe malgré l'arrêté
            else:
                verdict = "OSM_CONNAIT"       # le camion évite le tronçon
        bilan[verdict.split(" ")[0]] += 1
        resultats.append({"id": a["id"], "autorite": a["autorite"], "description": a["description"][:120],
                          "poids_max_t": a["poids_max_t"], "hauteur_max_m": a["hauteur_max_m"],
                          "lat": a["lat"], "lon": a["lon"], "verdict": verdict})
        print(f"   {verdict:14} | {str(a['poids_max_t']):>5} t | {str(a['hauteur_max_m']):>5} m | {a['autorite'][:30]:30} | {a['description'][:50]}")

    with open("dialog_ecart_osm.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(resultats[0]), delimiter=";")
        w.writeheader()
        w.writerows(resultats)
    testes = bilan["OSM_CONNAIT"] + bilan["OSM_IGNORE"]
    print(f"\nBilan : {dict(bilan)}")
    if testes:
        print(f"Sur {testes} cas concluants, OSM ignore {bilan['OSM_IGNORE']} interdiction(s) "
              f"({bilan['OSM_IGNORE'] / testes:.0%}).")
    print("Détail : dialog_ecart_osm.csv")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--croiser", type=int, default=0, help="nombre d'interdictions à tester contre Valhalla")
    args = p.parse_args()
    arretes = etape1()
    if arretes and args.croiser:
        etape2(arretes, args.croiser)
