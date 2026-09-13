import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Union, Dict, Any

import hashlib
import hmac

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

ADMIN_API_TOKEN_SHA256 = os.getenv("ADMIN_API_TOKEN")

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

def verifier_si_autorise(nom_parent: str, categorie: str) -> bool:
    """Autorise uniquement les utilisateurs PARENT ou ADMIN."""
    if not nom_parent or db is None:
        return False
    id_utilisateur = nom_parent.strip().replace(" ", "_").lower()
    doc = db.collection("users").document(id_utilisateur).get()
    if not doc.exists:
        return False
    roles = doc.to_dict().get("roles_par_categorie", {})
    if not isinstance(roles, dict):
        return False
    role = str(roles.get(categorie, "EXCLU")).strip().upper()
    return role in ("PARENT", "ADMIN")

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

def envoyer_email_notif_admin(raw_nom: str, categorie: str, id_utilisateur: str,demande_admin: bool = False):
    timestamp_str = datetime.now(timezone.utc).strftime("%d/%m/%Y à %H:%M:%S (UTC)")
    token = generer_token_validation(id_utilisateur, categorie)
    lien_validation = f"{API_BASE_URL}/users/validate?token={token}"
    role_demande = "ADMIN" if demande_admin else "PARENT"

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
        
           ✅ Valider l'accès (Passer en {role_demande})
        
        </a>
      </body>
    </html>
    """

    try:
        resend.Emails.send({
            "from": "FCVV App <onboarding@resend.dev>",
            "to": ADMIN_EMAIL,
            "subject": f"[FCVV] Inscription en attente : {raw_nom} ({categorie}) - {role_demande}",
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
    roles_dict = user_data.get("roles_par_categorie",{})
    if not isinstance(roles_dict, dict):
        roles_dict = {}
    demandes_admin_dict = user_data.get("demandes_admin_par_categorie",{})
    if not isinstance(demandes_admin_dict, dict):
        demandes_admin_dict = {}
    demande_admin = demandes_admin_dict.get(categorie,False)
    role_final = "ADMIN" if demande_admin else "PARENT"
    roles_dict[categorie] = role_final
    doc_ref.update({"roles_par_categorie": roles_dict})
    # ==========================================================
    # 🔔 NOTIFICATION CIBLÉE DU PARENT
    # ==========================================================
    fcm_tokens = user_data.get("fcm_tokens",[])
    if not isinstance(fcm_tokens, list):
        fcm_tokens = []
    fcm_tokens = [
        str(token).strip()
        for token in fcm_tokens
        if str(token).strip()
    ]
    nom_utilisateur = user_data.get("nom",id_utilisateur)
    titre_push = "FCVV - Accès validé"
    corps_push = (
        f"Votre demande d'accès à la catégorie "
        f"{categorie} a été validée."
    )
    print(
        f"[FCM VALIDATION] "
        f"Parent={nom_utilisateur} | "
        f"categorie={categorie} | "
        f"role={role_final} | "
        f"tokens={len(fcm_tokens)}"
    )
    for fcm_token in fcm_tokens:
        envoyer_notif_push_token(
            fcm_token=fcm_token,
            titre=titre_push,
            corps=corps_push,
            categorie=categorie,
            notif_type="validation"
        )

    return f"""
    <html>
        <body style="font-family: Arial; text-align: center; padding-top: 50px;">
            <h1 style="color: #16A34A;">✅ Accès Validé !</h1>
    
            <p>
                Le membre <strong>{user_data.get('nom', id_utilisateur)}</strong>
                est désormais
                <strong>{role_final}</strong>
                pour la catégorie <strong>{categorie}</strong>.
            </p>
        </body>
    </html>
    """

@app.post("/users/register")
def register_user(user: dict, background_tasks: BackgroundTasks):
    check_db()
    # ==========================================================
    # DONNÉES REÇUES
    # ==========================================================
    fcm_token = user.get("fcm_token")
    if fcm_token:
        fcm_token = str(fcm_token).strip()
    if not fcm_token:
        fcm_token = None
    raw_nom = user.get("nom", "").strip()
    categorie = user.get("categorie", "").strip()
    demande_admin = bool(user.get("demande_admin", False))
    # Ancien format : un seul joueur
    nouveau_joueur = user.get("joueur_associe","").strip()
    # Nouveau format : plusieurs joueurs
    joueurs_associes = user.get("joueurs_associes",[])
    # Sécurité : on s'assure que c'est bien une liste
    if not isinstance(joueurs_associes, list):
        joueurs_associes = []
    # Nettoyage de la liste
    joueurs_associes = [
        str(j).strip()
        for j in joueurs_associes
        if str(j).strip()
    ]
    # ==========================================================
    # COMPATIBILITÉ AVEC L'ANCIEN FORMAT
    #
    # Si le client envoie encore :
    # "joueur_associe": "COULOT Quentin"
    #
    # on transforme automatiquement en :
    # "joueurs_associes": ["COULOT Quentin"]
    # ==========================================================
    if not joueurs_associes and nouveau_joueur:
        joueurs_associes = [
            nouveau_joueur
        ]
    # ==========================================================
    # VALIDATION
    # ==========================================================
    if not raw_nom or not categorie:
        raise HTTPException(status_code=400,detail="Nom et Catégorie requis")
    # ==========================================================
    # IDENTIFIANT FIRESTORE
    #
    # Exemple :
    # "Quentin Dominati"
    # devient :
    # "quentin_dominati"
    # ==========================================================
    id_utilisateur = (raw_nom.replace(" ", "_").lower())
    doc_ref = db.collection("users").document(id_utilisateur)
    doc_snapshot = doc_ref.get()
    est_premiere_demande = False
    # ==========================================================
    # 🟢 CAS 1 : NOUVEL UTILISATEUR
    # ==========================================================
    if not doc_snapshot.exists:
        roles_dict = {categorie: "ATTENTE"}
        joueurs_dict = {categorie: joueurs_associes}
        doc_data = {
            "nom": raw_nom,
            "roles_par_categorie": roles_dict,
            "joueurs_par_categorie": joueurs_dict,
            "demandes_admin_par_categorie": {
                categorie: demande_admin
            },
            "created_at": firestore.SERVER_TIMESTAMP
        }
        if fcm_token:
            doc_data["fcm_tokens"] = [fcm_token]
        doc_ref.set(doc_data)
        est_premiere_demande = True
        print(
            f"[REGISTER] Nouveau parent="
            f"{raw_nom} | "
            f"categorie={categorie} | "
            f"joueurs={joueurs_associes}"
        )
    # ==========================================================
    # 🟡 CAS 2 : UTILISATEUR EXISTANT
    # ==========================================================
    else:
        data = doc_snapshot.to_dict()
        roles_dict = data.get("roles_par_categorie",{})
        joueurs_dict = data.get("joueurs_par_categorie",{})
        # Sécurité supplémentaire :
        # on s'assure que les dictionnaires sont bien des dicts.
        if not isinstance(roles_dict, dict):
            roles_dict = {}
        if not isinstance(joueurs_dict, dict):
            joueurs_dict = {}
        # ======================================================
        # 🔴 CAS 2A : CATÉGORIE DÉJÀ EXISTANTE
        #
        # TOUTE nouvelle demande pour cette catégorie
        # est considérée comme une tentative d'usurpation.
        #
        # Peu importe :
        # - le joueur demandé
        # - si le joueur est déjà associé
        # - si la liste est identique
        # - si la liste est différente
        # ======================================================
        if categorie in roles_dict:
            liste_joueurs_existants = joueurs_dict.get(categorie,[])
            if not isinstance(
                liste_joueurs_existants,
                list
            ):
                liste_joueurs_existants = []
            print(
                f"[SECURITE]"
                f"TENTATIVE D'USURPATION POSSIBLE : "
                f"parent={raw_nom} | "
                f"categorie={categorie} | "
                f"joueurs_demandes={joueurs_associes} | "
                f"joueurs_deja_associes="
                f"{liste_joueurs_existants}"
            )
            raise HTTPException(status_code=403,detail="USURPATION_IDENTITE")
        # ======================================================
        # 🟢 CAS 2B : NOUVELLE CATÉGORIE
        #
        # L'utilisateur existe déjà mais cette catégorie
        # n'existe pas encore dans son compte.
        #
        # La nouvelle demande est donc autorisée.
        # ======================================================
        else:
            roles_dict[categorie] = "ATTENTE"
            joueurs_dict[categorie] = joueurs_associes
            demandes_admin_dict = data.get(
                "demandes_admin_par_categorie",
                {}
            )
            if not isinstance(demandes_admin_dict, dict):
                demandes_admin_dict = {}
            demandes_admin_dict[categorie] = demande_admin
            update_data = {
                "roles_par_categorie": roles_dict,
                "joueurs_par_categorie": joueurs_dict,
                "demandes_admin_par_categorie": demandes_admin_dict
            }
            if fcm_token:
                tokens_existants = data.get("fcm_tokens", [])
            
                if not isinstance(tokens_existants, list):
                    tokens_existants = []
            
                tokens_existants = [
                    str(token).strip()
                    for token in tokens_existants
                    if str(token).strip()
                ]
                if fcm_token not in tokens_existants:
                    tokens_existants.append(fcm_token)
                update_data["fcm_tokens"] = tokens_existants
            doc_ref.update(update_data)
            est_premiere_demande = True
            print(
                f"[REGISTER] Nouvelle categorie : "
                f"parent={raw_nom} | "
                f"categorie={categorie} | "
                f"joueurs={joueurs_associes}"
            )
    # ==========================================================
    # 📧 EMAIL ADMIN
    #
    # Uniquement lorsqu'il s'agit réellement d'une nouvelle
    # inscription ou d'une nouvelle catégorie.
    #
    # Un seul email, même avec plusieurs joueurs.
    # ==========================================================
    if est_premiere_demande:
        background_tasks.add_task(
            envoyer_email_notif_admin,
            raw_nom=raw_nom,
            categorie=categorie,
            id_utilisateur=id_utilisateur,
            demande_admin=demande_admin
        )
        print(
            f"[REGISTER]"
            f"Notification admin programmee : "
            f"parent={raw_nom} | "
            f"categorie={categorie}",
            f"demande_admin={demande_admin}"
        )
    # ==========================================================
    # ✅ RÉPONSE API
    # ==========================================================
    return {
        "status": "success",
        "role": "ATTENTE"
    }
    
@app.post("/users/fcm-token")
def enregistrer_fcm_token(user: dict):
    check_db()
    # ==========================================================
    # DONNÉES REÇUES
    # ==========================================================
    raw_nom = str(user.get("nom", "")).strip()
    fcm_token = str(user.get("fcm_token", "")).strip()
    if not raw_nom:
        raise HTTPException(
            status_code=400,
            detail="Nom requis"
        )
    if not fcm_token:
        raise HTTPException(status_code=400,detail="Token FCM requis")
    # ==========================================================
    # IDENTIFIANT FIRESTORE
    # ==========================================================
    id_utilisateur = (raw_nom.replace(" ", "_").lower())
    doc_ref = db.collection("users").document(id_utilisateur)
    doc_snapshot = doc_ref.get()
    if not doc_snapshot.exists:
        raise HTTPException(status_code=404,detail="Utilisateur non trouvé")
    # ==========================================================
    # RÉCUPÉRATION DES TOKENS EXISTANTS
    # ==========================================================
    data = doc_snapshot.to_dict()
    tokens_existants = data.get("fcm_tokens",[])
    if not isinstance(tokens_existants, list):
        tokens_existants = []
    tokens_existants = [
        str(token).strip()
        for token in tokens_existants
        if str(token).strip()
    ]
    # ==========================================================
    # AJOUT DU NOUVEAU TOKEN
    # ==========================================================
    if fcm_token not in tokens_existants:
        tokens_existants.append(fcm_token)
        doc_ref.update({"fcm_tokens": tokens_existants})
        print(
            f"[FCM TOKEN] Nouveau token enregistre : "
            f"{raw_nom} -> {fcm_token[:25]}..."
        )
    else:
        print(
            f"[FCM TOKEN] Token deja enregistre : "
            f"{raw_nom}"
        )
    return {
        "status": "success",
        "tokens_count": len(tokens_existants)
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
        raise HTTPException(status_code=400,detail="Catégorie manquante")
    try:
        # Récupération du rôle réel dans Firebase
        id_utilisateur = nom_parent.strip().replace(" ", "_").lower()
        doc = db.collection("users").document(id_utilisateur).get()
        if not doc.exists:
            raise HTTPException(status_code=403,detail="Utilisateur inconnu")
        data_utilisateur = doc.to_dict()
        roles = data_utilisateur.get("roles_par_categorie", {})
        role = str(roles.get(categorie, "EXCLU")).strip().upper()
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
        raise HTTPException(status_code=500,detail=str(e))

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
            apns=apns_config,
            condition=condition_fcm,
        )
        response = messaging.send(message)
        print(f"[FCM API] envoye avec succes : {response}")
    except Exception as e:
        print(f"[FCM ERROR] {e}")

def envoyer_notif_push_token(
    fcm_token: str,
    titre: str,
    corps: str,
    categorie: str,
    notif_type: str = "validation"
):
    if not fcm_token:
        print(
            "[FCM TOKEN] Aucun token fourni -> "
            "notification non envoyee."
        )
        return False

    try:
        # ---------------------------------------------------------
        # Configuration Android
        # ---------------------------------------------------------
        android_config = messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                icon="ic_notification",
                channel_id="fcvv_high_priority_v2"
            )
        )

        # ---------------------------------------------------------
        # Configuration APNS (iOS)
        # ---------------------------------------------------------
        apns_config = messaging.APNSConfig(
            headers={
                "apns-priority": "10"
            },
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=titre,
                        body=corps
                    ),
                    sound="default"
                )
            )
        )

        # ---------------------------------------------------------
        # Data Payload
        # ---------------------------------------------------------
        data_payload = {
            "title": titre,
            "body": corps,
            "categorie": categorie,
            "notif_type": notif_type,
            "open_page": "vestiaire"
        }

        # ---------------------------------------------------------
        # Message FCM
        #
        # notification :
        #   permet à Android d'afficher la notification lorsque
        #   l'application est fermée / en arrière-plan.
        #
        # data :
        #   conserve les informations nécessaires à l'application
        #   pour la redirection et le traitement.
        # ---------------------------------------------------------
        message = messaging.Message(
            notification=messaging.Notification(
                title=titre,
                body=corps
            ),
            data=data_payload,
            android=android_config,
            apns=apns_config,
            token=fcm_token
        )

        # ---------------------------------------------------------
        # Envoi FCM
        # ---------------------------------------------------------
        response = messaging.send(message)

        print(
            f"[FCM TOKEN] Notification envoyee | "
            f"token={fcm_token[:30]}... | "
            f"message_id={response}"
        )

        return True

    except Exception as e:
        print(
            f"[FCM TOKEN ERROR] "
            f"Impossible d'envoyer la notification : {e}"
        )
        return False

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

def verifier_token_admin(x_admin_token: Optional[str]) -> bool:
    """
    Vérifie la clé utilisée par le logiciel admin.
    Le serveur ne connaît que le SHA-256 de la clé.
    """
    if not ADMIN_API_TOKEN_SHA256:
        print("[ADMIN AUTH] ADMIN_API_TOKEN_SHA256 non configure.")
        return False
    if not x_admin_token:
        print("[ADMIN AUTH] Aucun X-Admin-Token recu.")
        return False
    hash_recu = hashlib.sha256(
        x_admin_token.encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(
        hash_recu,
        ADMIN_API_TOKEN_SHA256
    )

@app.post("/admin/notifier/{categorie}")
def envoyer_notification_admin(
    categorie: str,
    notif: NotifRequest,
    x_admin_token: Optional[str] = Header(
        None,
        alias="X-Admin-Token"
    ),
):
    check_db()
    if not verifier_token_admin(x_admin_token):
        raise HTTPException(
            status_code=403,
            detail="Accès admin refusé"
        )
    try:
        envoyer_notif_push(
            topic=categorie,
            titre=notif.titre,
            corps=notif.corps,
            notif_type="manual"
        )
        return {
            "status": "success",
            "message": "Notification envoyée avec succès"
        }
    except Exception as e:
        print(f"[ADMIN NOTIF ERROR] {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.get("/chat/{categorie}")
def get_messages(
    categorie: str, nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    # 🔒 Sécurité : Header obligatoire et vérification EXCLU
    if not nom_parent or not verifier_si_autorise(nom_parent, categorie):
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
    if not parent or not verifier_si_autorise(parent, categorie):
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
    if not verifier_si_autorise(message.auteur, categorie):
        raise HTTPException(
            status_code=403,
            detail="Action interdite : accès non validé"
        )
    try:
        msg_data = {
            "auteur": message.auteur,
            "contenu": message.contenu,
            "role": message.role,
            "timestamp": firestore.SERVER_TIMESTAMP,
        }
        db.collection("chats").document(categorie).collection("messages").add(msg_data)
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
    if not verifier_si_autorise(parent, categorie):
        raise HTTPException(
            status_code=403,
            detail="Action interdite : accès non validé"
        )
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
    if not verifier_si_autorise(parent, categorie):
        raise HTTPException(
            status_code=403,
            detail="Action interdite : accès non validé"
        )
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
    if not nom_parent or not verifier_si_autorise(nom_parent, categorie):
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
    if not verifier_si_autorise(utilisateur_connecte, categorie):
        raise HTTPException(
            status_code=403,
            detail="Action interdite : accès non validé"
        )
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
        
        # Génération de la date et heure actuelle au même format que le reste de votre appli
        date_heure_actuelle = datetime.now().strftime("%d/%m/%Y à %H:%M")

        vote_updates = {
            f"votes.{nom_identifiant_vote}.dernier_modificateur": utilisateur_connecte,
            f"votes.{nom_identifiant_vote}.est_coach": is_coach,
            f"votes.{nom_identifiant_vote}.timestamp": date_heure_actuelle,  # <--- Ajout de l'horodatage ici
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

@app.get("/users/role")
def get_user_role(
    categorie: str,
    nom: Optional[str] = None,
    nom_parent: Optional[str] = Header(None, alias="nom_parent")
):
    check_db()
    # Accepte soit ?nom=..., soit le header nom_parent
    utilisateur = nom or nom_parent
    if not utilisateur:
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
        id_utilisateur = (
            utilisateur.strip()
            .replace(" ", "_")
            .lower()
        )
        doc = db.collection("users").document(id_utilisateur).get()
        if not doc.exists:
            raise HTTPException(
                status_code=404,
                detail="Utilisateur inconnu"
            )
        data = doc.to_dict()
        roles = data.get("roles_par_categorie", {})
        role = str(roles.get(categorie, "EXCLU")).strip().upper()
        print(
            f"[ROLE GET] utilisateur={utilisateur} "
            f"categorie={categorie} "
            f"role={role}"
        )
        return {
            "nom_parent": utilisateur,
            "categorie": categorie,
            "role": role
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERREUR ROLE GET] {e}")
        raise HTTPException(status_code=500,detail=str(e))
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

        date_evt = str(
            data_dict.get("date", "")
        ).strip()

        date_brute = date_evt.replace("/", "-")

        adversaire = str(
            data_dict.get("adversaire", "")
        ).strip()

        est_un_nouveau = (
            not match_id
            or match_id == "Nouvel événement"
            or match_id.strip() == ""
        )

        type_incoherent = (
            type_evt == "MATCH"
            and not match_id.startswith("match_")
        ) or (
            type_evt == "ENTRAINEMENT"
            and not match_id.startswith("entrainement_")
        )

        # ==========================================================
        # 🔄 GÉNÉRATION / CORRECTION DU MATCH_ID
        # ==========================================================

        if est_un_nouveau or type_incoherent:

            if type_evt == "MATCH":

                adversaire_clean = (
                    adversaire
                    or "inconnu"
                ).replace(" ", "_").lower()

                heure_rdv = str(
                    data_dict.get("heure_rdv", "")
                ).strip().replace(":", "h") or "00h00"

                nouveau_match_id = (
                    f"match_{adversaire_clean}_{date_brute}_{heure_rdv}"
                ).strip("_")

            elif type_evt == "ENTRAINEMENT":

                heure_ent = str(
                    data_dict.get(
                        "heure",
                        data_dict.get("heure_rdv", "")
                    )
                ).strip().replace(":", "h") or "00h00"

                nouveau_match_id = (
                    f"entrainement_{date_brute}_{heure_ent}"
                ).strip("_")

            else:

                titre_evt_temp = str(
                    data_dict.get("titre", "evt")
                ).strip().replace(" ", "_").lower()

                nouveau_match_id = (
                    f"evt_{titre_evt_temp}_{date_brute}"
                ).strip("_")

            # Si changement d'identifiant, suppression de l'ancien document
            if (
                not est_un_nouveau
                and match_id
                and match_id != nouveau_match_id
            ):
                try:
                    db.collection(
                        f"convocations_{categorie}"
                    ).document(match_id).delete()
                except Exception:
                    pass

            match_id = nouveau_match_id

        # ==========================================================
        # 📄 DOCUMENT CIBLE
        # ==========================================================

        doc_ref = db.collection(
            f"convocations_{categorie}"
        ).document(match_id)

        # ==========================================================
        # 🔎 RÉCUPÉRATION DE L'ANCIENNE CONVOCATION
        # ==========================================================

        ancien_data = {}

        try:
            ancien_snapshot = doc_ref.get()

            if ancien_snapshot.exists:
                ancien_data = (
                    ancien_snapshot.to_dict()
                    or {}
                )

        except Exception as e:

            print(
                "[FCM CONVOCATION] "
                f"Impossible de récupérer l'ancienne convocation : {e}"
            )

        anciens_joueurs_convoques = ancien_data.get(
            "joueurs_convoques",
            []
        )

        if not isinstance(
            anciens_joueurs_convoques,
            list
        ):
            anciens_joueurs_convoques = []

        # ==========================================================
        # 💾 SAUVEGARDE FIRESTORE
        # ==========================================================

        doc_ref.set(
            data_dict,
            merge=True
        )

        # ==========================================================
        # 🔔 NOTIFICATIONS CIBLÉES
        #    UNIQUEMENT POUR LES NOUVEAUX CONVOQUÉS
        # ==========================================================

        if type_evt == "MATCH":

            activer_convocation = bool(
                data_dict.get(
                    "activer_convocation",
                    False
                )
            )

            joueurs_actuels = data_dict.get(
                "joueurs_convoques",
                []
            )

            if not isinstance(
                joueurs_actuels,
                list
            ):
                joueurs_actuels = []

            # ------------------------------------------------------
            # Normalisation joueur
            # ------------------------------------------------------

            def normaliser_joueur(joueur):

                if isinstance(joueur, dict):

                    nom = str(
                        joueur.get("nom", "")
                    ).strip()

                    prenom = str(
                        joueur.get("prenom", "")
                    ).strip()

                    return (
                        f"{nom} {prenom}"
                    ).strip().casefold()

                return str(
                    joueur
                ).strip().casefold()

            # ------------------------------------------------------
            # Anciens joueurs
            # ------------------------------------------------------

            anciens_identites = set()

            for joueur in anciens_joueurs_convoques:

                identite = normaliser_joueur(
                    joueur
                )

                if identite:
                    anciens_identites.add(
                        identite
                    )

            # ------------------------------------------------------
            # Détection des NOUVEAUX convoqués
            # ------------------------------------------------------

            joueurs_nouvellement_convoques = []

            for joueur in joueurs_actuels:

                identite = normaliser_joueur(
                    joueur
                )

                if not identite:
                    continue

                if identite not in anciens_identites:

                    joueurs_nouvellement_convoques.append(
                        joueur
                    )

            print(
                "[FCM CONVOCATION] "
                f"match={match_id} | "
                f"convocation_active={activer_convocation} | "
                f"anciens={len(anciens_joueurs_convoques)} | "
                f"actuels={len(joueurs_actuels)} | "
                f"nouveaux={len(joueurs_nouvellement_convoques)}"
            )

            # ------------------------------------------------------
            # Envoi uniquement si les convocations sont actives
            # ------------------------------------------------------

            if (
                activer_convocation
                and joueurs_nouvellement_convoques
            ):

                titre_convocation = (
                    f"FCVV - Convocation ({categorie})"
                )

                adversaire_affiche = (
                    adversaire
                    or "match"
                )

                date_affichee = (
                    date_evt
                    or "date à confirmer"
                )

                heure_affichee = str(
                    data_dict.get(
                        "heure_sur_place",
                        ""
                    )
                ).strip()

                if not heure_affichee:

                    heure_affichee = str(
                        data_dict.get(
                            "heure_rdv",
                            ""
                        )
                    ).strip()

                # --------------------------------------------------
                # Préparation des informations du match
                # --------------------------------------------------

                corps_base = (
                    f"est convoqué pour le match "
                    f"contre {adversaire_affiche} "
                    f"le {date_affichee}"
                )

                if heure_affichee:
                    corps_base += (
                        f" à {heure_affichee}"
                    )

                # --------------------------------------------------
                # Fonction d'envoi en arrière-plan
                # --------------------------------------------------

                def envoyer_notifications_nouveaux_convoques():

                    tokens_deja_notifies = set()

                    for joueur in joueurs_nouvellement_convoques:

                        # ------------------------------------------
                        # Récupération nom + catégorie
                        # ------------------------------------------

                        if isinstance(joueur, dict):

                            nom = str(
                                joueur.get(
                                    "nom",
                                    ""
                                )
                            ).strip()

                            prenom = str(
                                joueur.get(
                                    "prenom",
                                    ""
                                )
                            ).strip()

                            nom_recherche = (
                                f"{nom} {prenom}"
                            ).strip()

                            categorie_joueur = str(
                                joueur.get(
                                    "categorie",
                                    categorie
                                )
                            ).strip() or categorie

                        else:

                            nom_recherche = str(
                                joueur
                            ).strip()

                            categorie_joueur = categorie

                        if not nom_recherche:
                            continue

                        # ------------------------------------------
                        # Recherche des tokens des parents
                        # ------------------------------------------

                        tokens = (
                            recuperer_tokens_fcm_pour_joueur(
                                joueur_nom=nom_recherche,
                                categorie=categorie_joueur
                            )
                        )

                        if not tokens:

                            print(
                                "[FCM CONVOCATION] "
                                f"Aucun token pour "
                                f"{nom_recherche} "
                                f"(categorie={categorie_joueur})"
                            )

                            continue

                        # ------------------------------------------
                        # Notification personnalisée au parent
                        # ------------------------------------------

                        corps_convocation = (
                            f"{nom_recherche} "
                            f"{corps_base}."
                        )

                        for fcm_token in tokens:

                            if fcm_token in tokens_deja_notifies:
                                continue

                            succes = (
                                envoyer_notif_convocation_token(
                                    fcm_token=fcm_token,
                                    titre=titre_convocation,
                                    corps=corps_convocation,
                                    categorie=categorie,
                                    match_id=match_id,
                                )
                            )

                            if succes:
                                tokens_deja_notifies.add(
                                    fcm_token
                                )

                    print(
                        "[FCM CONVOCATION] "
                        f"Notifications terminées pour "
                        f"match={match_id} | "
                        f"tokens_notifies="
                        f"{len(tokens_deja_notifies)}"
                    )

                # ----------------------------------------------
                # Exécution en arrière-plan
                # ----------------------------------------------

                background_tasks.add_task(
                    envoyer_notifications_nouveaux_convoques
                )

        # ==========================================================
        # 🔔 NOTIFICATION GÉNÉRALE EXISTANTE
        # ==========================================================

        titre_evt = str(
            data_dict.get(
                "titre",
                ""
            )
        ).strip()

        adversaire = str(
            data_dict.get(
                "adversaire",
                ""
            )
        ).strip()

        date_evt = str(
            data_dict.get(
                "date",
                ""
            )
        ).strip()

        est_mod = data_dict.get(
            "est_modification",
            False
        )

        motif = str(
            data_dict.get(
                "dernier_commit",
                ""
            )
        ).strip()

        # Si c'est une modification et que le dernier commit
        # est vide, on n'envoie pas la notification générale.

        if est_mod and not motif:

            print(
                "[NOTIF] Modification ignoree "
                f"(dernier_commit vide) pour {match_id}"
            )

        else:

            if type_evt == "ENTRAINEMENT":

                nom_affiche = (
                    titre_evt
                    if titre_evt
                    else "Entraînement"
                )

                type_libelle = (
                    "l'entraînement"
                )

            elif type_evt == "MATCH":

                nom_affiche = (
                    adversaire
                    if adversaire
                    else match_id
                )

                type_libelle = (
                    f"le match contre {nom_affiche}"
                )

            else:

                nom_affiche = (
                    titre_evt
                    if titre_evt
                    else match_id
                )

                type_libelle = (
                    f"l'événement {nom_affiche}"
                )

            # ------------------------------------------------------
            # Modification
            # ------------------------------------------------------

            if est_mod:

                titre_notif = (
                    f"FCVV - Modification ({categorie})"
                )

                corps_notif = (
                    f"Modification concernant "
                    f"{type_libelle} "
                    f"({date_evt})."
                )

                if motif:

                    corps_notif += (
                        f"\nMotif : {motif}"
                    )

            # ------------------------------------------------------
            # Nouvelle création
            # ------------------------------------------------------

            else:

                if type_evt == "ENTRAINEMENT":

                    corps_notif = (
                        f"Nouvel entraînement : "
                        f"{nom_affiche} "
                        f"({date_evt})"
                    ).strip()

                    titre_notif = (
                        f"FCVV - Entraînement ({categorie})"
                    )

                elif type_evt == "MATCH":

                    corps_notif = (
                        f"Match contre "
                        f"{nom_affiche} "
                        f"({date_evt})"
                    ).strip()

                    titre_notif = (
                        f"FCVV - Nouvelle Convocation "
                        f"({categorie})"
                    )

                else:

                    corps_notif = (
                        f"Événement : "
                        f"{nom_affiche} "
                        f"({date_evt})"
                    ).strip()

                    titre_notif = (
                        f"FCVV - Nouvel Événement "
                        f"({categorie})"
                    )

            background_tasks.add_task(
                envoyer_notif_push,
                categorie,
                titre_notif,
                corps_notif,
                notif_type="evenement",
                match_id=match_id,
                sender=nom_parent
            )

        # ==========================================================
        # ✅ RÉPONSE
        # ==========================================================

        return {
            "status": "updated",
            "id": match_id
        }

    except Exception as e:

        print(
            f"[API] Erreur update_convocations : {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

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
    if not nom_parent or not verifier_si_autorise(nom_parent, categorie):
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
    if not nom_parent or not verifier_si_autorise(nom_parent, categorie):
        raise HTTPException(
            status_code=403,
            detail="Accès refusé : accès non validé ou utilisateur non identifié"
        )
    doc = db.collection(f"convocations_{categorie}").document(match_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Match non trouvé")
    return doc.to_dict()


##########################
# STATISTIQUES & HISTORIQUE DES PRESENCES
##########################

class StatsVoteRequest(BaseModel):
    id_sondage: str
    nom_parent: str
    nom_joueur_concerne: Optional[str] = None
    choix: Optional[str] = None
    choix_trajet: Optional[str] = None
    second_vote: Optional[str] = None
    choix_multiple: Optional[str] = None
    nombre_de_places: Optional[int] = None


def _stats_id_joueur(nom_joueur: str) -> str:
    """
    Transforme le nom du joueur en identifiant Firestore stable.
    """
    return (
        str(nom_joueur)
        .strip()
        .replace(" ", "_")
        .lower()
    )


def _stats_normaliser_type(type_evenement: str) -> str:
    """
    Normalise le type d'événement.
    """
    valeur = str(type_evenement or "").strip().upper()

    if valeur in ("MATCH", "MATCHES"):
        return "MATCH"

    if valeur in (
        "ENTRAINEMENT",
        "ENTRAÎNEMENT",
        "ENTRAINEMENTS",
        "ENTRAÎNEMENTS",
    ):
        return "ENTRAINEMENT"

    return valeur or "EVENEMENT"


def _stats_creer_event_uid():
    """
    Génère un identifiant historique indépendant du match_id.

    Le match_id peut changer ou être supprimé.
    Le event_uid reste la référence permanente.
    """
    import uuid

    return uuid.uuid4().hex


def _stats_recuperer_event_uid(categorie: str, match_id: str):
    """
    Récupère l'identifiant historique d'un événement.

    Si l'événement n'en possède pas encore, on en crée un.

    La route existante /convocations/... n'est pas modifiée.
    On ajoute uniquement notre champ stats_event_uid.
    """
    check_db()

    event_ref = (
        db.collection(f"convocations_{categorie}")
        .document(match_id)
    )

    snapshot = event_ref.get()

    if not snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail="Événement non trouvé"
        )

    data = snapshot.to_dict() or {}

    event_uid = data.get("stats_event_uid")

    if event_uid:
        return event_uid, data

    event_uid = _stats_creer_event_uid()

    event_ref.update({
        "stats_event_uid": event_uid
    })

    data["stats_event_uid"] = event_uid

    return event_uid, data


# ============================================================
# ARCHIVAGE D'UN EVENEMENT
# ============================================================

@app.post("/stats/historique/evenement/{categorie}/{match_id}")
def stats_enregistrer_evenement(
    categorie: str,
    match_id: str,
    nom_parent: Optional[str] = Header(
        None,
        alias="nom_parent"
    ),
):
    """
    Crée ou synchronise la copie historique d'un événement.

    L'événement actuel reste dans convocations_{categorie}.
    L'historique est indépendant et sera conservé après suppression.
    """

    check_db()

    if not nom_parent or not verifier_si_admin(
        nom_parent,
        categorie
    ):
        raise HTTPException(
            status_code=403,
            detail="Accès refusé"
        )

    try:

        event_uid, evenement = _stats_recuperer_event_uid(
            categorie,
            match_id
        )

        historique_ref = (
            db.collection("historique_presences")
            .document(categorie)
            .collection("evenements")
            .document(event_uid)
        )

        # --------------------------------------------------------
        # IMPORTANT :
        # On conserve la liste des joueurs convoqués.
        # Ainsi un joueur qui n'a jamais voté sera quand même
        # compté dans le nombre total d'événements.
        # --------------------------------------------------------

        joueurs_convoques = evenement.get(
            "joueurs_convoques",
            []
        )

        if not isinstance(joueurs_convoques, list):
            joueurs_convoques = []

        historique_data = {
            "event_uid": event_uid,
            "match_id": match_id,
            "categorie": categorie,

            "type": _stats_normaliser_type(
                evenement.get("type", "")
            ),

            "titre": evenement.get(
                "titre",
                ""
            ),

            "adversaire": evenement.get(
                "adversaire",
                ""
            ),

            "date": evenement.get(
                "date",
                ""
            ),

            "heure": (
                evenement.get("heure")
                or evenement.get("heure_rdv")
                or evenement.get("heure_sur_place")
                or evenement.get("heure_match")
                or ""
            ),

            "lieu": evenement.get(
                "lieu",
                ""
            ),

            # Liste figée des joueurs convoqués
            "joueurs_convoques": joueurs_convoques,

            "deleted": False,

            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        # merge=True :
        # les votes déjà enregistrés ne sont jamais supprimés.
        historique_ref.set(
            historique_data,
            merge=True
        )

        print(
            f"[STATS EVENEMENT] "
            f"categorie={categorie} | "
            f"match_id={match_id} | "
            f"event_uid={event_uid} | "
            f"joueurs={len(joueurs_convoques)}"
        )

        return {
            "status": "success",
            "event_uid": event_uid,
            "match_id": match_id,
        }

    except HTTPException:
        raise

    except Exception as e:

        print(
            f"[STATS EVENEMENT ERROR] {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# HISTORIQUE DES VOTES
# ============================================================

@app.post("/stats/historique/vote/{categorie}")
def stats_enregistrer_vote(
    categorie: str,
    vote: StatsVoteRequest,
):
    """
    Sauvegarde définitivement un vote.

    Deux informations sont conservées :

    1. votes/{joueur_id}
       -> dernier état du vote

    2. votes_history/{action_id}
       -> chaque action de vote, définitivement

    Ainsi :

        Présent
        Absent
        Présent

    reste entièrement conservé dans votes_history.
    """

    check_db()

    utilisateur = (
        vote.nom_parent or ""
    ).strip()

    if not utilisateur:
        raise HTTPException(
            status_code=400,
            detail="Identifiant de l'utilisateur manquant"
        )

    if not verifier_si_autorise(
        utilisateur,
        categorie
    ):
        raise HTTPException(
            status_code=403,
            detail="Action interdite : accès non validé"
        )

    try:

        # --------------------------------------------------------
        # 1. Récupération de l'événement actuel
        # --------------------------------------------------------

        event_ref = (
            db.collection(f"convocations_{categorie}")
            .document(vote.id_sondage)
        )

        event_snapshot = event_ref.get()

        if not event_snapshot.exists:
            raise HTTPException(
                status_code=404,
                detail="Événement non trouvé"
            )

        evenement = (
            event_snapshot.to_dict()
            or {}
        )

        # --------------------------------------------------------
        # 2. Event UID
        # --------------------------------------------------------

        event_uid = evenement.get(
            "stats_event_uid"
        )

        if not event_uid:

            event_uid = _stats_creer_event_uid()

            event_ref.update({
                "stats_event_uid": event_uid
            })

        # --------------------------------------------------------
        # 3. Détermination du joueur
        # --------------------------------------------------------

        joueur = (
            vote.nom_joueur_concerne
            or ""
        ).strip()

        if not joueur:

            id_utilisateur = (
                utilisateur
                .replace(" ", "_")
                .lower()
            )

            user_ref = (
                db.collection("users")
                .document(id_utilisateur)
            )

            user_snapshot = user_ref.get()

            joueurs_lies = []

            if user_snapshot.exists:

                user_data = (
                    user_snapshot.to_dict()
                    or {}
                )

                joueurs_par_cat = (
                    user_data.get(
                        "joueurs_par_categorie",
                        {}
                    )
                )

                if isinstance(
                    joueurs_par_cat,
                    dict
                ):
                    joueurs_lies = (
                        joueurs_par_cat.get(
                            categorie,
                            []
                        )
                    )

            joueur = (
                joueurs_lies[0]
                if joueurs_lies
                else utilisateur
            )

        joueur_id = _stats_id_joueur(
            joueur
        )

        is_coach = (
            str(joueur)
            .upper()
            .startswith("COACH_")
        )

        # --------------------------------------------------------
        # 4. Référence historique
        # --------------------------------------------------------

        historique_event_ref = (
            db.collection("historique_presences")
            .document(categorie)
            .collection("evenements")
            .document(event_uid)
        )

        # --------------------------------------------------------
        # 5. Synchronisation des informations de l'événement
        # --------------------------------------------------------

        joueurs_convoques = evenement.get(
            "joueurs_convoques",
            []
        )

        if not isinstance(
            joueurs_convoques,
            list
        ):
            joueurs_convoques = []

        historique_event_ref.set(
            {
                "event_uid": event_uid,

                "match_id": vote.id_sondage,

                "categorie": categorie,

                "type": _stats_normaliser_type(
                    evenement.get(
                        "type",
                        ""
                    )
                ),

                "titre": evenement.get(
                    "titre",
                    ""
                ),

                "adversaire": evenement.get(
                    "adversaire",
                    ""
                ),

                "date": evenement.get(
                    "date",
                    ""
                ),

                "heure": (
                    evenement.get("heure")
                    or evenement.get("heure_rdv")
                    or evenement.get("heure_sur_place")
                    or evenement.get("heure_match")
                    or ""
                ),

                "lieu": evenement.get(
                    "lieu",
                    ""
                ),

                # Très important pour les statistiques
                "joueurs_convoques": joueurs_convoques,

                "deleted": False,

                "updated_at":
                    firestore.SERVER_TIMESTAMP,
            },
            merge=True
        )

        # --------------------------------------------------------
        # 6. Données du vote
        # --------------------------------------------------------

        vote_data = {
            "joueur": joueur,
            "joueur_id": joueur_id,
            "parent": utilisateur,
            "est_coach": is_coach,

            "choix": vote.choix,
            "disponibilite": vote.choix,

            "choix_trajet": vote.choix_trajet,

            "second_vote": vote.second_vote,

            "choix_multiple":
                vote.choix_multiple,

            "nombre_de_places":
                vote.nombre_de_places,

            "match_id":
                vote.id_sondage,

            "timestamp":
                firestore.SERVER_TIMESTAMP,
        }

        # --------------------------------------------------------
        # 7. Dernier état du vote
        # --------------------------------------------------------

        dernier_vote_ref = (
            historique_event_ref
            .collection("votes")
            .document(joueur_id)
        )

        dernier_vote_ref.set(
            vote_data,
            merge=True
        )

        # --------------------------------------------------------
        # 8. Historique IMMUTABLE
        # --------------------------------------------------------

        historique_vote_ref = (
            historique_event_ref
            .collection("votes_history")
            .document()
        )

        historique_vote_ref.set(
            {
                **vote_data,

                "action_id":
                    historique_vote_ref.id,

                "timestamp":
                    firestore.SERVER_TIMESTAMP,
            }
        )

        print(
            f"[STATS VOTE] "
            f"categorie={categorie} | "
            f"event_uid={event_uid} | "
            f"match_id={vote.id_sondage} | "
            f"joueur={joueur} | "
            f"choix={vote.choix}"
        )

        return {
            "status": "success",
            "event_uid": event_uid,
            "joueur": joueur,
            "message":
                "Vote sauvegardé dans l'historique",
        }

    except HTTPException:
        raise

    except Exception as e:

        print(
            f"[STATS VOTE ERROR] {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post("/stats/historique/evenement/{categorie}/{match_id}")
def stats_creer_evenement(
    categorie: str,
    match_id: str,
):
    check_db()

    event_ref = (
        db.collection(f"convocations_{categorie}")
        .document(match_id)
    )

    event_snapshot = event_ref.get()

    if not event_snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail="Événement non trouvé"
        )

    evenement = event_snapshot.to_dict() or {}

    # ---------------------------------------------------------
    # UNIQUEMENT MATCH et ENTRAINEMENT pour les statistiques
    # ---------------------------------------------------------
    type_evenement = _stats_normaliser_type(
        evenement.get("type", "")
    )

    if type_evenement not in ("MATCH", "ENTRAINEMENT"):
        return {
            "status": "ignored",
            "event_uid": None,
            "match_id": match_id,
            "message": (
                "Cet événement n'est pas pris en compte "
                "dans les statistiques."
            ),
        }

    # ---------------------------------------------------------
    # Récupération ou création de l'identifiant statistique
    # ---------------------------------------------------------
    event_uid = evenement.get("stats_event_uid")

    if not event_uid:
        event_uid = _stats_creer_event_uid()

        event_ref.update({
            "stats_event_uid": event_uid
        })

    # ---------------------------------------------------------
    # Joueurs convoqués
    # ---------------------------------------------------------
    joueurs_convoques = evenement.get(
        "joueurs_convoques",
        []
    )

    if not isinstance(joueurs_convoques, list):
        joueurs_convoques = []

    # ---------------------------------------------------------
    # Référence de l'événement dans l'historique des stats
    # ---------------------------------------------------------
    historique_event_ref = (
        db.collection("historique_presences")
        .document(categorie)
        .collection("evenements")
        .document(event_uid)
    )

    # ---------------------------------------------------------
    # Création / mise à jour de l'événement statistique
    # ---------------------------------------------------------
    historique_event_ref.set(
        {
            "event_uid": event_uid,
            "match_id": match_id,
            "categorie": categorie,
            "type": type_evenement,

            "titre": evenement.get(
                "titre",
                ""
            ),

            "adversaire": evenement.get(
                "adversaire",
                ""
            ),

            "date": evenement.get(
                "date",
                ""
            ),

            "heure": (
                evenement.get("heure")
                or evenement.get("heure_rdv")
                or evenement.get("heure_sur_place")
                or evenement.get("heure_match")
                or ""
            ),

            "lieu": evenement.get(
                "lieu",
                ""
            ),

            "joueurs_convoques": joueurs_convoques,

            "deleted": False,

            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True
    )

    return {
        "status": "success",
        "event_uid": event_uid,
        "match_id": match_id,
        "type": type_evenement,
        "message": (
            "Match ou entraînement initialisé "
            "dans les statistiques."
        ),
    }


# ============================================================
# SUPPRESSION HISTORIQUE
# ============================================================

@app.delete("/stats/historique/evenement/{categorie}/{match_id}")
def stats_marquer_evenement_supprime(
    categorie: str,
    match_id: str,
    nom_parent: Optional[str] = Header(
        None,
        alias="nom_parent"
    ),
):
    """
    Marque l'événement comme supprimé.

    IMPORTANT :
    aucune donnée historique n'est supprimée.
    """

    check_db()

    if not nom_parent or not verifier_si_admin(
        nom_parent,
        categorie
    ):
        raise HTTPException(
            status_code=403,
            detail="Accès refusé"
        )

    try:

        # --------------------------------------------------------
        # 1. Recherche de l'événement actuel
        # --------------------------------------------------------

        event_ref = (
            db.collection(
                f"convocations_{categorie}"
            )
            .document(match_id)
        )

        snapshot = event_ref.get()

        if snapshot.exists:

            data = (
                snapshot.to_dict()
                or {}
            )

            event_uid = data.get(
                "stats_event_uid"
            )

            if event_uid:

                historique_ref = (
                    db.collection(
                        "historique_presences"
                    )
                    .document(categorie)
                    .collection("evenements")
                    .document(event_uid)
                )

                historique_ref.set(
                    {
                        "deleted": True,

                        "deleted_at":
                            firestore.SERVER_TIMESTAMP,

                        "match_id":
                            match_id,
                    },
                    merge=True
                )

                return {
                    "status": "success",
                    "event_uid": event_uid,
                    "deleted": True,
                }

        # --------------------------------------------------------
        # 2. Cas où l'événement est déjà supprimé
        # --------------------------------------------------------

        historique_events = (
            db.collection(
                "historique_presences"
            )
            .document(categorie)
            .collection("evenements")
            .where(
                "match_id",
                "==",
                match_id
            )
            .stream()
        )

        nombre = 0

        for doc in historique_events:

            doc.reference.set(
                {
                    "deleted": True,

                    "deleted_at":
                        firestore.SERVER_TIMESTAMP,
                },
                merge=True
            )

            nombre += 1

        return {
            "status": "success",
            "deleted": True,
            "historique_trouve": nombre,
        }

    except Exception as e:

        print(
            f"[STATS DELETE ERROR] {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# STATISTIQUES
# ============================================================

@app.get("/stats/{categorie}")
def recuperer_stats(
    categorie: str,
    nom_parent: Optional[str] = Header(
        None,
        alias="nom_parent"
    ),
):
    """
    Retourne les statistiques de présence.

    Seuls les ADMIN peuvent accéder aux statistiques.

    Le total est calculé à partir des joueurs convoqués,
    et non uniquement à partir des joueurs ayant voté.
    """

    check_db()

    # --------------------------------------------------------
    # 1. Vérification ADMIN stricte
    # --------------------------------------------------------

    if not nom_parent:

        raise HTTPException(
            status_code=403,
            detail="Accès refusé"
        )

    id_utilisateur = (
        nom_parent
        .strip()
        .replace(" ", "_")
        .lower()
    )

    user_ref = (
        db.collection("users")
        .document(id_utilisateur)
    )

    user_snapshot = user_ref.get()

    if not user_snapshot.exists:

        raise HTTPException(
            status_code=403,
            detail="Utilisateur inconnu"
        )

    user_data = (
        user_snapshot.to_dict()
        or {}
    )

    roles = user_data.get(
        "roles_par_categorie",
        {}
    )

    role = str(
        roles.get(
            categorie,
            "EXCLU"
        )
    ).strip().upper()

    # ADMIN UNIQUEMENT
    if role != "ADMIN":

        raise HTTPException(
            status_code=403,
            detail=(
                "Statistiques réservées "
                "aux administrateurs"
            )
        )

    # --------------------------------------------------------
    # 2. Récupération des événements historiques
    # --------------------------------------------------------

    events_ref = (
        db.collection(
            "historique_presences"
        )
        .document(categorie)
        .collection("evenements")
    )

    events_docs = events_ref.stream()

    evenements = []

    for doc in events_docs:

        data = (
            doc.to_dict()
            or {}
        )

        data["event_uid"] = doc.id

        evenements.append(data)

    # --------------------------------------------------------
    # 3. Récupération de TOUS les joueurs de la catégorie
    # --------------------------------------------------------

    joueurs = {}

    users_docs = (
        db.collection("users")
        .stream()
    )

    for user_doc in users_docs:

        user = (
            user_doc.to_dict()
            or {}
        )

        roles_user = user.get(
            "roles_par_categorie",
            {}
        )

        if categorie not in roles_user:
            continue

        joueurs_user = user.get(
            "joueurs_par_categorie",
            {}
        )

        if not isinstance(
            joueurs_user,
            dict
        ):
            continue

        liste = joueurs_user.get(
            categorie,
            []
        )

        if not isinstance(
            liste,
            list
        ):
            continue

        for joueur in liste:

            joueur = str(
                joueur
            ).strip()

            if not joueur:
                continue

            joueur_id = _stats_id_joueur(
                joueur
            )

            joueurs[joueur_id] = {
                "id": joueur_id,
                "nom": joueur,

                "entrainements": 0,
                "entrainements_total": 0,

                "matchs": 0,
                "matchs_total": 0,

                "total_present": 0,
                "total_evenements": 0,

                "pourcentage_presence": 0,
            }

    # --------------------------------------------------------
    # 4. Calcul des statistiques
    # --------------------------------------------------------

    for evenement in evenements:

        event_uid = evenement.get(
            "event_uid"
        )

        type_evt = (
            _stats_normaliser_type(
                evenement.get(
                    "type",
                    ""
                )
            )
        )

        # ----------------------------------------------------
        # Joueurs réellement convoqués
        # ----------------------------------------------------

        joueurs_convoques = evenement.get(
            "joueurs_convoques",
            []
        )

        if not isinstance(
            joueurs_convoques,
            list
        ):
            joueurs_convoques = []

        # Ensemble des joueurs convoqués
        joueurs_convoques_ids = set()

        for joueur_conv in joueurs_convoques:

            # Format possible :
            # {"nom": "...", "prenom": "..."}
            if isinstance(
                joueur_conv,
                dict
            ):

                nom = str(
                    joueur_conv.get(
                        "nom",
                        ""
                    )
                ).strip()

                prenom = str(
                    joueur_conv.get(
                        "prenom",
                        ""
                    )
                ).strip()

                nom_complet = (
                    f"{nom} {prenom}"
                ).strip()

            else:

                nom_complet = str(
                    joueur_conv
                ).strip()

            if not nom_complet:
                continue

            joueur_id = _stats_id_joueur(
                nom_complet
            )

            joueurs_convoques_ids.add(
                joueur_id
            )

            # Si le joueur n'existe plus dans
            # users, on peut quand même conserver
            # son historique.
            if joueur_id not in joueurs:

                joueurs[joueur_id] = {
                    "id": joueur_id,
                    "nom": nom_complet,

                    "entrainements": 0,
                    "entrainements_total": 0,

                    "matchs": 0,
                    "matchs_total": 0,

                    "total_present": 0,
                    "total_evenements": 0,

                    "pourcentage_presence": 0,
                }

        # ----------------------------------------------------
        # Votes actuels
        # ----------------------------------------------------

        votes_ref = (
            events_ref
            .document(event_uid)
            .collection("votes")
        )

        votes_docs = votes_ref.stream()

        votes_par_joueur = {}

        for vote_doc in votes_docs:

            vote_data = (
                vote_doc.to_dict()
                or {}
            )

            joueur_id = vote_doc.id

            # Les coachs ne sont pas comptés
            if vote_data.get(
                "est_coach",
                False
            ):
                continue

            votes_par_joueur[joueur_id] = (
                vote_data
            )

        # ----------------------------------------------------
        # TOTAL : basé sur les joueurs convoqués
        # ----------------------------------------------------

        for joueur_id in joueurs_convoques_ids:

            joueur_data = joueurs.get(
                joueur_id
            )

            if not joueur_data:
                continue

            if type_evt == "ENTRAINEMENT":

                joueur_data[
                    "entrainements_total"
                ] += 1

            elif type_evt == "MATCH":

                joueur_data[
                    "matchs_total"
                ] += 1

        # ----------------------------------------------------
        # PRESENCES : basées sur le dernier vote
        # ----------------------------------------------------

        for joueur_id, vote_data in (
            votes_par_joueur.items()
        ):

            if joueur_id not in joueurs:
                continue

            choix = str(
                vote_data.get(
                    "disponibilite",
                    vote_data.get(
                        "choix",
                        ""
                    )
                )
                or ""
            ).strip().upper()

            est_present = choix in (
                "PRESENT",
                "PRÉSENT",
                "PRESENT(E)",
                "PRÉSENT(E)",
                "OUI",
                "DISPONIBLE",
                "PARTICIPE",
                "PARTICIPERA",
            )

            if not est_present:
                continue

            joueurs[joueur_id][
                "total_present"
            ] += 1

            if type_evt == "ENTRAINEMENT":

                joueurs[joueur_id][
                    "entrainements"
                ] += 1

            elif type_evt == "MATCH":

                joueurs[joueur_id][
                    "matchs"
                ] += 1

    # --------------------------------------------------------
    # 5. Pourcentages
    # --------------------------------------------------------

    resultats = []

    for joueur in joueurs.values():

        total = (
            joueur[
                "entrainements_total"
            ]
            +
            joueur[
                "matchs_total"
            ]
        )

        joueur[
            "total_evenements"
        ] = total

        if total > 0:

            joueur[
                "pourcentage_presence"
            ] = round(
                (
                    joueur[
                        "total_present"
                    ]
                    / total
                ) * 100,
                1
            )

        else:

            joueur[
                "pourcentage_presence"
            ] = 0

        resultats.append(
            joueur
        )

    resultats.sort(
        key=lambda x:
            str(
                x.get(
                    "nom",
                    ""
                )
            ).lower()
    )

    # --------------------------------------------------------
    # 6. Réponse
    # --------------------------------------------------------

    return {
        "categorie": categorie,

        "nombre_evenements": len(
            evenements
        ),

        "nombre_matchs": sum(
            1
            for e in evenements
            if _stats_normaliser_type(
                e.get(
                    "type",
                    ""
                )
            ) == "MATCH"
        ),

        "nombre_entrainements": sum(
            1
            for e in evenements
            if _stats_normaliser_type(
                e.get(
                    "type",
                    ""
                )
            ) == "ENTRAINEMENT"
        ),

        "joueurs": resultats,
    }

# ============================================================
# FCM - NOTIFICATION CIBLEE CONVOCATION MATCH
# ============================================================
#
# Cette fonction est volontairement indépendante de
# envoyer_notif_push_token().
#
# Elle reprend le même mécanisme FCM :
#   - Android HIGH priority
#   - APNS
#   - notification title/body
#   - data payload
#   - envoi sur un token précis
#
# Elle ajoute simplement match_id dans le payload data.
#
# ============================================================

def envoyer_notif_convocation_token(
    fcm_token: str,
    titre: str,
    corps: str,
    categorie: str,
    match_id: str,
):
    """
    Envoie une notification FCM de convocation sur un token précis.

    Fonction indépendante de envoyer_notif_push_token().
    Ne modifie aucune fonction existante.
    """

    if not fcm_token:
        print(
            "[FCM CONVOCATION] Aucun token FCM fourni."
        )
        return False

    try:

        # ----------------------------------------------------
        # Payload data
        # ----------------------------------------------------

        data_payload = {
            "title": titre,
            "body": corps,
            "categorie": categorie,
            "notif_type": "convocation",
            "open_page": "vestiaire",
            "match_id": str(match_id),
        }

        # ----------------------------------------------------
        # Configuration Android
        # ----------------------------------------------------

        android_config = messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                icon="ic_notification",
                channel_id="fcvv_high_priority_v2",
            )
        )

        # ----------------------------------------------------
        # Configuration APNS (iOS)
        # ----------------------------------------------------

        apns_config = messaging.APNSConfig(
            headers={
                "apns-priority": "10",
            },
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    sound="default",
                )
            ),
        )

        # ----------------------------------------------------
        # Message FCM
        # ----------------------------------------------------

        message = messaging.Message(

            notification=messaging.Notification(
                title=titre,
                body=corps,
            ),

            data=data_payload,

            android=android_config,

            apns=apns_config,

            token=fcm_token,
        )

        # ----------------------------------------------------
        # Envoi
        # ----------------------------------------------------

        response = messaging.send(message)

        print(
            f"[FCM CONVOCATION] Notification envoyée "
            f"sur le token {fcm_token[:20]}... "
            f"(match_id={match_id}) : {response}"
        )

        return True

    except Exception as e:

        print(
            f"[FCM CONVOCATION] Erreur envoi notification : "
            f"{e}"
        )

        return False

def recuperer_tokens_fcm_pour_joueur(
    joueur_nom: str,
    categorie: str
) -> list[str]:
    """
    Recherche dans users les parents associés à un joueur
    pour une catégorie donnée et retourne leurs tokens FCM.
    """

    check_db()

    joueur_recherche = str(joueur_nom or "").strip()

    if not joueur_recherche:
        return []

    tokens_trouves = []

    try:
        docs = db.collection("users").stream()

        for doc in docs:

            data = doc.to_dict()

            joueurs_par_categorie = data.get(
                "joueurs_par_categorie",
                {}
            )

            if not isinstance(joueurs_par_categorie, dict):
                continue

            joueurs = joueurs_par_categorie.get(
                categorie,
                []
            )

            if not isinstance(joueurs, list):
                continue

            joueurs_normalises = [
                str(j).strip().casefold()
                for j in joueurs
                if str(j).strip()
            ]

            if joueur_recherche.casefold() not in joueurs_normalises:
                continue

            fcm_tokens = data.get(
                "fcm_tokens",
                []
            )

            if not isinstance(fcm_tokens, list):
                continue

            for token in fcm_tokens:

                token = str(token).strip()

                if token and token not in tokens_trouves:
                    tokens_trouves.append(token)

        print(
            f"[FCM CONVOCATION] "
            f"joueur={joueur_recherche} | "
            f"categorie={categorie} | "
            f"tokens={len(tokens_trouves)}"
        )

        return tokens_trouves

    except Exception as e:

        print(
            f"[FCM CONVOCATION] "
            f"Erreur recherche joueur={joueur_recherche} : {e}"
        )

        return []


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)