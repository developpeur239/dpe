# -*- coding: utf-8 -*-
"""
agents_niches.py — Deux agents autonomes qui cherchent 10 idées de business
=========================================================================

AGENT 1, LE CHERCHEUR
    Lit nos données (classement Google Play + avis) et cherche sur le web.
    Propose des idées. Chaque idée DOIT être accompagnée de preuves.

AGENT 2, L'AVOCAT DU DIABLE
    Reçoit chaque idée et essaie de la tuer : concurrents gratuits,
    marché saturé, vente impossible en ligne, etc. Il note l'idée.

LE JUGE (du code Python, pas une IA)
    Vérifie chaque preuve AVANT de la croire :
      - une citation d'avis doit exister mot pour mot dans nos fichiers ;
      - un lien doit venir d'une vraie recherche web faite pendant la session.
    Une idée sans preuve vérifiée est rejetée. Les agents ne peuvent donc pas
    inventer : c'est la règle « on ne suppose rien » codée dans la machine.

Les tours continuent jusqu'à 10 idées survivantes, ou jusqu'au budget maximum.

UTILISATION
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."        (Windows : set ANTHROPIC_API_KEY=...)
    Placer dans le même dossier : classement_play.csv, verbatims_play.json
    (et, si vous les avez, les fichiers *verbatims*.md)

    python agents_niches.py --test     vérifie les données, sans rien dépenser
    python agents_niches.py            lance les agents
    python agents_niches.py --reprise  reprend là où le dernier lancement s'est arrêté

RÉSULTATS
    rapport_10_idees.md   le rapport final, lisible
    etat_agents.json      tout le détail (idées gardées, tuées, preuves)
    journal_agents.log    ce que les agents ont fait, étape par étape
"""

import argparse
import csv
import glob
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# RÉGLAGES
# ---------------------------------------------------------------------------
MODELE = os.environ.get("MODELE_AGENTS", "claude-sonnet-5-5")
OBJECTIF_IDEES = 10
MAX_TOURS = 6                 # nombre maximum d'allers-retours chercheur -> avocat
MAX_RECHERCHES_WEB = 100      # garde-fou de coût : recherches web au total
MAX_APPELS_API = 80           # garde-fou de coût : appels au modèle au total
RECHERCHES_PAR_APPEL = 8      # recherches web autorisées par appel d'agent
MAX_NOTES_APP = 50000         # on écarte les grosses apps grand public
NOTE_MINIMUM = 6.0            # score moyen minimum (sur 10) pour garder une idée

FICHIER_ETAT = "etat_agents.json"
FICHIER_RAPPORT = "rapport_10_idees.md"
FICHIER_JOURNAL = "journal_agents.log"

CRITERES = """
CRITÈRES OBLIGATOIRES (une idée qui en rate un seul est éliminée) :
1. Marché : la France, clients francophones.
2. 100 % en ligne : trouver les clients, vendre et livrer sans appel téléphonique
   ni rendez-vous physique. Pas de prospection par téléphone.
3. Faisable par UNE personne seule : un premier produit testable en moins de 4 semaines.
4. Des gens PAIENT DÉJÀ pour régler ce problème (prix réel observé).
5. Des plaintes RÉCENTES (moins de 18 mois) contre les solutions existantes.
6. Chemin crédible vers 10 000 €/mois (exemple : 500 clients à 20 €/mois).
7. Interdits : place de marché à effet de réseau à reconstruire, matériel physique,
   ventes longues (collectivités, écoles, grands comptes), activité réglementée
   exigeant un diplôme que l'on n'a pas.
"""

