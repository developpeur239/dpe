"""
test_gps_pl.py — Les moteurs d'itinéraire libres évitent-ils une interdiction poids lourds VÉRIFIÉE ?

Vérité terrain (sources officielles : Bison Futé, réponse ministérielle à l'Assemblée nationale) :
  Tunnel sous Fourvière (Lyon) interdit
    - aux poids lourds de plus de 7,5 t (sauf desserte locale)
    - aux transports de matières dangereuses (catégorie E)
    - aux véhicules au-dessus de la hauteur limite (4,30 m depuis les réparations)

Le test : un trajet nord -> sud de Lyon (A6 Limonest -> A7 Pierre-Bénite) qui passe
naturellement par le tunnel en voiture. On fait varier le véhicule et on regarde
si l'itinéraire passe par le tunnel alors qu'il ne devrait pas.

Moteurs testés :
  - Valhalla (serveur public de démonstration, sans clé)
  - openrouteservice, profil driving-hgv (optionnel : clé gratuite dans la variable ORS_KEY)

Usage :
    pip install requests
    python test_gps_pl.py
    ORS_KEY=ta_cle python test_gps_pl.py     # pour ajouter openrouteservice
"""

import os, math, time, requests

DEPART = (45.8370, 4.7710)   # A6, Limonest (nord de Lyon)
ARRIVEE = (45.7000, 4.8240)  # A7, Pierre-Bénite (sud de Lyon)
HDRS = {"User-Agent": "test-gps-pl/1.0 (etude de marche)"}
BALISES = ("maxweight", "maxheight", "hgv", "hazmat", "hazmat:E", "access", "goods")

# (nom, réglages véhicule, le tunnel est-il AUTORISÉ ?)
CAS = [
    ("Voiture (témoin)",                 None,                                                   True),
    ("Porteur 7 t, sans danger",         dict(weight=7.0, height=3.5, length=8.0, hazmat=False),   True),
    ("Porteur 7 t, matières dangereuses", dict(weight=7.0, height=3.5, length=8.0, hazmat=True),   False),
    ("Semi 40 t",                        dict(weight=40.0, height=4.0, length=16.5, hazmat=False), False),
    ("Semi 40 t, 4,40 m de haut",        dict(weight=40.0, height=4.4, length=16.5, hazmat=False), False),
]


def est_fourviere(tags):
    # Dans OSM, "name" vaut "Autoroute du Soleil" : le nom du tunnel est dans "tunnel:name"
    return "fourvi" in (tags.get("tunnel:name", "") + " " + tags.get("name", "")).lower()


def tunnel_overpass():
    q = """[out:json][timeout:60];
    (way(45.73,4.79,45.79,4.84)["tunnel"="yes"]["tunnel:name"~"Fourvi",i];
     way(45.73,4.79,45.79,4.84)["tunnel"="yes"]["name"~"Fourvi",i];);
    out tags geom;"""
    r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, headers=HDRS, timeout=90)
    return [(w.get("tags", {}), [(g["lat"], g["lon"]) for g in w.get("geometry", [])])
            for w in r.json().get("elements", [])]


def tunnel_api_osm():
    """Repli si Overpass est injoignable : API OSM sur une petite zone autour du tunnel."""
    r = requests.get("https://api.openstreetmap.org/api/0.6/map.json",
                     params={"bbox": "4.8025,45.7520,4.8205,45.7636"}, headers=HDRS, timeout=90)
    elements = r.json()["elements"]
    noeuds = {e["id"]: (e["lat"], e["lon"]) for e in elements if e["type"] == "node"}
    return [(e["tags"], [noeuds[n] for n in e["nodes"] if n in noeuds])
            for e in elements
            if e["type"] == "way" and e.get("tags", {}).get("tunnel") == "yes" and est_fourviere(e["tags"])]


def geometrie_tunnel():
    """Récupère dans OpenStreetMap le tracé du tunnel et ses balises de restriction."""
    try:
        ways = tunnel_overpass()
    except Exception as e:
        print(f"   Overpass injoignable ({type(e).__name__}), repli sur l'API OSM")
        ways = tunnel_api_osm()
    points, balises = [], {}
    for tags, geom in ways:
        points += geom
        for k in BALISES:
            if k in tags:
                balises.setdefault(k, set()).add(tags[k])
    return points, balises, len(ways)


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


