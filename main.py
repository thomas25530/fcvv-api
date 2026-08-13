import json
import os
from datetime import datetime
from typing import Optional

import firebase_admin
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from firebase_admin import credentials, firestore, messaging
from pydantic import BaseModel, Field
import uvicorn

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
  choix: Optional[str] = None          # Pour la disponibilité ("Présent" / "Absent")
  choix_trajet: Optional[str] = None   # Pour le trajet / second vote
  second_vote: Optional[str] = None


class NotifRequest(BaseModel):
  titre: str
  corps: str


class Message(BaseModel):
  auteur: str
  contenu: str
  role: Optional[str] = "PARENT"  # Valeur par défaut
  timestamp: Optional[datetime] = None


class SondageModel(BaseModel):
  titre: str
  date: str
  heure: str
  lieu: str
  type: Optional[str] = "entrainement"
  sondage_actif: Optional[bool] = True


# --- Fonctions utilitaires ---
def envoyer_notif_push(topic: str, titre: str, corps: str):
  topic = topic.strip()

  try:
    android_config = messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            icon="ic_notification",
            color="#1E3A8A",
            channel_id="fcvv_high_priority_v2",
            sound="default",
        ),
    )

    apns_config = messaging.APNSConfig(
        headers={"apns-priority": "10"},
        payload=messaging.APNSPayload(
            aps=messaging.Aps(
                alert=messaging.ApsAlert(title=titre, body=corps),
                sound="default",
            )
        ),
    )

    message = messaging.Message(
        notification=messaging.Notification(title=titre, body=corps),
        android=android_config,
        apns=apns_config,
        topic=topic,
    )

    response = messaging.send(message)
    print(f"[FCM API] envoyé : {response}")

  except Exception as e:
    print(f"[FCM ERROR] {e}")


def obtenir_role_utilisateur(nom_parent: str) -> Optional[str]:
  if not nom_parent:
    return None
  id_utilisateur = nom_parent.strip().replace(" ", "_").lower()
  doc_ref = db.collection("users").document(id_utilisateur).get()

  if doc_ref.exists:
    data = doc_ref.to_dict()
    return data.get("role")
  return None


def verifier_si_admin(nom_parent: str) -> bool:
  return obtenir_role_utilisateur(nom_parent) == "ADMIN"


def verifier_si_exclu(nom_parent: str) -> bool:
  return obtenir_role_utilisateur(nom_parent) == "EXCLU"


# --- Routes ---
@app.get("/")
def ping():
  return {"status": "ok", "message": "Server is awake"}


@app.get("/chat/{categorie}")
def get_messages(
    categorie: str, nom_parent: str = Header(alias="nom_parent")
):
  if verifier_si_exclu(nom_parent):
    raise HTTPException(status_code=403, detail="Accès refusé : compte exclu")

  try:
    docs = (
        db.collection("chats")
        .document(categorie)
        .collection("messages")
        .order_by("timestamp", direction=firestore.Query.ASCENDING)
        .limit(50)
        .stream()
    )

    results = []
    for doc in docs:
      data = doc.to_dict()
      ts = data.get("timestamp")
      data["timestamp"] = (
          ts.isoformat()
          if ts and hasattr(ts, "isoformat")
          else datetime.utcnow().isoformat()
      )
      results.append({"id": doc.id, **data})
    return results
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/{categorie}")
def poster_message(
    categorie: str, message: Message, background_tasks: BackgroundTasks
):
  if verifier_si_exclu(message.auteur):
    raise HTTPException(
        status_code=403, detail="Action interdite : compte exclu"
    )

  try:
    msg_data = {
        "auteur": message.auteur,
        "contenu": message.contenu,
        "role": message.role,
        "timestamp": firestore.SERVER_TIMESTAMP,
    }
    db.collection("chats").document(categorie).collection("messages").add(
        msg_data
    )

    background_tasks.add_task(
        envoyer_notif_push,
        categorie,
        f"FCVV - Nouveau message ({categorie})",
        f"{message.auteur}: {message.contenu}",
    )

    return {"message": "Message envoyé avec succès"}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/sondages/{categorie}")
