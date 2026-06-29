import os
import json
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore, messaging

from pydantic import BaseModel
from datetime import datetime
from typing import Optional


import requests
from bs4 import BeautifulSoup
import yaml
from apscheduler.schedulers.background import BackgroundScheduler


# 1. Initialisation de Firebase
try:
    firebase_config_str = os.getenv("FIREBASE_CONFIG")
    if not firebase_config_str:
        print("Erreur : Variable FIREBASE_CONFIG manquante")
    else:
        cred = credentials.Certificate(json.loads(firebase_config_str))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase initialisé avec succès")
except Exception as e:
    print(f"Erreur critique initialisation Firebase: {e}")

app = FastAPI()

# --- NOUVELLE ROUTE : WARMUP ---
@app.get("/")
def ping():
    """Route légère pour réveiller le serveur sans charger de données."""
    return {"status": "ok", "message": "Server is awake"}

class Vote(BaseModel):
    id_sondage: str
    nom_parent: str
    choix: str 

class NotifRequest(BaseModel):
    titre: str
    corps: str

class Message(BaseModel):
    auteur: str
    contenu: str
    timestamp: Optional[datetime] = None
    
@app.get("/chat/{categorie}")
def get_messages(categorie: str):
    try:
        # Cible bien la sous-collection "messages"
        docs = db.collection("chats").document(categorie).collection("messages") \
            .order_by("timestamp", direction=firestore.Query.ASCENDING) \
            .limit(50) \
            .stream()
        
        results = []
        for doc in docs:
            data = doc.to_dict()
            # Gérer le timestamp proprement
            ts = data.get('timestamp')
            if ts and hasattr(ts, 'isoformat'):
                data['timestamp'] = ts.isoformat()
            else:
                data['timestamp'] = datetime.utcnow().isoformat() # Fallback
                
            results.append({"id": doc.id, **data})
        return results
    except Exception as e:
        # Si Firestore demande un index, l'erreur apparaîtra ici
        print(f"Erreur Firestore: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat/{categorie}")
