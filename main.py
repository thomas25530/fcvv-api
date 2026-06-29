import os
import json
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
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

# --- Modèles ---
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

# --- Fonctions utilitaires ---
def envoyer_notif_push(topic: str, titre: str, corps: str):
    """Fonction exécutée en arrière-plan pour envoyer la notification FCM."""
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
        messaging.send(message)
    except Exception as e:
        print(f"Erreur envoi notif pour le topic {topic}: {e}")

# --- Routes ---
@app.get("/")
def ping():
    return {"status": "ok", "message": "Server is awake"}

@app.get("/chat/{categorie}")
def get_messages(categorie: str):
    try:
        docs = db.collection("chats").document(categorie).collection("messages") \
            .order_by("timestamp", direction=firestore.Query.ASCENDING) \
            .limit(50) \
            .stream()
        
        results = []
        for doc in docs:
            data = doc.to_dict()
            ts = data.get('timestamp')
            data['timestamp'] = ts.isoformat() if ts and hasattr(ts, 'isoformat') else datetime.utcnow().isoformat()
            results.append({"id": doc.id, **data})
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat/{categorie}")
def poster_message(categorie: str, message: Message, background_tasks: BackgroundTasks):
    try:
        msg_data = {
            "auteur": message.auteur,
            "contenu": message.contenu,
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        # 1. Ajout dans Firestore
        db.collection("chats").document(categorie).collection("messages").add(msg_data)
        
        # 2. Ajout de la notification en tâche de fond
        # On ne bloque pas la réponse API le temps que FCM réponde
        background_tasks.add_task(
            envoyer_notif_push, 
            categorie, 
            f"Nouveau message ({categorie})", 
            f"{message.auteur}: {message.contenu}"
        )
        
        return {"message": "Message envoyé avec succès"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sondages/{categorie}")
def get_sondages_par_categorie(categorie: str):
    try:
        docs = db.collection(f"sondages_{categorie}").stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/voter/{categorie}")
def enregistrer_vote(categorie: str, vote: Vote):
    try:
        db.collection(f"sondages_{categorie}").document(vote.id_sondage).update({f'votes.{vote.nom_parent}': vote.choix})
        return {"message": "Vote mis à jour"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/notifier/{categorie}")
def envoyer_alerte(categorie: str, payload: NotifRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(envoyer_notif_push, categorie, payload.titre, payload.corps)
    return {"message": "Notification programmée"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)