DEJA_ETUDIE = """
IDÉES DÉJÀ ÉTUDIÉES ET ÉLIMINÉES (ne pas les reproposer, même reformulées) :
- App de devis/factures mobile pour artisans : Abby et Tiime la donnent gratuitement.
- Classeur d'attestations de sous-traitants : e-Attestations, Provigis, Aprovall,
  effet de réseau ; le payeur est le donneur d'ordre.
- Dossiers de subvention pour associations : Cerfa gratuit sur Le Compte Asso,
  outils IA existants, aucune preuve de paiement.
- Plateforme de facturation électronique : 127 plateformes agréées.
- App GPS poids lourds complète : trop lourd en solo face à Michelin, Garmin, Sygic.
- Gestion locative « LouerSansStress » : déjà en cours de test, hors du périmètre.
DÉJÀ ÉCARTÉES DU CLASSEMENT : Crèche Connect, e-CPS, Klassly, Stuart, Allocab, Yper,
SignNow (face à Yousign/DocuSign), Equisense, AtClub, Sportpartner.
"""

# ---------------------------------------------------------------------------
# JOURNAL
# ---------------------------------------------------------------------------
def journal(msg):
    ligne = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(ligne, flush=True)
    with open(FICHIER_JOURNAL, "a", encoding="utf-8") as f:
        f.write(ligne + "\n")


