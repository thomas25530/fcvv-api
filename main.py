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
    print("Firebase initialise avec succes")
except Exception as e:
  print(f"Erreur critique initialisation Firebase: {e}")

app = FastAPI()


# --- Modèles ---
class Vote(BaseModel):
  id_sondage: str
  nom_parent: str
  nom_joueur_concerne: Optional[str] = None # <--- Pour cibler l'enfant en question
  choix: Optional[str] = None          
  choix_trajet: Optional[str] = None   
  second_vote: Optional[str] = None    
  choix_multiple: Optional[str] = None


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
    print(f"[FCM API] envoye : {response}")

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

# --- ROUTE NOTIFICATION MANUELLE (Panneau Admin Python) ---
@app.post("/notifier/{categorie}")
def envoyer_notification_manuelle(
    categorie: str, notif: NotifRequest
):
  try:
    envoyer_notif_push(
        topic=categorie,
        titre=notif.titre,
        corps=notif.corps,
    )
    return {"status": "success", "message": "Notification envoyée avec succès"}
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))

def obtenir_joueurs_associes(nom_parent: str) -> list:
  if not nom_parent:
    return []
  id_utilisateur = nom_parent.strip().replace(" ", "_").lower()
  doc_ref = db.collection("users").document(id_utilisateur).get()

  if doc_ref.exists:
    data = doc_ref.to_dict()
    return data.get("joueurs_associes", [])
  return []

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
def enregistrer_vote(
    categorie: str,
    vote: Vote,
    nom_parent_header: Optional[str] = Header(None, alias="nom_parent")
):
    # 1. Identification stricte de l'utilisateur connecté (Auteur de l'action)
    utilisateur_connecte = (nom_parent_header or vote.nom_parent or "").strip()

    if not utilisateur_connecte:
        raise HTTPException(status_code=400, detail="Identifiant de l'utilisateur manquant")

    if verifier_si_exclu(utilisateur_connecte):
        raise HTTPException(status_code=403, detail="Action interdite : compte exclu")

    try:
        # 2. Identification de l'entité/joueur ciblé par le vote
        nom_identifiant_vote = (vote.nom_joueur_concerne or "").strip()

        # S'il n'est pas fourni, recherche dans les rôles/enfants associés dans Firestore
        if not nom_identifiant_vote:
            joueurs_lies = obtenir_joueurs_associes(utilisateur_connecte)
            if joueurs_lies:
                nom_identifiant_vote = joueurs_lies[0]
            else:
                nom_identifiant_vote = utilisateur_connecte

        # 3. Détection du rôle COACH
        is_coach = str(nom_identifiant_vote).upper().startswith("COACH_")

        # 4. Récupération de la convocation Firestore
        doc_ref = db.collection(f"convocations_{categorie}").document(vote.id_sondage)
        doc_snapshot = doc_ref.get()
        
        current_votes = {}
        if doc_snapshot.exists:
            current_votes = doc_snapshot.to_dict().get("votes", {})
        
        if nom_identifiant_vote not in current_votes or not isinstance(current_votes[nom_identifiant_vote], dict):
            current_votes[nom_identifiant_vote] = {}

        # 5. Enregistrement des choix de vote
        if vote.choix is not None:
            current_votes[nom_identifiant_vote]["disponibilite"] = vote.choix
        if vote.choix_trajet is not None:
            current_votes[nom_identifiant_vote]["trajet"] = vote.choix_trajet
        if vote.second_vote is not None:
            current_votes[nom_identifiant_vote]["second_vote"] = vote.second_vote
        if vote.choix_multiple is not None:
            current_votes[nom_identifiant_vote]["choix_multiple"] = vote.choix_multiple

        # 6. Traçabilité & Métadonnées
        current_votes[nom_identifiant_vote]["dernier_modificateur"] = utilisateur_connecte
        current_votes[nom_identifiant_vote]["est_coach"] = is_coach

        doc_ref.set({"votes": current_votes}, merge=True)
        return {
            "message": "Vote mis à jour avec succès",
            "joueur": nom_identifiant_vote,
            "modifie_par": utilisateur_connecte
        }

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


