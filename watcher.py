#!/usr/bin/env python3
"""
Veille d'offres d'emploi.

À chaque passage :
1. ouvre chaque page de sources.yaml dans un navigateur sans écran ;
2. relève tous les liens et garde ceux qui n'existaient pas au passage précédent ;
3. demande à Claude lesquels sont des offres en ligne avec le profil ;
4. envoie une alerte Telegram pour chaque offre pertinente.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import yaml

RACINE = Path(__file__).resolve().parent
FICHIER_SOURCES = RACINE / "sources.yaml"
FICHIER_ETAT = RACINE / "state" / "seen.json"
FICHIER_PROFIL = RACINE / "profil.md"  # facultatif : utilisé seulement si le secret PROFIL est vide

MAX_LIENS_MEMORISES = 5000      # par source
MAX_URLS_EVALUEES = 30000       # toutes sources confondues
MAX_EN_ATTENTE = 500
TAILLE_LOT_CLAUDE = 40
ECHECS_AVANT_ALERTE = 3

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Paramètres d'URL qui ne servent qu'au suivi : on les retire pour ne pas
# prendre une même offre pour une nouvelle.
PARAMS_DE_SUIVI = re.compile(
    r"^(utm_.*|gclid|fbclid|mc_.*|trk.*|trackingid|refid|ref|src|source|"
    r"sessionid|jsessionid|_ga|position|pagenum|lipi)$",
    re.IGNORECASE,
)

CONSIGNE = """Tu es un recruteur expérimenté en asset management, banque privée et gestion de fortune.
Tu tries des liens relevés sur des pages carrières et des sites d'emploi pour une candidate dont le profil et les critères suivent.

Pour chaque lien :
- décide s'il mène à UNE offre d'emploi précise (et non à un menu, une catégorie, une page de recherche, un article, une page de connexion ou de cookies) ;
- si c'est une offre, note de 0 à 100 son adéquation avec le profil et les critères : type de poste, séniorité, lieu, secteur, langues, exclusions.

Sois exigeant : 80 et plus = à postuler sans attendre ; 60 à 79 = intéressant ; moins de 60 = hors cible.
Si le lieu n'apparaît pas dans le texte du lien, appuie-toi sur le contexte de la source.

Réponds UNIQUEMENT avec un tableau JSON, sans aucun texte autour. N'inclus que les liens qui sont des offres d'emploi :
[{"i": 3, "score": 85, "titre": "...", "entreprise": "...", "lieu": "...", "raison": "une phrase courte en français"}]
Si aucun lien n'est une offre, réponds []."""

JS_LIENS = """els => els.map(e => ({
  texte: (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim(),
  href: e.href
}))"""


# ───────────────────────── utilitaires ─────────────────────────

