import os
import json
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header
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
    role: Optional[str] = "PARENT" # Valeur par défaut
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
            "role": message.role, # <--- Ajout de l'enregistrement du rôle
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        db.collection("chats").document(categorie).collection("messages").add(msg_data)
        
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


# Hypothèse : vous avez une collection "admins" ou vous vérifiez le rôle dans une collection "users"
def verifier_si_admin(nom_parent: str):
    # Remplacez "users" par le nom de votre collection où sont stockés les rôles
    docs = db.collection("users").where("nom", "==", nom_parent).where("role", "==", "ADMIN").stream()
    return any(True for _ in docs)

# --- Modèle pour la mise à jour/création ---
class SondageModel(BaseModel):
    titre: str
    date: str
    lieu: str = "Non défini"
    type: str = "dispo"
    # Vous pouvez ajouter d'autres champs ici

@app.post("/sondages/create/{categorie}")
def create_sondage(categorie: str, sondage: SondageModel, nom_parent: str = Header(alias="nom_parent")):
    if not verifier_si_admin(nom_parent):
        raise HTTPException(status_code=403, detail="Accès refusé")
    try:
        db.collection(f"sondages_{categorie}").add(sondage.model_dump())
        return {"status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/sondages/update/{categorie}/{sid}")
def update_sondage(categorie: str, sid: str, data: dict, nom_parent: str = Header(alias="nom_parent")):
    if not verifier_si_admin(nom_parent):
        raise HTTPException(status_code=403, detail="Accès refusé")
    try:
        doc_ref = db.collection(f"sondages_{categorie}").document(sid)
        if not doc_ref.get().exists:
            raise HTTPException(status_code=404, detail="Sondage non trouvé")
        doc_ref.update(data)
        return {"status": "updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/sondages/delete/{categorie}/{sid}")
def delete_sondage(categorie: str, sid: str, nom_parent: str = Header(alias="nom_parent")):
    if not verifier_si_admin(nom_parent):
        raise HTTPException(status_code=403, detail="Accès refusé")
    try:
        doc_ref = db.collection(f"sondages_{categorie}").document(sid)
        if not doc_ref.get().exists:
            raise HTTPException(status_code=404, detail="Sondage non trouvé")
        doc_ref.delete()
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/users/register")
def register_user(user: dict):
    # On vérifie si l'utilisateur existe déjà
    query = db.collection("users").where("nom", "==", user.get("nom")).stream()
    if not any(query):
        # S'il n'existe pas, on le crée avec le rôle PARENT
        db.collection("users").add({
            "nom": user.get("nom"),
            "role": "PARENT"
        })
        return {"status": "created"}
    return {"status": "already_exists"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)