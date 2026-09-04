import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Union, Dict, Any

import firebase_admin
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from firebase_admin import credentials, firestore, messaging
from pydantic import BaseModel, Field
import uvicorn
from fastapi.responses import HTMLResponse
from itsdangerous import URLSafeTimedSerializer
import resend

# 1. Initialisation de Firebase
db = None
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

app = FastAPI(title="API FCVV", version="2.2")

def check_db():
    if db is None:
        raise HTTPException(
            status_code=500, detail="Base de données Firebase non disponible"
        )

##########################
######## GESTION EMAIL & AUTHENTIFICATION

SECRET_KEY = os.getenv("SECRET_KEY", "cle_secrete_fcvv_12345")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "responsable.club@email.com")
API_BASE_URL = os.getenv("API_BASE_URL", "https://votre-app.onrender.com")

resend.api_key = os.getenv("RESEND_API_KEY")
serializer = URLSafeTimedSerializer(SECRET_KEY)

def verifier_si_exclu(nom_parent: str, categorie: str) -> bool:
    """Vérifie si un utilisateur possède le statut EXCLU pour une catégorie donnée."""
    if not nom_parent or db is None:
        return True
    id_utilisateur = nom_parent.strip().replace(" ", "_").lower()
    doc = db.collection("users").document(id_utilisateur).get()
    if not doc.exists:
        return True
    roles = doc.to_dict().get("roles_par_categorie", {})
    return roles.get(categorie, "EXCLU") == "EXCLU"

def verifier_si_admin(nom_parent: str, categorie: str) -> bool:
    """Vérifie si l'utilisateur est ADMIN ou COACH pour la catégorie."""
    if not nom_parent or db is None:
        return False
    id_utilisateur = nom_parent.strip().replace(" ", "_").lower()
    doc = db.collection("users").document(id_utilisateur).get()
    if not doc.exists:
        return False
    roles = doc.to_dict().get("roles_par_categorie", {})
    return roles.get(categorie) in ["ADMIN", "COACH"]

def generer_token_validation(id_utilisateur: str, categorie: str) -> str:
    """Crée un token chiffré valable 7 jours."""
    return serializer.dumps({"id": id_utilisateur, "cat": categorie}, salt="user-validation")

def verifier_token_validation(token: str):
    """Décode le token et extrait les données."""
    try:
        return serializer.loads(token, salt="user-validation", max_age=604800)
    except Exception:
        return None