def maintenant() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def normaliser_url(url: str) -> str:
    """Rend une URL comparable d'un passage à l'autre (sans paramètres de suivi)."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    requete = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not PARAMS_DE_SUIVI.match(k)
    )
    chemin = re.sub(r";jsessionid=[^/?#]*", "", p.path, flags=re.IGNORECASE).rstrip("/") or "/"
    # On ne garde le fragment (#...) que s'il sert de route (ex. #/job/123).
    fragment = p.fragment if "/" in p.fragment else ""
    return urlunparse((p.scheme, p.netloc.lower(), chemin, "", urlencode(requete), fragment))


def nettoyer_texte(texte: str) -> str:
    return re.sub(r"\s+", " ", texte or "").strip()[:300]


def lire_json(texte: str) -> list:
    """Lit la réponse de Claude, même si elle est entourée de texte ou tronquée."""
    texte = re.sub(r"```(?:json)?", "", texte or "").strip()
    debut, fin = texte.find("["), texte.rfind("]")
    if debut != -1 and fin > debut:
        try:
            donnees = json.loads(texte[debut:fin + 1])
            if isinstance(donnees, list):
                return [d for d in donnees if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass
    resultats = []
    for bloc in re.findall(r"\{[^{}]*\}", texte):
        try:
            d = json.loads(bloc)
            if isinstance(d, dict):
                resultats.append(d)
        except json.JSONDecodeError:
            continue
    return resultats


# ───────────────────────── état ─────────────────────────

def charger_etat() -> dict:
    etat = {}
    if FICHIER_ETAT.exists():
        try:
            etat = json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            etat = {}
    etat.setdefault("sources", {})
    etat.setdefault("evaluees", [])
    etat.setdefault("en_attente", [])
    return etat


def sauvegarder_etat(etat: dict) -> None:
    etat["evaluees"] = etat["evaluees"][-MAX_URLS_EVALUEES:]
    etat["en_attente"] = etat["en_attente"][-MAX_EN_ATTENTE:]
    FICHIER_ETAT.parent.mkdir(parents=True, exist_ok=True)
    FICHIER_ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────── Telegram ─────────────────────────

def envoyer_telegram(texte: str) -> bool:
    jeton = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not jeton or not chat:
        print("[Telegram] TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant. Message non envoyé :\n" + texte)
        return False
    ok = True
    for i in range(0, len(texte), 4000):
        morceau = texte[i:i + 4000]
        for _ in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{jeton}/sendMessage",
                    json={"chat_id": chat, "text": morceau, "disable_web_page_preview": True},
                    timeout=30,
                )
            except requests.RequestException as err:
                print(f"[Telegram] erreur réseau : {err}")
                time.sleep(3)
                continue
            if r.status_code == 429:
                attente = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(int(attente) + 1)
                continue
            if not r.ok:
                print(f"[Telegram] erreur {r.status_code} : {r.text[:300]}")
                ok = False
            break
        time.sleep(1.1)  # Telegram limite à environ un message par seconde et par conversation
    return ok


def formater_alerte(score: int, element: dict, resultat: dict) -> str:
    pastille = "🟢" if score >= 80 else "🟡"
    titre = (resultat.get("titre") or element["texte"])[:150]
    entreprise_lieu = " · ".join(x for x in (resultat.get("entreprise"), resultat.get("lieu")) if x)
    lignes = [
        f"{pastille} {score}/100 — {titre}",
        entreprise_lieu,
        resultat.get("raison", ""),
        element["url"],
        f"(source : {element['source']})",
    ]
    return "\n".join(l for l in lignes if l)


# ───────────────────────── relevé des pages ─────────────────────────

async def relever_liens(navigateur, source: dict) -> dict:
    """Ouvre la page et renvoie {url normalisée: texte du lien}."""
    contexte = await navigateur.new_context(
        user_agent=UA, locale="en-GB", viewport={"width": 1366, "height": 2000}
    )
    page = await contexte.new_page()
    try:
        await page.goto(source["url"], wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass  # certaines pages ne se « calment » jamais : on continue
        await page.wait_for_timeout(int(source.get("attente", 3)) * 1000)
        for _ in range(3):  # défile pour charger les listes qui s'affichent au fil de l'eau
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(700)

        adresse_page = normaliser_url(page.url)
        liens = {}
        for cadre in page.frames:  # certaines pages carrières sont dans un cadre intégré
            try:
                bruts = await cadre.eval_on_selector_all("a[href]", JS_LIENS)
            except Exception:
                continue
            for brut in bruts:
                cle = normaliser_url(brut.get("href") or "")
                texte = nettoyer_texte(brut.get("texte"))
                if not cle or cle == adresse_page or len(texte) < 3:
                    continue
                if cle not in liens or len(texte) > len(liens[cle]):
                    liens[cle] = texte
        return liens
    finally:
        await contexte.close()


# ───────────────────────── tri par Claude ─────────────────────────

def evaluer(client, modele: str, profil: str, elements: list) -> list:
    resultats = []
    for debut in range(0, len(elements), TAILLE_LOT_CLAUDE):
        lot = elements[debut:debut + TAILLE_LOT_CLAUDE]
        lignes = []
        for e in lot:
            contexte = f" – {e['contexte']}" if e.get("contexte") else ""
            lignes.append(f"{e['i']}. [{e['source']}{contexte}] {e['texte']} → {e['url']}")
        message = (
            f"PROFIL ET CRITÈRES DE LA CANDIDATE :\n{profil}\n\n"
            "LIENS À TRIER :\n" + "\n".join(lignes)
        )
        reponse = client.messages.create(
            model=modele,
            max_tokens=6000,
            system=CONSIGNE,
            messages=[{"role": "user", "content": message}],
        )
        texte = "".join(b.text for b in reponse.content if getattr(b, "type", "") == "text")
        resultats.extend(lire_json(texte))
    return resultats


# ───────────────────────── programme ─────────────────────────

def lire_profil() -> str:
    profil = os.environ.get("PROFIL", "").strip()
    if not profil and FICHIER_PROFIL.exists():
        profil = FICHIER_PROFIL.read_text(encoding="utf-8").strip()
    return profil


async def main() -> int:
    mode = os.environ.get("MODE", "normal").strip().lower()
    config = yaml.safe_load(FICHIER_SOURCES.read_text(encoding="utf-8")) or {}
    reglages = config.get("reglages") or {}
    modele = str(reglages.get("modele", "claude-haiku-5-5"))
    seuil = int(reglages.get("score_minimum", 70))
    resume_initial = bool(reglages.get("premier_passage_resume", True))
    max_alertes = int(reglages.get("max_alertes_par_passage", 15))
    sources = [
        s for s in (config.get("sources") or [])
        if isinstance(s, dict) and s.get("actif", True) and str(s.get("url") or "").startswith("http")
    ]

    profil = lire_profil()
    if not profil:
        envoyer_telegram("⚠️ Veille : le profil est vide. Ajoute le secret PROFIL dans GitHub.")
        print("Profil manquant (secret PROFIL).")
        return 1

    from anthropic import Anthropic
    client = Anthropic()  # lit ANTHROPIC_API_KEY

    # ── Mode test : vérifie Telegram et Claude, puis s'arrête.
    if mode == "test":
        try:
            r = client.messages.create(
                model=modele, max_tokens=20,
                messages=[{"role": "user", "content": "Réponds seulement : OK"}],
            )
            etat_claude = "Claude répond"
        except Exception as err:
            etat_claude = f"Claude ne répond pas ({err})"
        envoyer_telegram(
            f"✅ Test de la veille : Telegram fonctionne. {etat_claude} (modèle {modele}).\n"
            f"{len(sources)} sources actives dans sources.yaml."
        )
        return 0

    from playwright.async_api import async_playwright

    etat = charger_etat()
    a_evaluer = list(etat["en_attente"])  # offres non triées lors d'un passage précédent
    etat["en_attente"] = []
    bilan_initial = []

    async with async_playwright() as p:
        navigateur = await p.chromium.launch()
        for source in sources:
            nom = str(source.get("nom") or source["url"])
            es = etat["sources"].setdefault(nom, {"liens": [], "initialisee": False, "echecs": 0})
            try:
                liens = await relever_liens(navigateur, source)
                if not liens:
                    raise RuntimeError("aucun lien trouvé sur la page")
            except Exception as err:
                es["echecs"] = es.get("echecs", 0) + 1
                message_erreur = str(err).splitlines()[0][:200]
                print(f"[{nom}] échec : {message_erreur}")
                if not es.get("initialisee") and es["echecs"] == 1:
                    bilan_initial.append(f"❌ {nom} : échec ({message_erreur})")
                elif es["echecs"] == ECHECS_AVANT_ALERTE:
                    envoyer_telegram(
                        f"⚠️ La source « {nom} » ne répond plus depuis {ECHECS_AVANT_ALERTE} passages "
                        f"({message_erreur}). Vérifie son adresse dans sources.yaml."
                    )
                continue

            es["echecs"] = 0
            premiere_fois = not es.get("initialisee")
            connus = set(es.get("liens", []))
            frais = [(u, t) for u, t in liens.items() if u not in connus]
            es["liens"] = (es.get("liens", []) + [u for u, _ in frais])[-MAX_LIENS_MEMORISES:]
            es["initialisee"] = True
            es["derniere_verification"] = maintenant()
            print(f"[{nom}] {len(liens)} liens, dont {len(frais)} nouveaux")

            if premiere_fois:
                bilan_initial.append(f"✅ {nom} : {len(liens)} liens relevés")
                if not resume_initial:
                    continue
            for url, texte in frais:
                a_evaluer.append({
                    "source": nom,
                    "contexte": str(source.get("contexte") or ""),
                    "url": url,
                    "texte": texte,
                    "initial": premiere_fois,
                })
        await navigateur.close()

    # Une même offre peut apparaître sur plusieurs sources : on ne la trie qu'une fois.
    deja = set(etat["evaluees"])
    uniques, vus = [], set()
    for e in a_evaluer:
        if e["url"] in deja or e["url"] in vus:
            continue
        vus.add(e["url"])
        uniques.append(e)
    for i, e in enumerate(uniques):
        e["i"] = i

    alertes = []
    if uniques:
        try:
            resultats = evaluer(client, modele, profil, uniques)
            etat["evaluees"].extend(e["url"] for e in uniques)
        except Exception as err:
            print(f"Tri par Claude impossible : {err}")
            etat["en_attente"] = [{k: v for k, v in e.items() if k != "i"} for e in uniques]
            envoyer_telegram(f"⚠️ Veille : le tri par Claude a échoué ({str(err)[:200]}). Nouvel essai au prochain passage.")
            resultats = []
        for r in resultats:
            try:
                idx, score = int(r.get("i")), int(r.get("score", 0))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(uniques) and score >= seuil:
                alertes.append((score, uniques[idx], r))

    alertes.sort(key=lambda a: a[0], reverse=True)
    nouvelles = [a for a in alertes if not a[1]["initial"]]
    deja_en_ligne = [a for a in alertes if a[1]["initial"]]

    for score, element, resultat in nouvelles[:max_alertes]:
        envoyer_telegram(formater_alerte(score, element, resultat))
    if len(nouvelles) > max_alertes:
        reste = nouvelles[max_alertes:]
        envoyer_telegram(
            f"… et {len(reste)} autres offres pertinentes :\n"
            + "\n".join(f"• {s}/100 {r.get('titre') or e['texte'][:80]} — {e['url']}" for s, e, r in reste)
        )

    if deja_en_ligne:
        envoyer_telegram(
            f"📋 Premier passage : {len(deja_en_ligne)} offre(s) déjà en ligne correspondent à ton profil."
        )
        for score, element, resultat in deja_en_ligne[:max_alertes]:
            envoyer_telegram(formater_alerte(score, element, resultat))
        if len(deja_en_ligne) > max_alertes:
            reste = deja_en_ligne[max_alertes:]
            envoyer_telegram(
                "Les autres :\n"
                + "\n".join(f"• {s}/100 {r.get('titre') or e['texte'][:80]} — {e['url']}" for s, e, r in reste)
            )

    if bilan_initial:
        envoyer_telegram(
            "🔎 Bilan des nouvelles sources :\n" + "\n".join(bilan_initial)
            + "\n\nUne source en échec ou avec très peu de liens : vérifie son adresse dans sources.yaml."
        )

    sauvegarder_etat(etat)
    print(f"Passage terminé : {len(uniques)} liens triés, {len(alertes)} alertes.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
