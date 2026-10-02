"""
test_gps_commerciaux.py — Les GPS commerciaux connaissent-ils les interdictions que OSM ignore ?

Pour chaque tronçon interdit (issu de cas_osm.json, produit par verif_balises_osm.py) :
  1. trajet en VOITURE qui doit emprunter le tronçon (témoin, sinon le cas est non concluant)
  2. même trajet en CAMION au-dessus de la limite
  -> EVITE : le moteur respecte l'interdiction / PASSE : il l'ignore

Moteurs : Valhalla (sans clé), HERE (clé HERE_KEY), TomTom (clé TOMTOM_KEY).
Un moteur sans clé est simplement sauté.

Usage :
    pip install requests flexpolyline
    HERE_KEY=xxx TOMTOM_KEY=yyy python test_gps_commerciaux.py --max 30

Par défaut : tous les cas OSM_IGNORE (jusqu'à --max) + 5 cas OSM_CONNAIT comme témoins.
"""

import argparse, csv, json, math, os, random, time
from collections import Counter, defaultdict
import requests

HDRS = {"User-Agent": "test-gps-pl/1.0 (etude de marche)"}
SEUIL_M = 20


# ---------- géométrie ----------

def proj(p, ref):
    return ((p[1] - ref[1]) * 111320 * math.cos(math.radians(ref[0])), (p[0] - ref[0]) * 110540)


def dist_point_segment(p, a, b):
    A, B = proj(a, p), proj(b, p)
    dx, dy = B[0] - A[0], B[1] - A[1]
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, (-A[0] * dx - A[1] * dy) / L2))
    return math.hypot(A[0] + t * dx, A[1] + t * dy)


def dist_point_trace(p, trace):
    return min(dist_point_segment(p, trace[i], trace[i + 1]) for i in range(len(trace) - 1)) if len(trace) > 1 else 1e9


def densifier(ligne, pas_m=15):
    pts = [ligne[0]]
    for a, b in zip(ligne, ligne[1:]):
        n = max(1, int(math.hypot(*proj(b, a)) / pas_m))
        pts += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)]
    return pts


def emprunte(trace, troncon):
    """Vrai si au moins 60 % du tronçon (hors 10 % de chaque bout) est couvert par le trajet."""
    pts = densifier(troncon)
    coeur = pts[len(pts) // 10: len(pts) - len(pts) // 10] or pts
    proches = sum(1 for p in coeur if dist_point_trace(p, trace) < SEUIL_M)
    return proches / len(coeur) >= 0.6


def prolonger(p, q, metres):
    """Point à `metres` au-delà de p, dans la direction q -> p."""
    dx, dy = proj(p, q)
    n = math.hypot(dx, dy) or 1e-9
    return (p[0] + dy / n * metres / 110540, p[1] + dx / n * metres / (111320 * math.cos(math.radians(p[0]))))


# ---------- moteurs ----------

def valhalla(a, b, camion):
    corps = {"locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
             "costing": "truck" if camion else "auto"}
    if camion:
        corps["costing_options"] = {"truck": {"weight": camion["t"], "height": camion["h"], "length": camion["l"]}}
    r = requests.post("https://valhalla1.openstreetmap.de/route", json=corps, headers=HDRS, timeout=60)
    j = r.json()
    if "trip" not in j:
        return None, "AUCUN_ITINERAIRE"
    trace = []
    for leg in j["trip"]["legs"]:
        trace += decode6(leg["shape"])
    return trace, ""


def decode6(s):
    coords, idx, lat, lon = [], 0, 0, 0
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
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def here(a, b, camion, cle):
    import flexpolyline
    p = {"origin": f"{a[0]},{a[1]}", "destination": f"{b[0]},{b[1]}", "return": "polyline", "apikey": cle,
         "transportMode": "truck" if camion else "car"}
    if camion:
        p["vehicle[grossWeight]"] = int(camion["t"] * 1000)
        p["vehicle[height]"] = int(camion["h"] * 100)
        p["vehicle[length]"] = int(camion["l"] * 100)
    r = requests.get("https://router.hereapi.com/v8/routes", params=p, headers=HDRS, timeout=60)
    j = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"HERE {r.status_code} {str(j)[:100]}")
    if not j.get("routes"):
        return None, "AUCUN_ITINERAIRE " + ",".join(n.get("code", "") for n in j.get("notices", []))
    trace, notices = [], set()
    for s in j["routes"][0]["sections"]:
        trace += [(x[0], x[1]) for x in flexpolyline.decode(s["polyline"])]
        notices |= {n.get("code", "") for n in s.get("notices", [])}
    return trace, ",".join(sorted(notices))


def tomtom(a, b, camion, cle):
    p = {"key": cle, "travelMode": "truck" if camion else "car", "routeRepresentation": "polyline"}
    if camion:
        p.update({"vehicleWeight": int(camion["t"] * 1000), "vehicleHeight": camion["h"],
                  "vehicleLength": camion["l"], "vehicleCommercial": "true"})
    url = f"https://api.tomtom.com/routing/1/calculateRoute/{a[0]},{a[1]}:{b[0]},{b[1]}/json"
    r = requests.get(url, params=p, headers=HDRS, timeout=60)
    if r.status_code != 200:
        if r.status_code == 400 and "NO_ROUTE" in r.text.upper():
            return None, "AUCUN_ITINERAIRE"
        raise RuntimeError(f"TomTom {r.status_code} {r.text[:100]}")
    trace = [(pt["latitude"], pt["longitude"]) for leg in r.json()["routes"][0]["legs"] for pt in leg["points"]]
    return trace, ""


