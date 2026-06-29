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
    print(f"DEBUG: Tentative de scraping de {url}")
    try:
        # Utilisation d'un User-Agent plus "standard" pour éviter les blocages de base
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        r = requests.get(url, timeout=15, headers=headers)
        
        print(f"DEBUG: Status Code reçu: {r.status_code}")
        
        # Si vous recevez une erreur (403, 404), on le saura tout de suite
        if r.status_code != 200:
            print(f"DEBUG: Erreur HTTP {r.status_code} sur l'URL.")
            return None

        # Affichage d'un échantillon du HTML pour voir si le tableau est bien présent
        print(f"DEBUG: Taille du contenu HTML reçu: {len(r.text)} octets")
        
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # On teste le sélecteur, et s'il est vide, on logue une alerte
        rows = soup.select('table.classement-table tbody tr') 
        print(f"DEBUG: Nombre de lignes trouvées avec 'table.classement-table': {len(rows)}")
        
        if len(rows) == 0:
            # Si aucune ligne n'est trouvée, c'est peut-être que la structure a changé
            # On logue le HTML pour debug (limité aux 500 premiers caractères)
            print(f"DEBUG: HTML extrait (début): {r.text[:500]}...")
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
        
        print(f"DEBUG: Scraping réussi, {len(tableau_data)} équipes ajoutées.")
        return tableau_data
        
    except Exception as e:
        print(f"Erreur critique lors du scraping de {url}: {str(e)}")
        return None

# URL de téléchargement direct de votre fichier YAML sur le Drive
YAML_DRIVE_URL = "https://docs.google.com/uc?export=download&id=161ngxPQz66QumHjG_us6qqyAtA0GPX2x"

def job_update_classements():
    print("Début de la mise à jour automatique des classements...")
    try:
        # 1. Lecture du YAML depuis le Drive
        response = requests.get(YAML_DRIVE_URL, timeout=15)
        config = yaml.safe_load(response.text)
        classements_config = config.get("classements", [])

        # 2. Scraping et mise à jour Firestore
        for item in classements_config:
            nom = item.get("equipe_nom")
            url = item.get("fff_url")
            if not url: continue
            
            data = scrape_fff_classement(url)
            if data:
                db.collection("classements").document(nom).set({
                    "tableau": data,
                    "maj": datetime.now().strftime("%d/%m/%Y %H:%M")
                })
    except Exception as e:
        print(f"Erreur lors du job de mise à jour : {e}")

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
    """Route pour forcer le scraping immédiatement."""
    try:
        job_update_classements()
        return {"message": "Mise à jour lancée avec succès"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)