# --- USERS (REGISTER & UNREGISTER) ---
@app.post("/users/register")
def register_user(user: dict):
    raw_nom = user.get("nom", "").strip()
    id_utilisateur = raw_nom.replace(" ", "_").lower()
    
    nouveau_joueur = user.get("joueur_associe", "").strip()
    categorie = user.get("categorie", "").strip()
    
    doc_ref = db.collection("users").document(id_utilisateur)
    doc_snapshot = doc_ref.get()
    
    if not doc_snapshot.exists:
        joueurs_list = [nouveau_joueur] if nouveau_joueur else []
        categories_list = [categorie] if categorie else []
        
        doc_ref.set({
            "nom": raw_nom,
            "role": "PARENT",
            "joueurs_associes": joueurs_list,
            "categories_associees": categories_list
        })
        return {"status": "created", "id": id_utilisateur}
    else:
        data = doc_snapshot.to_dict()
        
        joueurs_list = data.get("joueurs_associes", [])
        if nouveau_joueur and nouveau_joueur not in joueurs_list:
            joueurs_list.append(nouveau_joueur)
            
        categories_list = data.get("categories_associees", [])
        if categorie and categorie not in categories_list:
            categories_list.append(categorie)
    
        doc_ref.update({
            "joueurs_associes": joueurs_list,
            "categories_associees": categories_list
        })
        return {"status": "already_exists"}


@app.post("/users/unregister")
def unregister_user(data: dict):
    raw_nom = data.get("nom", "").strip()
    categorie = data.get("categorie", "").strip()
    
    joueur_associe = data.get("joueur_associe", "")
    joueurs_a_retirer = data.get("joueurs_a_retirer", [])
    
    if joueur_associe and not joueurs_a_retirer:
        joueurs_a_retirer = [joueur_associe]

    if not raw_nom:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur requis")
    
    id_utilisateur = raw_nom.replace(" ", "_").lower()
    doc_ref = db.collection("users").document(id_utilisateur)
    doc_snapshot = doc_ref.get()
    
    if not doc_snapshot.exists:
        return {"status": "not_found", "message": "Utilisateur non trouvé"}
    
    user_data = doc_snapshot.to_dict()
    categories_list = user_data.get("categories_associees", [])
    joueurs_list = user_data.get("joueurs_associes", [])
    
    # 1. Retirer la catégorie si elle est présente
    if categorie and categorie in categories_list:
        categories_list.remove(categorie)
    
    # 2. Retirer les joueurs concernés
    for j in joueurs_a_retirer:
        if j in joueurs_list:
            joueurs_list.remove(j)
    
    # 3. MISE À JOUR : On garde le document pour conserver le 'role' (ADMIN, etc.)
    # On met simplement à jour les listes, même si elles deviennent vides.
    doc_ref.update({
        "categories_associees": categories_list,
        "joueurs_associes": joueurs_list
    })
    
    return {
        "status": "unregistered", 
        "message": f"Désinscription de la catégorie {categorie} effectuée. Profil conservé."
    }


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
  sondage_trajet: Optional[bool] = False
  titre_second_vote: Optional[str] = "Second Vote"
  type_sondage: Optional[str] = "classique"
  titre_sondage_multiple: Optional[str] = "Choix multiple" # <--- Nouveau champ
  options_sondage: Optional[list] = Field(default_factory=list)
  activer_convocation: Optional[bool] = False
  sondage_actif: Optional[bool] = True
  joueurs_convoques: Optional[list] = Field(default_factory=list)
  dernier_commit: Optional[str] = ""
  timestamp_action: Optional[str] = ""
  est_modification: Optional[bool] = False


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

        # --- LOGIQUE DE NOTIFICATION AMÉLIORÉE ---
        titre_evt = data_dict.get("titre", "")
        adversaire = data_dict.get("adversaire", "")
        date_evt = data_dict.get("date", "")
        est_mod = data_dict.get("est_modification", False)
        motif = data_dict.get("dernier_commit", "").strip()

        # Détermination du nom pour l'affichage
        if type_evt == "ENTRAINEMENT":
            nom_affiche = titre_evt if titre_evt else "Entraînement"
            type_libelle = "l'entraînement"
        elif type_evt == "MATCH":
            nom_affiche = adversaire if adversaire else match_id
            type_libelle = f"le match contre {nom_affiche}"
        else:
            nom_affiche = titre_evt if titre_evt else match_id
            type_libelle = f"l'événement {nom_affiche}"

        if est_mod:
            titre_notif = f"FCVV - Modification ({categorie})"
            corps_notif = f"Modification concernant {type_libelle} ({date_evt})."
            if motif:
                corps_notif += f"\nMotif : {motif}"
        else:
            if type_evt == "ENTRAINEMENT":
                corps_notif = f"Nouvel entraînement : {nom_affiche} ({date_evt})".strip()
                titre_notif = f"FCVV - Entraînement ({categorie})"
            elif type_evt == "MATCH":
                corps_notif = f"Match contre {nom_affiche} ({date_evt})".strip()
                titre_notif = f"FCVV - Nouvelle Convocation ({categorie})"
            else:
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