def tester(moteur, troncon, camion):
    """Essaie deux distances de prolongement pour obtenir un témoin voiture valide."""
    for metres in (150, 400):
        a = prolonger(troncon[0], troncon[1], metres)
        b = prolonger(troncon[-1], troncon[-2], metres)
        tv, _ = moteur(a, b, None)
        time.sleep(0.6)
        if tv and emprunte(tv, troncon):
            tc, note = moteur(a, b, camion)
            time.sleep(0.6)
            if tc is None:
                return "EVITE", note          # aucun itinéraire camion possible : restriction connue
            return ("PASSE" if emprunte(tc, troncon) else "EVITE"), note
    return "NON_CONCLUANT", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=30, help="nombre maximum de cas OSM_IGNORE testés")
    ap.add_argument("--temoins", type=int, default=5, help="cas OSM_CONNAIT ajoutés comme témoins")
    args = ap.parse_args()

    with open("cas_osm.json", encoding="utf-8") as f:
        tous = json.load(f)
    random.seed(7)
    ign = [c for c in tous if c["verdict"] == "OSM_IGNORE" and len(c["ligne"]) >= 2 and c["longueur_m"] >= 40]
    con = [c for c in tous if c["verdict"] == "OSM_CONNAIT" and len(c["ligne"]) >= 2 and c["longueur_m"] >= 40]
    cas = random.sample(ign, min(args.max, len(ign))) + random.sample(con, min(args.temoins, len(con)))

    moteurs = [("Valhalla", valhalla)]
    if os.environ.get("HERE_KEY"):
        moteurs.append(("HERE", lambda a, b, c: here(a, b, c, os.environ["HERE_KEY"])))
    if os.environ.get("TOMTOM_KEY"):
        moteurs.append(("TomTom", lambda a, b, c: tomtom(a, b, c, os.environ["TOMTOM_KEY"])))
    print(f"Moteurs : {', '.join(m for m, _ in moteurs)}  |  {len(cas)} cas "
          f"({min(args.max, len(ign))} ignorés par OSM + {min(args.temoins, len(con))} témoins)\n")

    lignes, bilan = [], defaultdict(Counter)
    for c in cas:
        troncon = [tuple(p) for p in c["ligne"]]
        lim_t, lim_h = c.get("poids_max_t"), c.get("hauteur_max_m")
        camion = {"t": min(44.0, max(lim_t * 1.5, lim_t + 5)) if lim_t else 19.0,
                  "h": (lim_h + 0.3) if lim_h else 3.8, "l": 12.0}
        ligne = {"id": c["id"], "autorite": c["autorite"], "osm": c["verdict"],
                 "poids_max_t": lim_t, "hauteur_max_m": lim_h,
                 "lat": round(troncon[len(troncon) // 2][0], 5), "lon": round(troncon[len(troncon) // 2][1], 5)}
        resume = []
        for nom, fn in moteurs:
            try:
                v, note = tester(fn, troncon, camion)
            except Exception as e:
                v, note = "ERREUR", str(e)[:60]
            ligne[nom] = v
            ligne[f"{nom}_note"] = note
            bilan[(c["verdict"], nom)][v] += 1
            resume.append(f"{nom}:{v}")
        lignes.append(ligne)
        print(f"  {c['verdict']:11} | {str(lim_t):>5} t | {c['autorite'][:28]:28} | {'  '.join(resume)}")

    print("\n=== BILAN (cas concluants uniquement) ===")
    for groupe in ("OSM_IGNORE", "OSM_CONNAIT"):
        print(f"\nInterdictions {'IGNORÉES' if groupe == 'OSM_IGNORE' else 'CONNUES (témoins)'} par OSM :")
        for nom, _ in moteurs:
            b = bilan[(groupe, nom)]
            n = b["EVITE"] + b["PASSE"]
            if n:
                print(f"  {nom:9} évite {b['EVITE']}/{n} ({b['EVITE'] / n:.0%})   "
                      f"[non concluant : {b['NON_CONCLUANT']}, erreur : {b['ERREUR']}]")
            else:
                print(f"  {nom:9} aucun cas concluant {dict(b)}")

    print("\nLecture : si HERE et TomTom PASSENT sur les cas ignorés par OSM, tout le marché ignore ces")
    print("arrêtés et DiaLog est un avantage. S'ils les ÉVITENT, les grands ont déjà la donnée.")
    print("Les témoins vérifient la méthode : Valhalla doit les éviter.")

    with open("gps_commerciaux.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0]), delimiter=";")
        w.writeheader()
        w.writerows(lignes)
    print("\nDétail : gps_commerciaux.csv")


if __name__ == "__main__":
    main()
