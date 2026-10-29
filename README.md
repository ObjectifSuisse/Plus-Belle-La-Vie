# Veille d'offres d'emploi

Toutes les 15 minutes, ce programme ouvre les pages listées dans `sources.yaml`, repère les nouvelles offres, demande à Claude lesquelles correspondent à ton profil, et t'envoie une alerte Telegram pour chacune.

Mise en place : environ 30 minutes, à faire sur ordinateur.

## 1. Telegram (5 min)
1. Dans Telegram, cherche **@BotFather**, envoie `/newbot`, choisis un nom (ex. « Ma veille »), puis un identifiant qui finit par `bot`.
2. BotFather te donne un **jeton** (une longue suite de caractères). Garde-le : c'est `TELEGRAM_TOKEN`.
3. Ouvre ton nouveau bot et envoie-lui « bonjour ».
4. Dans ton navigateur, ouvre `https://api.telegram.org/botTONJETON/getUpdates` (remplace TONJETON par ton jeton).
5. Repère `"chat":{"id":` suivi d'un nombre : c'est `TELEGRAM_CHAT_ID`.

## 2. Clé API Claude (5 min)
1. Va sur **console.anthropic.com**, crée un compte et ajoute un peu de crédit (Billing).
2. Dans **API Keys**, crée une clé : c'est `ANTHROPIC_API_KEY`.

## 3. GitHub (15 min)
1. Crée un compte sur **github.com**. Un nom neutre suffit (rien ne t'oblige à mettre ton nom).
2. Clique sur **New repository**, nomme-le `veille`, choisis **Public** (les exécutions sont alors gratuites ; tes informations personnelles restent dans les secrets, invisibles).
3. Clique sur **uploading an existing file** et dépose : `watcher.py`, `sources.yaml`, `requirements.txt`, `README.md` et le dossier `state`.
4. Le fichier du minuteur se crée à part, car son dossier est caché : **Add file → Create new file**, tape le nom `.github/workflows/veille.yml`, puis colle le contenu du fichier `veille.yml` fourni. Clique sur **Commit changes**.

## 4. Secrets (5 min)
Dans ton dépôt : **Settings → Secrets and variables → Actions → New repository secret**. Crée ces quatre secrets :

| Nom | Contenu |
|---|---|
| `ANTHROPIC_API_KEY` | ta clé Claude |
| `TELEGRAM_TOKEN` | le jeton de BotFather |
| `TELEGRAM_CHAT_ID` | le nombre trouvé à l'étape 1 |
| `PROFIL` | le texte de `PROFIL_a_coller_dans_GitHub.txt` |

Ne dépose pas le fichier de profil dans le dépôt : il reste uniquement dans le secret.

## 5. Lancer
1. Onglet **Actions** → **Veille offres** → **Run workflow** → mode **test**. Tu dois recevoir un message Telegram « Test de la veille ».
2. Relance avec le mode **normal**. Tu reçois les offres pertinentes déjà en ligne, puis un **bilan** des sources (✅ ou ❌).
3. Ensuite, tout tourne seul toutes les 15 minutes.

## Au quotidien
- **Ajouter ou corriger une page** : ouvre `sources.yaml` sur GitHub, clique sur le crayon, modifie, puis **Commit changes**.
- **Trop ou pas assez d'alertes** : change `score_minimum` dans `sources.yaml` (70 par défaut).
- **Modifier tes critères** : mets à jour le secret `PROFIL`.
- Si une page ne répond plus trois fois de suite, tu reçois un avertissement sur Telegram.

## En cas de problème
- Rien ne se passe : onglet **Actions**, clique sur la dernière exécution et regarde le message en rouge.
- Erreur au moment d'enregistrer (« push ») : **Settings → Actions → General → Workflow permissions** → coche **Read and write permissions**.
