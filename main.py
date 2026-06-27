import os
import json
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore, messaging  # <--- Ajout de messaging

# 1. Initialisation de Firebase
firebase_config_str = os.getenv("FIREBASE_CONFIG")
if not firebase_config_str:
    raise Exception("La variable d'environnement FIREBASE_CONFIG n'est pas définie !")

cred = credentials.Certificate(json.loads(firebase_config_str))
firebase_admin.initialize_app(cred)
db = firestore.client()

app = FastAPI()

class Vote(BaseModel):
    id_sondage: str
    nom_parent: str
    choix: str 

# --- NOUVELLE FONCTION : Envoyer notification ---
def envoyer_notif_push(topic, titre, corps):
    topic = topic.strip()
    try:
        # On définit des options spécifiques pour Android
        android_config = messaging.AndroidConfig(
            priority='high', # Demande au système d'afficher immédiatement
            notification=messaging.AndroidNotification(
                channel_id="fcvv_service_channel" # Le même que dans votre service.py
            )
        )
        
        message = messaging.Message(
            notification=messaging.Notification(title=titre, body=corps),
            android=android_config,
            topic=topic,
        )
        response = messaging.send(message)
        return response
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

# --- NOUVELLE ROUTE : Pour déclencher une notification manuellement ---
# 1. Créez un modèle pour le corps de la requête
class NotifRequest(BaseModel):
    titre: str
    corps: str

# 2. Modifiez la route
@app.post("/notifier/{categorie}")
def envoyer_alerte(categorie: str, payload: NotifRequest):
    res = envoyer_notif_push(categorie, payload.titre, payload.corps)
    if res:
        return {"message": "Notification envoyée", "id": res}
    raise HTTPException(status_code=500, detail="Échec envoi notification")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)