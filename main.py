import os
import json
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore, messaging

from datetime import datetime
from typing import Optional

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)