def dist_m(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = dl * math.cos((p1 + p2) / 2)
    return R * math.hypot(x, p2 - p1)


def passe_par_tunnel(trace, tunnel, seuil=25):
    """Vrai si au moins 3 points de l'itinéraire tombent à moins de `seuil` m du tracé du tunnel."""
    proches = sum(1 for p in trace if any(dist_m(p, t) < seuil for t in tunnel))
    return proches >= 3


def valhalla(vehicule):
    corps = {
        "locations": [{"lat": DEPART[0], "lon": DEPART[1]}, {"lat": ARRIVEE[0], "lon": ARRIVEE[1]}],
        "costing": "auto" if vehicule is None else "truck",
        "directions_options": {"units": "km", "language": "fr-FR"},
    }
    if vehicule:
        corps["costing_options"] = {"truck": vehicule}
    r = requests.post("https://valhalla1.openstreetmap.de/route", json=corps, headers=HDRS, timeout=60)
    trip = r.json()["trip"]
    leg = trip["legs"][0]
    noms = {n for m in leg["maneuvers"] for n in m.get("street_names", [])}
    return decode_polyline(leg["shape"], 6), trip["summary"]["length"], noms


def ors(vehicule, cle):
    profil = "driving-car" if vehicule is None else "driving-hgv"
    corps = {"coordinates": [[DEPART[1], DEPART[0]], [ARRIVEE[1], ARRIVEE[0]]]}
    if vehicule:
        corps["options"] = {"vehicle_type": "hgv", "profile_params": {"restrictions": {
            "weight": vehicule["weight"], "height": vehicule["height"],
            "length": vehicule["length"], "hazmat": vehicule["hazmat"]}}}
    r = requests.post(f"https://api.openrouteservice.org/v2/directions/{profil}/geojson",
                      json=corps, headers={**HDRS, "Authorization": cle}, timeout=60)
    f = r.json()["features"][0]
    trace = [(lat, lon) for lon, lat in f["geometry"]["coordinates"]]
    return trace, f["properties"]["summary"]["distance"] / 1000, set()


def main():
    print("1) Ce que dit OpenStreetMap sur le tunnel sous Fourvière")
    tunnel, balises, n = geometrie_tunnel()
    print(f"   {n} tronçons trouvés, {len(tunnel)} points de tracé")
    for k, v in balises.items():
        print(f"   {k} = {', '.join(sorted(v))}")
    manquantes = [k for k in ("maxweight", "hazmat", "maxheight") if k not in balises and "hgv" not in balises]
    if manquantes:
        print(f"   ATTENTION, balises absentes : {', '.join(manquantes)}")
    if not tunnel:
        print("   Tunnel introuvable dans OSM : test impossible.")
        return

    moteurs = [("Valhalla", valhalla)]
    if os.environ.get("ORS_KEY"):
        moteurs.append(("openrouteservice", lambda v: ors(v, os.environ["ORS_KEY"])))
    else:
        print("\n   (openrouteservice ignoré : pas de variable ORS_KEY)")

    print("\n2) Itinéraires Lyon nord -> Lyon sud")
    score = {}
    for nom_moteur, fn in moteurs:
        print(f"\n   --- {nom_moteur} ---")
        ok = 0
        for nom_cas, vehicule, autorise in CAS:
            try:
                trace, km, noms = fn(vehicule)
            except Exception as e:
                print(f"   {nom_cas:36} ERREUR : {e}")
                continue
            dedans = passe_par_tunnel(trace, tunnel) or any("fourvi" in n.lower() for n in noms)
            correct = (dedans == autorise) if autorise is False else True
            # Pour les cas autorisés, passer ou non par le tunnel est acceptable (pas d'erreur de sécurité)
            verdict = "OK" if correct else "ERREUR : passe par un tunnel interdit"
            if autorise is False:
                ok += correct
            print(f"   {nom_cas:36} {km:6.1f} km | tunnel : {'OUI' if dedans else 'non':3} | {verdict}")
            time.sleep(1.5)
        nb_interdits = sum(1 for c in CAS if c[2] is False)
        score[nom_moteur] = (ok, nb_interdits)

    print("\n3) Bilan")
    for m, (ok, total) in score.items():
        print(f"   {m} : {ok}/{total} cas interdits correctement évités")


if __name__ == "__main__":
    main()
