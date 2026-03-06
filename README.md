# BookSearch 📚

Application de recherche et téléchargement de livres. **100% indépendante de BookLore.**

## Stack
- **Backend** : Python / Flask
- **Moteurs** : Anna's Archive + LibGen
- **Sécurité** : Auth par session + Rate limiting (5 tentatives / 15min)

## Installation sur le VPS

```bash
# 1. Copier le projet
scp -P 1710 -r search-app/ sabu@VPS_IP:/opt/search-app

# 2. Créer le venv et installer les dépendances
cd /opt/search-app
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
chmod +x start.sh

# 3. Lancer avec PM2
SEARCH_PASSWORD="VotreMotDePasse" FLASK_SECRET="UnSecretAleatoire" \
  pm2 start /opt/search-app/start.sh --name search-app
pm2 save

# 4. Vérifier
curl http://localhost:5000/health   # doit répondre "OK"
```

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `SEARCH_PASSWORD` | `changeme` | Mot de passe de connexion |
| `FLASK_SECRET` | Aléatoire | Clé de signature des sessions |
| `BOOKLORE_DIR` | `/opt/booklore/bookdrop` | Dossier de dépôt des livres |

## Cloudflare Tunnel

Pointez `searchbook.s4bu.tech` → `http://localhost:5000`