# ---------------------------------------------------------------------------
# DONNÉES
# ---------------------------------------------------------------------------
def nombre(x, defaut=0.0):
    try:
        return float(str(x).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return defaut


def charger_classement(chemin="classement_play.csv"):
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        lignes = list(csv.DictReader(f, delimiter=";"))
    gardees = []
    for l in lignes:
        if l.get("statut_flux") not in ("OK", "CACHE"):
            continue
        if nombre(l.get("nb_notes")) > MAX_NOTES_APP:
            continue
        paie = l.get("achats_integres") == "True" or l.get("app_payante") == "True"
        if not paie:
            continue
        gardees.append(l)
    gardees.sort(key=lambda l: nombre(l.get("score")), reverse=True)
    return lignes, gardees


def charger_verbatims(chemin="verbatims_play.json"):
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def normaliser_texte(t):
    t = unicodedata.normalize("NFKC", t or "").lower()
    t = t.replace("’", "'").replace("«", '"').replace("»", '"')
    t = re.sub(r"[\"“”]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def construire_corpus(verbatims):
    """Tout le texte d'avis que nous possédons, pour vérifier les citations."""
    morceaux = []
    for app in verbatims:
        for a in app.get("avis", []):
            morceaux.append(a.get("texte", ""))
    for md in glob.glob("*verbatims*.md"):
        with open(md, encoding="utf-8") as f:
            morceaux.append(f.read())
    return normaliser_texte("\n".join(morceaux))


def dossier_donnees(gardees, verbatims, nb_apps=45, avis_par_app=3):
    """Résumé compact des données, donné au chercheur."""
    par_app = {v["app"]: v for v in verbatims}
    blocs = []
    for l in gardees[:nb_apps]:
        bloc = (f"- {l['app']} ({l.get('editeur','')}, {l.get('genre','')}) | "
                f"{l.get('nb_notes')} notes | {l.get('pct_neg_recents')} % négatifs récents | "
                f"prix : {l.get('prix_achats') or 'payante'} | MAJ {l.get('derniere_maj')} | "
                f"mot-clé : {l.get('termes')}")
        v = par_app.get(l["app"])
        if v:
            avis = sorted(v.get("avis", []), key=lambda a: -a.get("votes_utiles", 0))
            for a in [a for a in avis if a.get("note", 5) <= 2][:avis_par_app]:
                txt = a.get("texte", "").replace("\n", " ")[:300]
                bloc += f"\n    avis {a.get('note')}★ {a.get('date','')[:10]} ({a.get('votes_utiles',0)} votes) : {txt}"
        blocs.append(bloc)
    return "\n".join(blocs)


# ---------------------------------------------------------------------------
# LE JUGE : vérification des preuves (aucune IA ici)
# ---------------------------------------------------------------------------
def cle_url(u):
    try:
        p = urlparse(u.strip())
        hote = p.netloc.lower().removeprefix("www.")
        return hote + p.path.rstrip("/").lower()
    except Exception:
        return ""


def verifier_preuve(preuve, corpus, urls_vues):
    """Renvoie (valide: bool, raison: str)."""
    sorte = (preuve.get("type") or "").lower()
    if sorte == "avis":
        citation = normaliser_texte(preuve.get("citation", ""))
        if len(citation) < 25:
            return False, "citation trop courte pour être vérifiée"
        if citation in corpus:
            return True, "citation trouvée mot pour mot dans nos avis"
        return False, "citation introuvable dans nos fichiers d'avis"
    if sorte == "web":
        k = cle_url(preuve.get("url", ""))
        if not k:
            return False, "lien absent"
        if k in urls_vues:
            return True, "lien issu d'une vraie recherche web"
        # tolérance : même page avec une petite variation de chemin
        if any(k.startswith(v) or v.startswith(k) for v in urls_vues if len(v) > 15):
            return True, "lien issu d'une vraie recherche web (variante)"
        return False, "lien jamais vu dans les recherches : possiblement inventé"
    return False, f"type de preuve inconnu : {sorte!r}"


def trier_preuves(preuves, corpus, urls_vues):
    ok, rejetees = [], []
    for p in preuves or []:
        valide, raison = verifier_preuve(p, corpus, urls_vues)
        p = dict(p, verification=raison)
        (ok if valide else rejetees).append(p)
    return ok, rejetees


# ---------------------------------------------------------------------------
# APPEL D'UN AGENT
# ---------------------------------------------------------------------------
class Budget:
    def __init__(self, etat):
        self.etat = etat
        etat.setdefault("compteurs", {"appels": 0, "recherches": 0,
                                      "jetons_entree": 0, "jetons_sortie": 0})

    @property
    def c(self):
        return self.etat["compteurs"]

    def epuise(self):
        return (self.c["appels"] >= MAX_APPELS_API
                or self.c["recherches"] >= MAX_RECHERCHES_WEB)


def deja_fait(etat, max_pages=150):
    """Rappel donné aux agents : ne refaire ni une recherche ni une page déjà vue."""
    req = etat.get("requetes", [])
    pages = etat.get("urls_vues", [])[-max_pages:]
    if not req and not pages:
        return ""
    return ("\n\nRECHERCHES DÉJÀ FAITES pendant cette session (interdit de les refaire, même reformulées) :\n"
            + ("\n".join(f"- {q}" for q in req) or "aucune")
            + "\n\nPAGES DÉJÀ CONSULTÉES (ne pas les rechercher à nouveau ; tu peux les citer comme preuve) :\n"
            + ("\n".join(f"- {u}" for u in pages) or "aucune"))


def memoriser(etat, urls, requetes):
    vues = etat.setdefault("urls_vues", [])
    for u in sorted(urls):
        if u and u not in vues:
            vues.append(u)
    faites = etat.setdefault("requetes", [])
    for q in requetes:
        if q and q not in faites:
            faites.append(q)


def appeler_agent(client, budget, systeme, consigne, max_recherches=RECHERCHES_PAR_APPEL):
    """Appelle un agent avec la recherche web. Renvoie (texte, urls_vues, requetes)."""
    consigne += deja_fait(budget.etat)
    messages = [{"role": "user", "content": consigne}]
    outils = [{"type": "web_search_20250305", "name": "web_search",
               "max_uses": max_recherches, "user_location": {
                   "type": "approximate", "country": "FR", "timezone": "Europe/Paris"}}]
    textes, urls, requetes = [], set(), []
    for _ in range(4):  # relances si l'API met la réponse en pause
        if budget.epuise():
            raise RuntimeError("budget épuisé")
        for essai in range(5):
            try:
                # 32 000 : la réflexion du modèle + 8 idées en JSON dépassaient 8 000 (JSON tronqué).
                # À cette taille, le SDK exige le streaming.
                with client.messages.stream(model=MODELE, max_tokens=32000, system=systeme,
                                            messages=messages, tools=outils) as flux:
                    rep = flux.get_final_message()
                break
            except Exception as e:
                attente = 10 * (essai + 1)
                journal(f"   erreur API ({type(e).__name__}: {e}), nouvel essai dans {attente} s")
                time.sleep(attente)
        else:
            raise RuntimeError("l'API ne répond plus")

        budget.c["appels"] += 1
        u = rep.usage
        budget.c["jetons_entree"] += getattr(u, "input_tokens", 0) or 0
        budget.c["jetons_sortie"] += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        budget.c["recherches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

        for bloc in rep.content:
            d = bloc.model_dump() if hasattr(bloc, "model_dump") else dict(bloc)
            if d.get("type") == "text":
                textes.append(d.get("text", ""))
            elif d.get("type") == "server_tool_use" and d.get("name") == "web_search":
                q = (d.get("input") or {}).get("query", "")
                if q:
                    requetes.append(q)
            elif d.get("type") == "web_search_tool_result":
                contenu = d.get("content")
                if isinstance(contenu, list):
                    for r in contenu:
                        if r.get("url"):
                            urls.add(cle_url(r["url"]))
        if rep.stop_reason not in ("end_turn", "pause_turn"):
            journal(f"   ! réponse arrêtée par : {rep.stop_reason}")
        if rep.stop_reason == "pause_turn":
            messages = [{"role": "user", "content": consigne},
                        {"role": "assistant", "content": rep.content}]
            continue
        break
    memoriser(budget.etat, urls, requetes)
    return "\n".join(textes), urls, requetes


def extraire_json(texte):
    """Récupère le dernier bloc JSON de la réponse."""
    m = re.findall(r"```json\s*(.*?)```", texte, re.S)
    candidats = m[::-1] if m else []
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut != -1 and fin > debut:
        candidats.append(texte[debut:fin + 1])
    for c in candidats:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# AGENT 1 : LE CHERCHEUR
# ---------------------------------------------------------------------------
SYSTEME_CHERCHEUR = f"""Tu es un chercheur d'opportunités business pour le marché français.
Règle absolue : on ne suppose rien. Chaque affirmation repose sur une preuve.
Tu suis cette méthode, dans l'ordre : 1) le problème, 2) les solutions existantes,
3) les gens paient-ils (prix réel), 4) leurs plaintes, 5) comment faire mieux.
{CRITERES}
{DEJA_ETUDIE}
FORMAT DES PREUVES (un code les vérifie ensuite automatiquement) :
- {{"type": "avis", "citation": "..."}} : citation COPIÉE MOT POUR MOT d'un avis du dossier
  (au moins 25 caractères, sans rien changer). Une citation modifiée sera rejetée.
- {{"type": "web", "url": "...", "ce_que_ca_prouve": "..."}} : uniquement un lien obtenu par
  TES recherches web de cette session. Un lien de mémoire sera rejeté.
Chaque idée doit avoir au moins une preuve de DOULEUR et au moins une preuve de PAIEMENT.
Si tu manques de preuves, propose moins d'idées plutôt que d'en inventer."""


def consigne_chercheur(dossier, nb, tuees, gardees):
    deja = "\n".join(f"- {i['nom']} ({i.get('raison_mort','')})" for i in tuees) or "aucune"
    ok = "\n".join(f"- {i['nom']}" for i in gardees) or "aucune"
    return f"""DONNÉES GOOGLE PLAY FRANCE (apps payantes, ≤ {MAX_NOTES_APP} notes, triées par score de douleur) :
{dossier}

Idées déjà GARDÉES (ne pas les répéter) :
{ok}

Idées déjà TUÉES ou REJETÉES pendant cette session (ne pas les reproposer, même reformulées ;
apprends de leurs défauts) :
{deja}

TÂCHE : propose {nb} nouvelles idées. Pars des données ci-dessus, puis utilise la recherche
web pour trouver des preuves françaises (forums, Trustpilot, pages de prix, articles).
Tu peux aussi trouver des idées hors du dossier si les preuves web sont solides.

Termine ta réponse par un bloc ```json au format exact :
{{"idees": [{{
  "nom": "nom court",
  "probleme": "le problème, en une phrase",
  "client": "qui paie exactement",
  "solutions_existantes": ["nom (prix)"],
  "prix_observe": "prix réel constaté, avec la source",
  "plaintes": "ce qui ne va pas dans l'existant",
  "amelioration": "comment entrer en force",
  "canal_en_ligne": "où trouver les clients sans téléphone",
  "calcul_10k": "exemple : 400 clients x 25 €/mois",
  "preuves_douleur": [ ... ],
  "preuves_paiement": [ ... ]
}}]}}"""


# ---------------------------------------------------------------------------
# AGENT 2 : L'AVOCAT DU DIABLE
# ---------------------------------------------------------------------------
SYSTEME_AVOCAT = f"""Tu es l'avocat du diable. Ton travail : TUER les mauvaises idées de business
avant que quelqu'un y perde des mois. Tu n'es ni gentil ni méchant : tu cherches la vérité.
Règle absolue : on ne suppose rien. Pour tuer une idée, il faut une preuve trouvée par
recherche web pendant cette session (un lien que tu as vraiment obtenu).
{CRITERES}
Cherche en priorité, en France :
- des concurrents GRATUITS ou très bon marché qui font déjà la même chose ;
- un marché saturé (beaucoup d'acteurs bien notés) ;
- un obstacle à la vente 100 % en ligne (clients injoignables en ligne) ;
- une raison pour laquelle les gens ne paieraient pas (habitude du gratuit, budget nul) ;
- un obstacle légal ou technique trop lourd pour une personne seule.
Si après recherche tu ne trouves rien de mortel, garde l'idée : c'est une bonne nouvelle."""


def consigne_avocat(idee):
    return f"""IDÉE À ATTAQUER :
{json.dumps(idee, ensure_ascii=False, indent=2)}

Fais tes recherches, puis termine par un bloc ```json au format exact :
{{
  "verdict": "GARDER" ou "TUER",
  "raison": "en deux phrases maximum",
  "concurrents_trouves": [{{"nom": "...", "prix": "...", "url": "..."}}],
  "preuves_attaque": [{{"type": "web", "url": "...", "ce_que_ca_prouve": "..."}}],
  "notes": {{
    "douleur": 0-10,
    "paiement": 0-10,
    "faisabilite_solo": 0-10,
    "vente_en_ligne": 0-10,
    "concurrence": 0-10  (10 = peu de concurrence sérieuse)
  }},
  "plus_grand_risque": "...",
  "test_vente_14_jours": "le test le moins cher pour savoir si des gens paient, 100 % en ligne"
}}"""


# ---------------------------------------------------------------------------
# BOUCLE PRINCIPALE
# ---------------------------------------------------------------------------
def charger_etat(reprise):
    if reprise and os.path.exists(FICHIER_ETAT):
        with open(FICHIER_ETAT, encoding="utf-8") as f:
            return json.load(f)
    return {"debut": datetime.now().isoformat(timespec="seconds"), "tour": 0,
            "gardees": [], "tuees": [], "sans_preuve": []}


def sauver_etat(etat):
    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=2)


def cle_idee(nom):
    t = unicodedata.normalize("NFKD", nom or "").encode("ascii", "ignore").decode().lower()
    mots = re.findall(r"[a-z0-9]+", t)
    return " ".join(sorted(m for m in mots if len(m) > 2))


def est_doublon(idee, etat):
    k = cle_idee(idee.get("nom"))
    deja = {cle_idee(i.get("nom")) for i in etat["gardees"] + etat["tuees"] + etat["sans_preuve"]}
    return not k or k in deja


def examiner_idee(client, budget, idee, corpus, urls_chercheur, etat):
    nom = idee.get("nom", "sans nom")

    # 1) Le juge vérifie les preuves du chercheur
    douleur_ok, douleur_ko = trier_preuves(idee.get("preuves_douleur"), corpus, urls_chercheur)
    paie_ok, paie_ko = trier_preuves(idee.get("preuves_paiement"), corpus, urls_chercheur)
    idee["preuves_douleur"], idee["preuves_paiement"] = douleur_ok, paie_ok
    idee["preuves_rejetees"] = douleur_ko + paie_ko
    if not douleur_ok or not paie_ok:
        manque = "douleur" if not douleur_ok else "paiement"
        idee["raison_mort"] = f"aucune preuve vérifiée de {manque}"
        etat["sans_preuve"].append(idee)
        journal(f"   ✗ {nom} : rejetée par le juge ({idee['raison_mort']}, "
                f"{len(idee['preuves_rejetees'])} preuve(s) invalide(s))")
        return

    # 2) L'avocat du diable attaque
    journal(f"   → avocat du diable sur « {nom} »")
    texte, _, _ = appeler_agent(client, budget, SYSTEME_AVOCAT, consigne_avocat(idee))
    urls_avocat = set(etat.get("urls_vues", []))
    avis = extraire_json(texte)
    if not avis:
        journal(f"   ! réponse illisible de l'avocat pour « {nom} », idée mise de côté")
        idee["raison_mort"] = "examen impossible (réponse illisible)"
        etat["sans_preuve"].append(idee)
        return

    attaque_ok, attaque_ko = trier_preuves(avis.get("preuves_attaque"), corpus, urls_avocat)
    avis["preuves_attaque"], avis["preuves_attaque_rejetees"] = attaque_ok, attaque_ko
    for c in avis.get("concurrents_trouves", []) or []:
        c["lien_verifie"] = bool(c.get("url")) and verifier_preuve(
            {"type": "web", "url": c["url"]}, corpus, urls_avocat)[0]

    notes = avis.get("notes") or {}
    valeurs = [nombre(v) for v in notes.values() if v is not None]
    score = round(sum(valeurs) / len(valeurs), 2) if valeurs else 0.0
    idee["examen"], idee["score"] = avis, score

    verdict = (avis.get("verdict") or "").upper()
    if verdict == "TUER" and not attaque_ok:
        # Pas le droit de tuer sans preuve : l'idée survit mais est signalée.
        verdict = "GARDER"
        avis["alerte"] = "l'avocat voulait tuer l'idée mais sans preuve vérifiée"

    if verdict == "TUER":
        idee["raison_mort"] = avis.get("raison", "")
        etat["tuees"].append(idee)
        journal(f"   ✗ {nom} : tuée ({idee['raison_mort'][:120]})")
    elif score < NOTE_MINIMUM:
        idee["raison_mort"] = f"score trop faible ({score}/10)"
        etat["tuees"].append(idee)
        journal(f"   ✗ {nom} : score {score}/10 sous le minimum de {NOTE_MINIMUM}")
    else:
        etat["gardees"].append(idee)
        journal(f"   ✓ {nom} : GARDÉE, score {score}/10")


def lancer(reprise):
    try:
        import anthropic
    except ImportError:
        sys.exit("Installez la bibliothèque : pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Clé manquante : définissez la variable ANTHROPIC_API_KEY.")

    client = anthropic.Anthropic()
    _, gardees_play = charger_classement()
    verbatims = charger_verbatims()
    corpus = construire_corpus(verbatims)
    dossier = dossier_donnees(gardees_play, verbatims)
    etat = charger_etat(reprise)
    budget = Budget(etat)
    journal(f"Démarrage — modèle {MODELE}, {len(gardees_play)} apps candidates, "
            f"{len(etat['gardees'])} idée(s) déjà gardée(s)")

    try:
        while len(etat["gardees"]) < OBJECTIF_IDEES and etat["tour"] < MAX_TOURS:
            etat["tour"] += 1
            manque = OBJECTIF_IDEES - len(etat["gardees"])
            a_proposer = min(manque + 3, 8)   # marge, car l'avocat en tuera
            journal(f"TOUR {etat['tour']} — le chercheur doit proposer {a_proposer} idées")
            texte, urls, _ = appeler_agent(
                client, budget, SYSTEME_CHERCHEUR,
                consigne_chercheur(dossier, a_proposer,
                                   etat["tuees"] + etat["sans_preuve"], etat["gardees"]),
                max_recherches=RECHERCHES_PAR_APPEL * 2)
            donnees = extraire_json(texte) or {}
            idees = donnees.get("idees") or []
            if not idees:
                with open(f"reponse_illisible_tour{etat['tour']}.txt", "w", encoding="utf-8") as f:
                    f.write(texte)
                journal(f"   ! aucune idée lisible : réponse brute dans reponse_illisible_tour{etat['tour']}.txt")
            journal(f"   {len(idees)} idée(s) reçue(s), {len(urls)} page(s) web consultée(s)")
            for idee in idees:
                if len(etat["gardees"]) >= OBJECTIF_IDEES:
                    break
                if est_doublon(idee, etat):
                    journal(f"   = {idee.get('nom', 'sans nom')} : déjà examinée, ignorée (aucun appel dépensé)")
                    continue
                examiner_idee(client, budget, idee, corpus, set(etat.get("urls_vues", [])), etat)
                sauver_etat(etat)
            sauver_etat(etat)
    except RuntimeError as e:
        journal(f"ARRÊT : {e}")
    finally:
        sauver_etat(etat)
        ecrire_rapport(etat)
        c = etat["compteurs"]
        journal(f"Fin — {len(etat['gardees'])} idée(s) gardée(s), {len(etat['tuees'])} tuée(s), "
                f"{len(etat['sans_preuve'])} sans preuve | {c['appels']} appels, "
                f"{c['recherches']} recherches web, {c['jetons_entree']:,} jetons lus, "
                f"{c['jetons_sortie']:,} jetons écrits")


# ---------------------------------------------------------------------------
# RAPPORT
# ---------------------------------------------------------------------------
def fmt_preuve(p):
    if p.get("type") == "avis":
        return f"  - Avis : « {p.get('citation','')[:250]} »"
    return f"  - [{p.get('ce_que_ca_prouve','source')}]({p.get('url','')})"


def ecrire_rapport(etat):
    g = sorted(etat["gardees"], key=lambda i: -i.get("score", 0))
    L = [f"# Les idées qui ont survécu ({len(g)}/{OBJECTIF_IDEES})", "",
         f"Généré le {datetime.now():%d/%m/%Y à %H:%M}. Chaque preuve ci-dessous a été "
         "vérifiée par le code : citation présente mot pour mot dans nos avis, "
         "ou lien obtenu par une vraie recherche web.", ""]
    for n, i in enumerate(g, 1):
        ex = i.get("examen", {})
        notes = ex.get("notes", {})
        L += [f"## {n}. {i.get('nom')} — {i.get('score')}/10", "",
              f"**Problème :** {i.get('probleme','')}", "",
              f"**Qui paie :** {i.get('client','')}", "",
              f"**Prix observé :** {i.get('prix_observe','')}", "",
              f"**Plaintes contre l'existant :** {i.get('plaintes','')}", "",
              f"**Comment faire mieux :** {i.get('amelioration','')}", "",
              f"**Trouver les clients en ligne :** {i.get('canal_en_ligne','')}", "",
              f"**Chemin vers 10 000 €/mois :** {i.get('calcul_10k','')}", "",
              "**Notes de l'avocat du diable :** " + ", ".join(f"{k} {v}/10" for k, v in notes.items()), "",
              f"**Plus grand risque :** {ex.get('plus_grand_risque','')}", "",
              f"**Test de vente en 14 jours :** {ex.get('test_vente_14_jours','')}", ""]
        if ex.get("alerte"):
            L += [f"> Attention : {ex['alerte']}.", ""]
        conc = ex.get("concurrents_trouves") or []
        if conc:
            L.append("**Concurrents trouvés :**")
            L += [f"  - {c.get('nom')} ({c.get('prix','prix inconnu')})"
                  + ("" if c.get("lien_verifie") else " — lien non vérifié") for c in conc]
            L.append("")
        L.append("**Preuves de douleur :**")
        L += [fmt_preuve(p) for p in i.get("preuves_douleur", [])]
        L.append("**Preuves de paiement :**")
        L += [fmt_preuve(p) for p in i.get("preuves_paiement", [])]
        L.append("")

    L += ["---", "", "# Idées tuées par l'avocat du diable", ""]
    L += [f"- **{i.get('nom')}** : {i.get('raison_mort','')}" for i in etat["tuees"]] or ["- aucune"]
    L += ["", "# Idées rejetées par le juge (preuves invérifiables)", ""]
    L += [f"- **{i.get('nom')}** : {i.get('raison_mort','')}" for i in etat["sans_preuve"]] or ["- aucune"]
    with open(FICHIER_RAPPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    journal(f"Rapport écrit : {FICHIER_RAPPORT}")


# ---------------------------------------------------------------------------
# MODE TEST (aucune dépense)
# ---------------------------------------------------------------------------
def tester():
    toutes, gardees = charger_classement()
    verbatims = charger_verbatims()
    corpus = construire_corpus(verbatims)
    dossier = dossier_donnees(gardees, verbatims)
    print(f"Classement : {len(toutes)} apps, {len(gardees)} retenues "
          f"(payantes, ≤ {MAX_NOTES_APP} notes)")
    print(f"Avis : {len(verbatims)} apps, corpus de {len(corpus):,} caractères "
          f"(+ {len(glob.glob('*verbatims*.md'))} fichier(s) .md)")
    print(f"Dossier envoyé au chercheur : {len(dossier):,} caractères\n")

    vrai = next(a["texte"] for v in verbatims for a in v.get("avis", []) if len(a.get("texte", "")) > 60)
    cas = [
        ("vraie citation", {"type": "avis", "citation": vrai[:80]}, True),
        ("citation inventée", {"type": "avis", "citation": "Cette application est une arnaque totale je veux être remboursé"}, False),
        ("lien vu en recherche", {"type": "web", "url": "https://www.trustpilot.com/review/exemple.fr"}, True),
        ("lien inventé", {"type": "web", "url": "https://www.site-invente.fr/prix"}, False),
    ]
    urls_vues = {cle_url("https://fr.trustpilot.com/review/exemple.fr/")} | {cle_url("https://www.trustpilot.com/review/exemple.fr")}
    erreurs = 0
    for nom, p, attendu in cas:
        ok, raison = verifier_preuve(p, corpus, urls_vues)
        statut = "OK " if ok == attendu else "ERREUR"
        erreurs += ok != attendu
        print(f"[{statut}] juge — {nom} : {'acceptée' if ok else 'rejetée'} ({raison})")
    print("\nDébut du dossier :\n" + dossier[:1500])
    print("\nTout est prêt." if not erreurs else f"\n{erreurs} test(s) en échec.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Deux agents qui cherchent 10 idées de business prouvées.")
    ap.add_argument("--test", action="store_true", help="vérifier les données sans appeler l'API")
    ap.add_argument("--reprise", action="store_true", help="reprendre le dernier lancement")
    args = ap.parse_args()
    tester() if args.test else lancer(args.reprise)
