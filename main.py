import os
import json
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore

# 1. Initialisation de Firebase via variable d'environnement (Cloud Ready)
# Sur Render, vous créerez une variable 'FIREBASE_CONFIG' contenant le JSON
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
        
        # Mise à jour atomique du dictionnaire de votes
        doc_ref.update({
            f'votes.{vote.nom_parent}': vote.choix
        })
        return {"message": "Vote mis à jour"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 2. Lancement pour le cloud (Port 10000, Host 0.0.0.0)
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)