def get_sondages_par_categorie(
    categorie: str, nom_parent: str = Header(alias="nom_parent")
):
  if verifier_si_exclu(nom_parent):
    raise HTTPException(status_code=403, detail="Accès refusé : compte exclu")

  try:
    docs = db.collection(f"sondages_{categorie}").stream()
    return {doc.id: doc.to_dict() for doc in docs}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/voter/{categorie}")
def enregistrer_vote(categorie: str, vote: Vote):
  if verifier_si_exclu(vote.nom_parent):
    raise HTTPException(
        status_code=403, detail="Action interdite : compte exclu"
    )

  try:
    doc_ref = db.collection(f"convocations_{categorie}").document(vote.id_sondage)
    
    doc_snapshot = doc_ref.get()
    current_votes = {}
    if doc_snapshot.exists:
      current_votes = doc_snapshot.to_dict().get("votes", {})
    
    if vote.nom_parent not in current_votes or not isinstance(current_votes[vote.nom_parent], dict):
      current_votes[vote.nom_parent] = {}

    if vote.choix is not None:
      current_votes[vote.nom_parent]["disponibilite"] = vote.choix
    if vote.choix_trajet is not None:
      current_votes[vote.nom_parent]["trajet"] = vote.choix_trajet
    if vote.second_vote is not None:
      current_votes[vote.nom_parent]["second_vote"] = vote.second_vote

    doc_ref.set(
        {"votes": current_votes}, 
        merge=True
    )
    return {"message": "Vote mis à jour avec succès"}
  except Exception as e:
    print(f"[ERREUR VOTE] {e}")
    raise HTTPException(status_code=500, detail=str(e))


# --- SONDAGES (CRUD + NOTIF AUTOMATIQUE) ---
@app.post("/sondages/create/{categorie}")
def create_sondage(
    categorie: str,
    sondage: SondageModel,
    background_tasks: BackgroundTasks,
    nom_parent: str = Header(alias="nom_parent"),
):
  if not verifier_si_admin(nom_parent):
    raise HTTPException(status_code=403, detail="Accès refusé")
  try:
    db.collection(f"sondages_{categorie}").add(sondage.model_dump())

    background_tasks.add_task(
        envoyer_notif_push,
        categorie,
        f"FCVV - Nouveau sondage ({categorie})",
        f"Sondage : {sondage.titre}",
    )

    return {"status": "created"}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.put("/sondages/update/{categorie}/{sid}")
def update_sondage(
    categorie: str,
    sid: str,
    data: dict,
    nom_parent: str = Header(alias="nom_parent"),
):
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
def delete_sondage(
    categorie: str, sid: str, nom_parent: str = Header(alias="nom_parent")
):
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


# --- USERS ---
@app.post("/users/register")
def register_user(user: dict):
  raw_nom = user.get("nom", "").strip()
  id_utilisateur = raw_nom.replace(" ", "_").lower()

  doc_ref = db.collection("users").document(id_utilisateur)

  if not doc_ref.get().exists:
    doc_ref.set({"nom": raw_nom, "role": "PARENT"})
    return {"status": "created", "id": id_utilisateur}

  return {"status": "already_exists"}


# --- Modèle Pydantic pour les Convocations & Événements ---
class ConvocationModel(BaseModel):
  type: Optional[str] = "MATCH"
  titre: Optional[str] = ""
  adversaire: Optional[str] = ""
  date: Optional[str] = ""
  heure_rdv: Optional[str] = ""
  heure_coup_envoi: Optional[str] = ""
  heure: Optional[str] = ""
  lieu: Optional[str] = ""
  entraineurs: Optional[str] = ""
  sondage_classique: Optional[bool] = True
  sondage_trajet: Optional[bool] = False         # Pilote le second vote
  titre_second_vote: Optional[str] = "Second Vote"  # Titre personnalisable du second vote
  activer_convocation: Optional[bool] = False
  sondage_actif: Optional[bool] = True
  joueurs_convoques: Optional[list] = Field(default_factory=list)