def poster_message(categorie: str, message: Message):
    try:
        msg_data = {
            "auteur": message.auteur,
            "contenu": message.contenu,
            "timestamp": firestore.SERVER_TIMESTAMP # Utilise l'heure de Google
        }
        # Ajout dans la sous-collection
        db.collection("chats").document(categorie).collection("messages").add(msg_data)
        
        # Optionnel : Envoyer une notif FCM aux autres membres du topic
        # envoyer_notif_push(categorie, f"Nouveau message de {message.auteur}", message.contenu)
        
        return {"message": "Message envoyé"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def envoyer_notif_push(topic, titre, corps):
    topic = topic.strip()
    try:
        android_config = messaging.AndroidConfig(
            priority='high',
            notification=messaging.AndroidNotification(
                channel_id="fcvv_service_channel"
            )
        )
        message = messaging.Message(
            notification=messaging.Notification(title=titre, body=corps),
            android=android_config,
            topic=topic,
        )
        return messaging.send(message)
    except Exception as e:
        print(f"Erreur envoi notif: {e}")
        return None

@app.get("/sondages/{categorie}")
def get_sondages_par_categorie(categorie: str):
    try:
        collection_name = f"sondages_{categorie}"
        docs = db.collection(collection_name).stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/voter/{categorie}")
def enregistrer_vote(categorie: str, vote: Vote):
    try:
        collection_name = f"sondages_{categorie}"
        doc_ref = db.collection(collection_name).document(vote.id_sondage)
        doc_ref.update({f'votes.{vote.nom_parent}': vote.choix})
        return {"message": "Vote mis à jour"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/notifier/{categorie}")
def envoyer_alerte(categorie: str, payload: NotifRequest):
    res = envoyer_notif_push(categorie, payload.titre, payload.corps)
    if res:
        return {"message": "Notification envoyée", "id": res}
    raise HTTPException(status_code=500, detail="Échec envoi notification")

# Ajout de cette fonction pour scraper la FFF
def scrape_fff_classement(url):
    print(f"DEBUG [Scraping]: Tentative de scraping de {url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        r = requests.get(url, timeout=15, headers=headers)
        
        print(f"DEBUG [Scraping]: Statut HTTP {r.status_code}")
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, 'html.parser')
        
        # LOGS POUR DIAGNOSTIQUER SI LA PAGE EST VIDE OU SI LE SÉLECTEUR EST MAUVAIS
        rows = soup.select('table.classement-table tbody tr')
        print(f"DEBUG [Scraping]: {len(rows)} lignes trouvées avec 'table.classement-table'")
        
        if len(rows) == 0:
            # Affiche un extrait pour vérifier si on a bien la page FFF ou une erreur
            print(f"DEBUG [Scraping]: Contenu HTML extrait (début): {r.text[:300]}...")
            return None
        
        tableau_data = []
        for row in rows:
            cols = row.find_all('td')
            if len(cols) > 1:
                tableau_data.append({
                    "rang": cols[0].text.strip(),
                    "equipe": cols[1].text.strip(),
                    "pts": cols[2].text.strip(),
                    "j": cols[3].text.strip(),
                    "g": cols[4].text.strip(),
                    "n": cols[5].text.strip(),
                    "p": cols[6].text.strip(),
                    "bp": cols[8].text.strip() if len(cols) > 8 else "0",
                    "bc": cols[9].text.strip() if len(cols) > 9 else "0",
                    "diff": cols[11].text.strip() if len(cols) > 11 else "0"
                })
        return tableau_data
        
    except Exception as e:
        print(f"DEBUG [Scraping]: Erreur critique sur {url} : {str(e)}")
        return None

YAML_DRIVE_URL = "https://docs.google.com/uc?export=download&id=161ngxPQz66QumHjG_us6qqyAtA0GPX2x"

def job_update_classements():
    print("DEBUG [Job]: --- DÉBUT EFFECTIF DE job_update_classements ---")
    try:
        # 1. Lecture du YAML
        response = requests.get(YAML_DRIVE_URL, timeout=15)
        print(f"DEBUG [Job]: Réponse Drive status={response.status_code}")
        
        config = yaml.safe_load(response.text)
        if not config:
            print("DEBUG [Job]: Erreur, YAML vide ou malformé.")
            return

        classements_config = config.get("classements", [])
        print(f"DEBUG [Job]: {len(classements_config)} équipes configurées.")

        # 2. Boucle de traitement
        for item in classements_config:
            nom = item.get("equipe_nom")
            url = item.get("fff_url")
            print(f"DEBUG [Job]: Traitement de {nom}...")
            
            if not url:
                print(f"DEBUG [Job]: Pas d'URL pour {nom}, on saute.")
                continue
            
            data = scrape_fff_classement(url)
            if data:
                db.collection("classements").document(nom).set({
                    "tableau": data,
                    "maj": datetime.now().strftime("%d/%m/%Y %H:%M")
                })
                print(f"DEBUG [Job]: Succès Firestore pour {nom}.")
            else:
                print(f"DEBUG [Job]: Échec scraping pour {nom}.")
                
        print("DEBUG [Job]: --- FIN DU JOB ---")
        
    except Exception as e:
        print(f"DEBUG [Job]: ERREUR FATALE DANS LE JOB : {str(e)}")

# Initialisation du planificateur (tous les jours à 05h00)
scheduler = BackgroundScheduler()
scheduler.add_job(job_update_classements, 'cron', hour=5, minute=0)
scheduler.start()

@app.get("/classement")
def get_classement_auto():
    """Route appelée par l'application pour lire les données déjà en cache dans Firestore"""
    try:
        docs = db.collection("classements").stream()
        return {doc.id: {"equipe_nom": doc.id, "tableau": doc.to_dict().get("tableau"), "maj": doc.to_dict().get("maj")} for doc in docs}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erreur lors de la récupération des classements")

@app.post("/trigger-update")
def force_update():
    print("DEBUG: Entrée dans la route /trigger-update")
    try:
        # On exécute le job directement sans threading pour voir les erreurs en direct
        job_update_classements()
        return {"message": "Mise à jour terminée avec succès"}
    except Exception as e:
        print(f"DEBUG: ERREUR DANS LA ROUTE: {str(e)}")
        return {"message": "Erreur lors de la mise à jour", "error": str(e)}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)