def envoyer_email_notif_admin(raw_nom: str, categorie: str, id_utilisateur: str):
    timestamp_str = datetime.now(timezone.utc).strftime("%d/%m/%Y à %H:%M:%S (UTC)")
    token = generer_token_validation(id_utilisateur, categorie)
    lien_validation = f"{API_BASE_URL}/users/validate?token={token}"

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <h2>🔔 Nouvelle demande d'accès FCVV</h2>
        <p>Un nouvel utilisateur demande l'accès au vestiaire :</p>
        <ul>
          <li><strong>Nom / Prénom :</strong> {raw_nom}</li>
          <li><strong>Catégorie :</strong> {categorie}</li>
          <li><strong>Horodatage :</strong> {timestamp_str}</li>
        </ul>
        <br/>
        <a href="{lien_validation}" 
           style="background-color: #16A34A; color: white; padding: 12px 24px; text-decoration: none; font-weight: bold; border-radius: 6px; display: inline-block;">
           ✅ Valider l'accès (Passer en PARENT)
        </a>
      </body>
    </html>
    """

    try:
        resend.Emails.send({
            "from": "FCVV App <onboarding@resend.dev>",
            "to": ADMIN_EMAIL,
            "subject": f"[FCVV] Inscription en attente : {raw_nom} ({categorie})",
            "html": html_content
        })
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

@app.get("/users/validate", response_class=HTMLResponse)
def valider_utilisateur_via_email(token: str = Query(...)):
    check_db()
    data = verifier_token_validation(token)
    if not data:
        return "<h1>❌ Lien invalide ou expiré</h1>"

    id_utilisateur = data["id"]
    categorie = data["cat"]
    doc_ref = db.collection("users").document(id_utilisateur)
    doc_snapshot = doc_ref.get()

    if not doc_snapshot.exists:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

    user_data = doc_snapshot.to_dict()
    roles_dict = user_data.get("roles_par_categorie", {})
    roles_dict[categorie] = "PARENT"

    doc_ref.update({"roles_par_categorie": roles_dict})

    return f"""
    <html>
        <body style="font-family: Arial; text-align: center; padding-top: 50px;">
            <h1 style="color: #16A34A;">✅ Accès Validé !</h1>
            <p>Le membre <strong>{user_data.get('nom', id_utilisateur)}</strong> est désormais <strong>PARENT</strong> pour la catégorie <strong>{categorie}</strong>.</p>
        </body>
    </html>
    """

@app.post("/users/register")
def register_user(user: dict, background_tasks: BackgroundTasks):

    check_db()

    raw_nom = user.get("nom", "").strip()
    categorie = user.get("categorie", "").strip()
    nouveau_joueur = user.get("joueur_associe", "").strip()

    if not raw_nom or not categorie:
        raise HTTPException(
            status_code=400,
            detail="Nom et Catégorie requis"
        )

    id_utilisateur = raw_nom.replace(" ", "_").lower()

    doc_ref = db.collection("users").document(id_utilisateur)
    doc_snapshot = doc_ref.get()

    est_premiere_demande = False

    # ==========================================================
    # 🟢 NOUVEL UTILISATEUR
    # ==========================================================
    if not doc_snapshot.exists:

        roles_dict = {
            categorie: "EXCLU"
        }

        joueurs_dict = {
            categorie: [nouveau_joueur] if nouveau_joueur else []
        }

        doc_ref.set({
            "nom": raw_nom,
            "roles_par_categorie": roles_dict,
            "joueurs_par_categorie": joueurs_dict,
            "created_at": firestore.SERVER_TIMESTAMP
        })

        est_premiere_demande = True

        print(
            f"[REGISTER] Nouveau parent={raw_nom} "
            f"categorie={categorie} "
            f"joueur={nouveau_joueur}"
        )

    # ==========================================================
    # 🟡 UTILISATEUR EXISTANT
    # ==========================================================
    else:

        data = doc_snapshot.to_dict()

        roles_dict = data.get("roles_par_categorie", {})
        joueurs_dict = data.get("joueurs_par_categorie", {})

        # ------------------------------------------------------
        # CAS 1 : catégorie déjà existante
        # ------------------------------------------------------
        if categorie in roles_dict:

            liste_joueurs_cat = joueurs_dict.get(categorie, [])

            # Sécurité : on s'assure d'avoir une vraie liste
            if not isinstance(liste_joueurs_cat, list):
                liste_joueurs_cat = []

            # Ajouter le joueur s'il est nouveau
            if nouveau_joueur and nouveau_joueur not in liste_joueurs_cat:

                liste_joueurs_cat.append(nouveau_joueur)

                joueurs_dict[categorie] = liste_joueurs_cat

                doc_ref.update({
                    "joueurs_par_categorie": joueurs_dict
                })

                print(
                    f"[REGISTER] Joueur ajouté : "
                    f"parent={raw_nom} "
                    f"categorie={categorie} "
                    f"joueur={nouveau_joueur}"
                )

            else:

                print(
                    f"[REGISTER] Aucun ajout : "
                    f"parent={raw_nom} "
                    f"categorie={categorie} "
                    f"joueur={nouveau_joueur}"
                )

        # ------------------------------------------------------
        # CAS 2 : catégorie nouvelle pour cet utilisateur
        # ------------------------------------------------------
        else:

            roles_dict[categorie] = "EXCLU"

            liste_joueurs_cat = []

            if nouveau_joueur:
                liste_joueurs_cat.append(nouveau_joueur)

            joueurs_dict[categorie] = liste_joueurs_cat

            doc_ref.update({
                "roles_par_categorie": roles_dict,
                "joueurs_par_categorie": joueurs_dict
            })

            est_premiere_demande = True

            print(
                f"[REGISTER] Nouvelle catégorie : "
                f"parent={raw_nom} "
                f"categorie={categorie} "
                f"joueur={nouveau_joueur}"
            )

    # ==========================================================
    # 📧 EMAIL : uniquement pour une première demande de catégorie
    # ==========================================================
    if est_premiere_demande:

        background_tasks.add_task(
            envoyer_email_notif_admin,
            raw_nom=raw_nom,
            categorie=categorie,
            id_utilisateur=id_utilisateur
        )

    return {
        "status": "success",
        "role": "EXCLU"
    }

# 🆕 AJOUT : Récupération du rôle d'un utilisateur pour une catégorie donnée
@app.get("/users")
def get_users(
    categorie: Optional[str] = None,
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()

    if not nom_parent:
        raise HTTPException(
            status_code=400,
            detail="Identifiant de l'utilisateur manquant"
        )

    if not categorie:
        raise HTTPException(
            status_code=400,
            detail="Catégorie manquante"
        )

    try:
        # Récupération du rôle réel dans Firebase
        id_utilisateur = nom_parent.strip().replace(" ", "_").lower()

        doc = db.collection("users").document(id_utilisateur).get()

        if not doc.exists:
            raise HTTPException(
                status_code=403,
                detail="Utilisateur inconnu"
            )

        data_utilisateur = doc.to_dict()
        roles = data_utilisateur.get("roles_par_categorie", {})

        role = str(
            roles.get(categorie, "EXCLU")
        ).strip().upper()

        print(
            f"[USERS GET] utilisateur={nom_parent} "
            f"categorie={categorie} role={role}"
        )

        # SEULS ADMIN et PARENT peuvent voir les membres
        if role not in ("ADMIN", "PARENT"):
            raise HTTPException(
                status_code=403,
                detail="Accès refusé pour ce rôle"
            )

        # Récupération des utilisateurs
        query = db.collection("users")
        docs = query.stream()

        results = []

        for doc in docs:
            data = doc.to_dict()

            roles_membre = data.get(
                "roles_par_categorie",
                {}
            )

            if categorie in roles_membre:
                results.append({
                    "id": doc.id,
                    **data
                })

        print(
            f"[USERS GET] {categorie} -> "
            f"{len(results)} membre(s)"
        )

        return results

    except HTTPException:
        raise

    except Exception as e:
        print(f"[ERREUR USERS GET] {e}")

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

##########################
######## GESTION NOTIFS & MODELS

class Vote(BaseModel):
    id_sondage: str
    nom_parent: str
    nom_joueur_concerne: Optional[str] = None
    choix: Optional[str] = None
    choix_trajet: Optional[str] = None
    second_vote: Optional[str] = None
    choix_multiple: Optional[str] = None
    nombre_de_places: Optional[int] = None

class NotifRequest(BaseModel):
    titre: str
    corps: str

class Message(BaseModel):
    auteur: str
    contenu: str
    role: Optional[str] = "PARENT"
    timestamp: Optional[datetime] = None

class EchangeMessage(BaseModel):
    contenu: str

class SondageModel(BaseModel):
    titre: str
    date: str
    heure: str
    lieu: str
    type: Optional[str] = "entrainement"
    sondage_actif: Optional[bool] = True

class ConvocationModel(BaseModel):
    type: Optional[str] = "MATCH"
    titre: Optional[str] = ""
    adversaire: Optional[str] = ""
    date: Optional[str] = ""
    heure_rdv: Optional[str] = ""
    heure_sur_place: Optional[str] = ""
    heure_coup_envoi: Optional[str] = ""
    heure: Optional[str] = ""
    lieu: Optional[str] = ""
    entraineurs: Optional[str] = ""
    notes: Optional[str] = ""
    sondage_classique: Optional[bool] = True
    sondage_trajet: Optional[bool] = False
    sondage_places: Optional[bool] = False
    titre_sondage_places: Optional[str] = "Places disponibles (départ Valdahon)"
    titre_second_vote: Optional[str] = "Second Vote"
    type_sondage: Optional[str] = "classique"
    titre_sondage_multiple: Optional[str] = "Choix multiple"
    options_sondage: List[str] = Field(default_factory=list)
    activer_convocation: Optional[bool] = False
    sondage_actif: Optional[bool] = True
    joueurs_convoques: List[Union[str, Dict[str, Any]]] = Field(default_factory=list)
    dernier_commit: Optional[str] = ""
    timestamp_action: Optional[str] = ""
    est_modification: Optional[bool] = False

class BatchConvocationModel(BaseModel):
    evenements: List[ConvocationModel]

def envoyer_notif_push(
    topic: str, 
    titre: str, 
    corps: str, 
    notif_type: str = "home", 
    match_id: str = None,
    sender: str = None
):
    topic = topic.strip()
    sender_clean = sender.strip().replace(" ", "_").lower() if sender else None

    try:
        android_config = messaging.AndroidConfig(priority="high")
        apns_config = messaging.APNSConfig(
            headers={"apns-priority": "10"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(alert=messaging.ApsAlert(title=titre, body=corps), sound="default")
            ),
        )

        target_page = "home" if notif_type in ["manual", "home"] else "vestiaire"
        data_payload = {
            "title": titre,
            "body": corps,
            "topic": topic,
            "open_page": target_page,
            "categorie": topic,
            "notif_type": notif_type,
            "sender": sender or ""
        }
        if match_id:
            data_payload["match_id"] = match_id

        # 🔒 Filtre FCM : Diffusion au topic sauf aux membres avec le statut EXCLU
        condition_fcm = f"'{topic}' in topics && !('{topic}_exclure' in topics)"
        if sender_clean:
            condition_fcm += f" && !('{topic}_exclure_{sender_clean}' in topics)"

        message = messaging.Message(
            data=data_payload,
            android=android_config,
            apns_config=apns_config,
            condition=condition_fcm,
        )

        response = messaging.send(message)
        print(f"[FCM API] envoye avec succes : {response}")

    except Exception as e:
        print(f"[FCM ERROR] {e}")

# --- Routes ---
@app.get("/")
def ping():
    return {"status": "ok", "message": "Server is awake"}

@app.post("/notifier/{categorie}")
def envoyer_notification_manuelle(
    categorie: str,
    notif: NotifRequest,
    nom_parent: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    if not nom_parent or not verifier_si_admin(nom_parent, categorie):
        raise HTTPException(status_code=403, detail="Accès refusé")

    try:
        envoyer_notif_push(
            topic=categorie, 
            titre=notif.titre, 
            corps=notif.corps, 
            notif_type="manual"
        )
        return {"status": "success", "message": "Notification envoyee avec succes"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/chat/{categorie}")
def get_messages(
    categorie: str, nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    # 🔒 Sécurité : Header obligatoire et vérification EXCLU
    if not nom_parent or verifier_si_exclu(nom_parent, categorie):
        return []

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
                else datetime.now(timezone.utc).isoformat()
            )
            results.append({"id": doc.id, **data})
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/echange/{categorie}")
def get_echange_messages(categorie: str, nom_parent: Optional[str] = Header(None, alias="nom_parent")):
    check_db()
    parent = (nom_parent or "").strip()

    # 🔒 Sécurité : Header obligatoire et vérification EXCLU
    if not parent or verifier_si_exclu(parent, categorie):
        return []

    try:
        docs = db.collection("echanges").document(categorie).collection("messages").order_by("timestamp", direction=firestore.Query.ASCENDING).limit(100).stream()
        
        return [
            {
                "id": doc.id,
                **doc.to_dict(),
                "timestamp": doc.to_dict().get("timestamp").isoformat() if hasattr(doc.to_dict().get("timestamp"), "isoformat") else datetime.now(timezone.utc).isoformat()
            }
            for doc in docs
        ]
    except Exception as e:
        print(f"[ERREUR ECHANGE GET] {e}")
        raise HTTPException(status_code=500, detail=str(e))

##########################
######## MESSAGERIE & CHAT

@app.post("/chat/{categorie}")
def poster_message(
    categorie: str, message: Message, background_tasks: BackgroundTasks
):
    check_db()
    if verifier_si_exclu(message.auteur, categorie):
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
            notif_type="chat",
            sender=message.auteur
        )

        return {"message": "Message envoyé avec succès"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/echange/{categorie}")
def poster_echange_message(
    categorie: str, 
    message: EchangeMessage, 
    background_tasks: BackgroundTasks, 
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    parent = (nom_parent or "").strip()
    if not parent:
        raise HTTPException(status_code=400, detail="Identifiant de l'utilisateur manquant")
    if verifier_si_exclu(parent, categorie):
        raise HTTPException(status_code=403, detail="Action interdite : compte exclu")

    contenu = message.contenu.strip()
    if not contenu:
        raise HTTPException(status_code=400, detail="Le message ne peut pas être vide")

    try:
        role = "PARENT"
        if verifier_si_admin(parent, categorie):
            role = "ADMIN"

        msg_data = {
            "auteur": parent, 
            "contenu": contenu, 
            "role": role, 
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        db.collection("echanges").document(categorie).collection("messages").add(msg_data)

        background_tasks.add_task(
            envoyer_notif_push, 
            categorie, 
            f"FCVV - Nouveau message ({categorie})", 
            f"{parent}: {contenu}", 
            notif_type="echange", 
            sender=parent
        )
        return {"status": "success", "message": "Message envoyé avec succès"}

    except Exception as e:
        print(f"[ERREUR ECHANGE POST] {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/echange/{categorie}/{message_id}")
def delete_echange_message(
    categorie: str, 
    message_id: str, 
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    parent = (nom_parent or "").strip()
    if not parent:
        raise HTTPException(status_code=400, detail="Identifiant de l'utilisateur manquant")
    if verifier_si_exclu(parent, categorie):
        raise HTTPException(status_code=403, detail="Action interdite : compte exclu")

    try:
        ref = db.collection("echanges").document(categorie).collection("messages").document(message_id)
        doc = ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Message non trouvé")

        est_admin = verifier_si_admin(parent, categorie)
        auteur = doc.to_dict().get("auteur", "").strip().lower()

        if not est_admin and auteur != parent.lower():
            raise HTTPException(status_code=403, detail="Vous ne pouvez supprimer que vos propres messages")

        ref.delete()
        return {"status": "deleted", "message": f"Message supprimé {'par un administrateur' if est_admin else 'avec succès'}"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERREUR ECHANGE DELETE] {e}")
        raise HTTPException(status_code=500, detail=str(e))

##########################
######## SONDAGES & VOTES

@app.get("/sondages/{categorie}")
def get_sondages_par_categorie(
    categorie: str, nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    # 🔒 Sécurité : Header obligatoire et vérification EXCLU
    if not nom_parent or verifier_si_exclu(nom_parent, categorie):
        return {}

    try:
        docs = db.collection(f"sondages_{categorie}").stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/voter/{categorie}")
def enregistrer_vote(
    categorie: str,
    vote: Vote,
    nom_parent_header: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    utilisateur_connecte = (nom_parent_header or vote.nom_parent or "").strip()

    if not utilisateur_connecte:
        raise HTTPException(
            status_code=400, detail="Identifiant de l'utilisateur manquant"
        )

    if verifier_si_exclu(utilisateur_connecte, categorie):
        raise HTTPException(status_code=403, detail="Action interdite : compte exclu")

    try:
        nom_identifiant_vote = (vote.nom_joueur_concerne or "").strip()

        if not nom_identifiant_vote:
            id_utilisateur = utilisateur_connecte.replace(" ", "_").lower()
            doc_user = db.collection("users").document(id_utilisateur).get()
            joueurs_lies = []
            if doc_user.exists:
                joueurs_par_cat = doc_user.to_dict().get("joueurs_par_categorie", {})
                joueurs_lies = joueurs_par_cat.get(categorie, [])

            nom_identifiant_vote = (
                joueurs_lies[0] if joueurs_lies else utilisateur_connecte
            )

        is_coach = str(nom_identifiant_vote).upper().startswith("COACH_")

        vote_updates = {
            f"votes.{nom_identifiant_vote}.dernier_modificateur": utilisateur_connecte,
            f"votes.{nom_identifiant_vote}.est_coach": is_coach,
        }

        if vote.choix is not None:
            vote_updates[f"votes.{nom_identifiant_vote}.disponibilite"] = vote.choix
        if vote.choix_trajet is not None:
            vote_updates[f"votes.{nom_identifiant_vote}.trajet"] = vote.choix_trajet
        if vote.second_vote is not None:
            vote_updates[f"votes.{nom_identifiant_vote}.second_vote"] = vote.second_vote
        if vote.choix_multiple is not None:
            vote_updates[f"votes.{nom_identifiant_vote}.choix_multiple"] = vote.choix_multiple
        if vote.nombre_de_places is not None:
            vote_updates[f"votes.{nom_identifiant_vote}.nombre_de_places"] = vote.nombre_de_places

        doc_ref = db.collection(f"convocations_{categorie}").document(vote.id_sondage)
        doc_ref.update(vote_updates)

        return {
            "message": "Vote mis à jour avec succès",
            "joueur": nom_identifiant_vote,
            "modifie_par": utilisateur_connecte,
        }

    except Exception as e:
        print(f"[ERREUR VOTE] {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sondages/create/{categorie}")
def create_sondage(
    categorie: str,
    sondage: SondageModel,
    background_tasks: BackgroundTasks,
    nom_parent: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    if not nom_parent or not verifier_si_admin(nom_parent, categorie):
        raise HTTPException(status_code=403, detail="Accès refusé")

    try:
        db.collection(f"sondages_{categorie}").add(sondage.model_dump())

        background_tasks.add_task(
            envoyer_notif_push,
            categorie,
            f"FCVV - Nouveau sondage ({categorie})",
            f"Sondage : {sondage.titre}",
            notif_type="evenement",
            sender=nom_parent
        )

        return {"status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/sondages/update/{categorie}/{sid}")
def update_sondage(
    categorie: str,
    sid: str,
    data: dict,
    nom_parent: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    if not nom_parent or not verifier_si_admin(nom_parent, categorie):
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
    categorie: str,
    sid: str,
    nom_parent: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    if not nom_parent or not verifier_si_admin(nom_parent, categorie):
        raise HTTPException(status_code=403, detail="Accès refusé")

    try:
        doc_ref = db.collection(f"sondages_{categorie}").document(sid)
        if not doc_ref.get().exists:
            raise HTTPException(status_code=404, detail="Sondage non trouvé")
        doc_ref.delete()
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

##########################
######## USERS MANAGEMENT

@app.post("/users/unregister")
def unregister_user(data: dict):
    check_db()
    raw_nom = data.get("nom", "").strip()
    categorie = data.get("categorie", "").strip()

    if not raw_nom:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur requis")

    id_utilisateur = raw_nom.replace(" ", "_").lower()
    doc_ref = db.collection("users").document(id_utilisateur)
    doc_snapshot = doc_ref.get()

    if not doc_snapshot.exists:
        return {"status": "not_found", "message": "Utilisateur non trouvé"}

    user_data = doc_snapshot.to_dict()
    roles_dict = user_data.get("roles_par_categorie", {})
    joueurs_dict = user_data.get("joueurs_par_categorie", {})

    if categorie:
        roles_dict.pop(categorie, None)
        joueurs_dict.pop(categorie, None)

    doc_ref.update(
        {
            "roles_par_categorie": roles_dict,
            "joueurs_par_categorie": joueurs_dict,
        }
    )

    return {
        "status": "unregistered",
        "message": f"Désinscription de la catégorie {categorie} effectuée.",
    }

@app.get("/users")
def get_users(
    categorie: Optional[str] = None,
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    if role not in ("ADMIN", "PARENT"):
        raise HTTPException(
            status_code=403,
            detail="Accès refusé pour ce rôle"
        )

    try:
        query = db.collection("users")
        docs = query.stream()
        results = []
        for doc in docs:
            data = doc.to_dict()
            if categorie:
                roles = data.get("roles_par_categorie", {})
                if categorie in roles:
                    results.append({"id": doc.id, **data})
            else:
                results.append({"id": doc.id, **data})
        return results
    except Exception as e:
        print(f"[ERREUR USERS GET] {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/users/role")
def get_user_role(
    categorie: str,
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()

    if not nom_parent:
        raise HTTPException(
            status_code=400,
            detail="Identifiant de l'utilisateur manquant"
        )

    if not categorie:
        raise HTTPException(
            status_code=400,
            detail="Catégorie manquante"
        )

    try:
        id_utilisateur = nom_parent.strip().replace(" ", "_").lower()

        doc = db.collection("users").document(id_utilisateur).get()

        if not doc.exists:
            raise HTTPException(
                status_code=404,
                detail="Utilisateur inconnu"
            )

        data = doc.to_dict()

        roles = data.get("roles_par_categorie", {})

        role = str(
            roles.get(categorie, "EXCLU")
        ).strip().upper()

        print(
            f"[ROLE GET] utilisateur={nom_parent} "
            f"categorie={categorie} "
            f"role={role}"
        )

        return {
            "nom_parent": nom_parent,
            "categorie": categorie,
            "role": role
        }

    except HTTPException:
        raise

    except Exception as e:
        print(f"[ERREUR ROLE GET] {e}")

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

##########################
######## CONVOCATIONS & EVENEMENTS

@app.put("/convocations/update/{categorie}/{match_id}")
def update_convocations(
    categorie: str,
    match_id: str,
    payload: ConvocationModel,
    background_tasks: BackgroundTasks,
    nom_parent: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    if not nom_parent or not verifier_si_admin(nom_parent, categorie):
        raise HTTPException(status_code=403, detail="Accès refusé")

    try:
        data_dict = payload.model_dump()
        type_evt = data_dict.get("type", "EVENEMENT").upper()
        date_brute = data_dict.get("date", "").replace("/", "-")

        est_un_nouveau = (
            not match_id
            or match_id == "Nouvel événement"
            or match_id.strip() == ""
        )
        type_incoherent = (
            type_evt == "MATCH" and not match_id.startswith("match_")
        ) or (
            type_evt == "ENTRAINEMENT"
            and not match_id.startswith("entrainement_")
        )

        if est_un_nouveau or type_incoherent:
            if type_evt == "MATCH":
                adversaire = (
                    data_dict.get("adversaire", "inconnu")
                    .strip()
                    .replace(" ", "_")
                    .lower()
                )
                heure_rdv = (
                    data_dict.get("heure_rdv", "").replace(":", "h") or "00h00"
                )
                nouveau_match_id = (
                    f"match_{adversaire}_{date_brute}_{heure_rdv}".strip("_")
                )
            elif type_evt == "ENTRAINEMENT":
                heure_ent = data_dict.get(
                    "heure", data_dict.get("heure_rdv", "")
                ).replace(":", "h") or "00h00"
                nouveau_match_id = (
                    f"entrainement_{date_brute}_{heure_ent}".strip("_")
                )
            else:
                titre_evt = (
                    data_dict.get("titre", "evt")
                    .strip()
                    .replace(" ", "_")
                    .lower()
                )
                nouveau_match_id = f"evt_{titre_evt}_{date_brute}".strip("_")

            if not est_un_nouveau and match_id and match_id != nouveau_match_id:
                try:
                    db.collection(f"convocations_{categorie}").document(
                        match_id
                    ).delete()
                except Exception:
                    pass
            match_id = nouveau_match_id

        doc_ref = db.collection(f"convocations_{categorie}").document(match_id)
        doc_ref.set(data_dict, merge=True)

        titre_evt = data_dict.get("titre", "")
        adversaire = data_dict.get("adversaire", "")
        date_evt = data_dict.get("date", "")
        est_mod = data_dict.get("est_modification", False)
        motif = data_dict.get("dernier_commit", "").strip()

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
                corps_notif = (
                    f"Nouvel entraînement : {nom_affiche} ({date_evt})".strip()
                )
                titre_notif = f"FCVV - Entraînement ({categorie})"
            elif type_evt == "MATCH":
                corps_notif = f"Match contre {nom_affiche} ({date_evt})".strip()
                titre_notif = f"FCVV - Nouvelle Convocation ({categorie})"
            else:
                corps_notif = f"Événement : {nom_affiche} ({date_evt})".strip()
                titre_notif = f"FCVV - Nouvel Événement ({categorie})"

        background_tasks.add_task(
            envoyer_notif_push, categorie, titre_notif, corps_notif, notif_type="evenement", match_id=match_id, sender=nom_parent
        )

        return {"status": "updated", "id": match_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/convocations/batch-update/{categorie}")
def batch_update_convocations(
    categorie: str,
    payload: BatchConvocationModel,
    background_tasks: BackgroundTasks,
    nom_parent: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    if not nom_parent or not verifier_si_admin(nom_parent, categorie):
        raise HTTPException(status_code=403, detail="Accès refusé")

    try:
        batch = db.batch()
        nb_evenements = len(payload.evenements)
        
        if nb_evenements == 0:
            return {"status": "updated", "count": 0}

        premiere_date = ""
        derniere_date = ""

        for idx, evt in enumerate(payload.evenements):
            data_dict = evt.model_dump()
            date_brute = data_dict.get("date", "").replace("/", "-")
            date_evt = data_dict.get("date", "")

            if idx == 0:
                premiere_date = date_evt
            if idx == nb_evenements - 1:
                derniere_date = date_evt

            heure_ent = data_dict.get("heure", data_dict.get("heure_rdv", "")).replace(":", "h") or "00h00"
            match_id = f"entrainement_{date_brute}_{heure_ent}".strip("_")

            doc_ref = db.collection(f"convocations_{categorie}").document(match_id)
            batch.set(doc_ref, data_dict, merge=True)

        batch.commit()

        titre_notif = f"FCVV - Entraînements ({categorie})"
        if nb_evenements == 1:
            corps_notif = f"1 nouvel entraînement a été planifié pour le {premiere_date}."
        else:
            corps_notif = f"{nb_evenements} nouveaux entraînements planifiés (du {premiere_date} au {derniere_date})."

        background_tasks.add_task(
            envoyer_notif_push, categorie, titre_notif, corps_notif, notif_type="evenement", sender=nom_parent
        )

        return {"status": "updated", "count": nb_evenements}

    except Exception as e:
        print(f"[ERREUR BATCH ENTRAINEMENTS] {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/convocations/delete/{categorie}/{match_id}")
def delete_convocation(
    categorie: str,
    match_id: str,
    nom_parent: Optional[str] = Header(None, alias="nom_parent"),
):
    check_db()
    if not nom_parent or not verifier_si_admin(nom_parent, categorie):
        raise HTTPException(status_code=403, detail="Accès refusé")

    try:
        db.collection(f"convocations_{categorie}").document(match_id).delete()
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/convocations/{categorie}")
def get_convocations(
    categorie: str, 
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    # 🔒 Sécurité : Header obligatoire et vérification EXCLU
    if not nom_parent or verifier_si_exclu(nom_parent, categorie):
        return {}

    docs = db.collection(f"convocations_{categorie}").stream()
    return {doc.id: doc.to_dict() for doc in docs}

@app.get("/convocations/{categorie}/{match_id}")
def get_one_convocation(
    categorie: str, 
    match_id: str,
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    # 🔒 Sécurité : Vérification du statut EXCLU pour l'événement unitaire
    if not nom_parent or verifier_si_exclu(nom_parent, categorie):
        raise HTTPException(status_code=403, detail="Accès refusé : compte exclu ou non identifié")

    doc = db.collection(f"convocations_{categorie}").document(match_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Match non trouvé")
    return doc.to_dict()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)