# --- CONVOCATIONS & ÉVÉNEMENTS (CRUD + NOTIF AUTOMATIQUE) ---
@app.put("/convocations/update/{categorie}/{match_id}")
def update_convocations(
    categorie: str,
    match_id: str,
    payload: ConvocationModel,
    background_tasks: BackgroundTasks,
    nom_parent: str = Header(alias="nom_parent"),
):
  if not verifier_si_admin(nom_parent):
    raise HTTPException(status_code=403, detail="Accès refusé")

  try:
    data_dict = payload.model_dump()
    
    type_evt = data_dict.get("type", "EVENEMENT").upper()
    date_brute = data_dict.get("date", "").replace("/", "-")

    est_un_nouveau = not match_id or match_id == "Nouvel événement" or match_id.strip() == ""
    type_incoherent = (type_evt == "MATCH" and not match_id.startswith("match_")) or \
                      (type_evt == "ENTRAINEMENT" and not match_id.startswith("entrainement_"))

    if est_un_nouveau or type_incoherent:
      if type_evt == "MATCH":
        adversaire = data_dict.get("adversaire", "inconnu").strip().replace(" ", "_").lower()
        heure_rdv = data_dict.get("heure_rdv", "").replace(":", "h") or "00h00"
        nouveau_match_id = f"match_{adversaire}_{date_brute}_{heure_rdv}".strip("_")
      elif type_evt == "ENTRAINEMENT":
        heure_ent = data_dict.get("heure", data_dict.get("heure_rdv", "")).replace(":", "h") or "00h00"
        nouveau_match_id = f"entrainement_{date_brute}_{heure_ent}".strip("_")
      else:
        titre_evt = data_dict.get("titre", "evt").strip().replace(" ", "_").lower()
        nouveau_match_id = f"evt_{titre_evt}_{date_brute}".strip("_")
      
      if not est_un_nouveau and match_id and match_id != nouveau_match_id:
        try:
          db.collection(f"convocations_{categorie}").document(match_id).delete()
        except Exception:
          pass
          
      match_id = nouveau_match_id

    doc_ref = db.collection(f"convocations_{categorie}").document(match_id)
    doc_ref.set(data_dict, merge=True)

    titre_evt = data_dict.get("titre", "")
    adversaire = data_dict.get("adversaire", "")
    date_evt = data_dict.get("date", "")

    if type_evt == "ENTRAINEMENT":
      nom_affiche = titre_evt if titre_evt else "Entraînement"
      corps_notif = f"Nouvel entraînement : {nom_affiche} ({date_evt})".strip()
      titre_notif = f"FCVV - Entraînement ({categorie})"
    elif type_evt == "MATCH":
      nom_affiche = adversaire if adversaire else match_id
      corps_notif = f"Match contre {nom_affiche} ({date_evt})".strip()
      titre_notif = f"FCVV - Nouvelle Convocation ({categorie})"
    else:
      nom_affiche = titre_evt if titre_evt else match_id
      corps_notif = f"Événement : {nom_affiche} ({date_evt})".strip()
      titre_notif = f"FCVV - Nouvel Événement ({categorie})"

    background_tasks.add_task(
        envoyer_notif_push,
        categorie,
        titre_notif,
        corps_notif,
    )

    return {"status": "updated", "id": match_id}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.delete("/convocations/delete/{categorie}/{match_id}")
def delete_convocation(
    categorie: str, match_id: str, nom_parent: str = Header(alias="nom_parent")
):
  if not verifier_si_admin(nom_parent):
    raise HTTPException(status_code=403, detail="Accès refusé")

  try:
    db.collection(f"convocations_{categorie}").document(match_id).delete()
    return {"status": "deleted"}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.get("/convocations/{categorie}")
def get_convocations(categorie: str):
  docs = db.collection(f"convocations_{categorie}").stream()
  return {doc.id: doc.to_dict() for doc in docs}


@app.get("/convocations/{categorie}/{match_id}")
def get_one_convocation(categorie: str, match_id: str):
  doc = db.collection(f"convocations_{categorie}").document(match_id).get()
  if not doc.exists:
    raise HTTPException(status_code=404, detail="Match non trouvé")
  return doc.to_dict()


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)