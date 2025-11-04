
import asyncio
import json
from datetime import datetime ,timedelta,UTC
from pathlib import Path
import httpx
from motor.motor_asyncio import AsyncIOMotorClient  # ✅ Motor
from pymongo.errors import ConnectionFailure
from dotenv import load_dotenv
import os
from flask import Flask, jsonify
# --- Constantes ---



app=Flask(__name__)
REGIONS = ["IND", "BD", "ME","BR"]
BATCH_SIZE = 105
CHECK_INTERVAL = 10  # en secondes
REFRESH_INTERVAL = 6 * 60 * 60  # en secondes (6 heures)
TEMP_EXPIRE_LIMIT = 30 * 60  # en secondes (30 minutes)

JWT_SERVERS= [
"http://token1.thug4ff.com/",
"http://token2.thug4ff.com/",
"http://token3.thug4ff.com/",
"http://token4.thug4ff.com/",
]
load_dotenv()
# --- Variables globales ---
client = None
db = None
token_state = {}
processing = {}


MONGO_URI = os.getenv("MONGO_URI")
print(MONGO_URI)

async def init_mongo():
    global client, db
    try:

        
        client = AsyncIOMotorClient(MONGO_URI)
        await client.admin.command("ping")  # ✅ maintenant possible
        db = client.get_database("spam_xpert")
        print("Connecté à MongoDB avec succès.")
    except ConnectionFailure as e:
        print(f"[ERREUR] Échec de la connexion à MongoDB : {e}")
        raise
    except Exception as e:
        print(f"[ERREUR] Problème MongoDB : {e}")
        raise

async def load_token_state():
    state_collection = db.get_collection("token_state")
    for region in REGIONS:
        doc = await state_collection.find_one({"region": region})
        if not doc:
            initial_state = {
                "region": region,
                "success_count": 0,
                "last_token_update_time": None,
                "current_index": 0,
                "refresh_done": False,
                "refresh_count": 0  
            }
            await state_collection.insert_one(initial_state)
            token_state[region] = initial_state
        else:
            token_state[region] = {
                "success_count": doc.get("success_count", 0),
                "last_token_update_time": doc.get("last_token_update_time", None),
                "current_index": doc.get("current_index", 0),
                "refresh_done": doc.get("refresh_done", False),
                "refresh_count": doc.get("refresh_count", 0) 
            }


async def save_token_state(region, updates):
    await db.get_collection("token_state").update_one(
        {"region": region}, {"$set": updates}
    )
    token_state[region].update(updates)


async def refresh_tokens(region, should_update_index=True):
    file_path = Path(__file__).parent / f"data/{region.lower()}_data.json"
    data = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[ERREUR] Fichier introuvable pour {region} : {file_path}")
        return 0
    except json.JSONDecodeError as e:
        print(f"[ERREUR] Fichier invalide {region} : {e}")
        return 0

    if not data:
        print(f"[{region}] Aucune donnée UID/mdp.")
        return 0

    start_index = token_state[region]["current_index"]
    temp_col = db.get_collection(f"{region.lower()}_temp_tokens")
    await temp_col.delete_many({})  # Réinitialisation temporaire

    token_docs = []
    tasks = []

    async def fetch_token(uid, password, jwt_server):
        url = f"{jwt_server}token?uid={uid}&password={password}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client_http:
                res = await client_http.get(url)
                res.raise_for_status()
                token = res.json().get("token")
                if token:
                    token_docs.append({
                                        "uid": uid,
                                        "token": token,
                                       "timestamp": datetime.now(UTC)
                                       })
        except Exception as e:
            err_msg = str(e)[:50]  
            print(f"[{region}] UID {uid} erreur : {err_msg}")


    for i in range(BATCH_SIZE):
        index = (start_index + i) % len(data)
        user_data = data[index]
        uid = user_data.get("uid")
        password = user_data.get("password")
        jwt_server = JWT_SERVERS[i % len(JWT_SERVERS)]

        if uid and password:
            tasks.append(fetch_token(uid, password, jwt_server))
        else:
            print(f"[{region}] Données UID/mdp manquantes à l'index {index}.")

    await asyncio.gather(*tasks)

    refreshed_count = 0
    if token_docs:
        await temp_col.insert_many(token_docs)
        refreshed_count = len(token_docs)

    if refreshed_count > 0 and should_update_index:
        new_index = (start_index + BATCH_SIZE) % len(data)
        await save_token_state(
            region,
            {
                "current_index": new_index,
                "last_token_update_time": datetime.now(UTC),
                "refresh_count": refreshed_count,
            },
        )
        print(f"[{region}] ✅ {refreshed_count} jetons générés. Index mis à jour.")

    elif refreshed_count > 0:
        print(f"[{region}] 🔁 Même plage réutilisée.")
    else:
        print(f"[{region}] ❌ Aucun jeton généré.")

    return refreshed_count


async def move_tokens(region):
    temp_col = db.get_collection(f"{region.lower()}_temp_tokens")
    main_col = db.get_collection(f"{region.lower()}_tokens")
    threshold_time = datetime.now(UTC) - timedelta(seconds=TEMP_EXPIRE_LIMIT)

    tokens_to_move = await temp_col.find({"timestamp": {"$gte": threshold_time}}).to_list(None)

    if not tokens_to_move:
        print(f"[{region}] ❌ Aucun jeton à déplacer.")
        return False

    await main_col.delete_many({})
    await main_col.insert_many(
        [{"uid":t["uid"],   "token": t["token"], "timestamp": datetime.now(UTC)} for t in tokens_to_move]
    )
    await temp_col.delete_many({})

    await save_token_state(
        region,
        {
            "success_count": 0,
            "refresh_done": False,
            "last_token_update_time": datetime.now(UTC),
        },
    )
    print(f"[{region}] ✅ {len(tokens_to_move)} jetons déplacés.")
    return True


async def check_loop():
    now = datetime.now(UTC).timestamp()
    await load_token_state()
    for region in REGIONS:
        if processing.get(region):
            continue
        processing[region] = True
        try:
            
            state = token_state[region]
            success_count = state["success_count"]
            refresh_done = state["refresh_done"]
            last_update_time = state.get("last_token_update_time")

            last_update_ts = last_update_time.timestamp() if isinstance(last_update_time, datetime) else 0
            
            # 👉 Si aucun token n'existe, on génère immédiatement
            main_col = db.get_collection(f"{region.lower()}_tokens")
            existing = await main_col.count_documents({})
            if existing == 0:
                print(f"[{region}] 🚨 Aucune donnée. Génération forcée...")
                refreshed = await refresh_tokens(region)
                if refreshed > 0:
                    await save_token_state(region, {
                        "refresh_done": True,
                        "last_token_update_time": datetime.now(UTC),
                    })
                    await move_tokens(region)
                continue  # on passe au suivant
            time_since_last_update= now - last_update_ts
           

           # 🔄 1️⃣ Rafraîchissement après 28 succès
            if success_count >= 28 and not refresh_done:
                print(f"[{region}] 🔄 28 succès. Rafraîchissement...")
                refreshed = await refresh_tokens(region)

                # Tant qu'on n'a pas au moins 95 jetons, on relance
                while refreshed < 100:
                    print(f"[{region}] ⚠️ Seulement {refreshed} jetons générés. Relance du rafraîchissement...")
                    refreshed = await refresh_tokens(region)

                print(f"[{region}] ✅ Rafraîchissement terminé : {refreshed} jetons générés.")
                if refreshed > 0:
                    await save_token_state(region, {"refresh_done": True})

            if time_since_last_update >= REFRESH_INTERVAL and not refresh_done:
                print(f"[{region}] ⏰ 6h écoulées. Rafraîchissement...")
                refreshed = await refresh_tokens(region)

                while refreshed < 100:
                    print(f"[{region}] ⚠️ Seulement {refreshed} jetons générés. Relance du rafraîchissement...")
                    refreshed = await refresh_tokens(region)

                print(f"[{region}] ✅ Rafraîchissement (6h) terminé : {refreshed} jetons générés.")
                if refreshed > 0:
                    await save_token_state(
                        region,
                        {
                            "refresh_done": True,
                            "last_token_update_time": datetime.now(UTC),
                        },
                    )
                    moved = await move_tokens(region)
                    if not moved:
                        print(f"[{region}] ❌ Jetons 6h expirés. Nouvelle génération...")
                        refreshed_again = await refresh_tokens(region, False)

                        while refreshed_again < 95:
                            print(f"[{region}] ⚠️ Seulement {refreshed_again} jetons régénérés. Relance...")
                            refreshed_again = await refresh_tokens(region, False)

                        print(f"[{region}] ✅ Nouvelle génération terminée : {refreshed_again} jetons générés.")
                        if refreshed_again > 0:
                            await save_token_state(region, {"last_token_update_time": datetime.now(UTC)})


        except Exception as err:
            print(f"[{region}] ❌ Erreur : {err}")
        finally:
            processing[region] = False



async def start_token_manager():
    try:
        await init_mongo()
        await load_token_state()
        print(f"Démarrage de la boucle toutes les {CHECK_INTERVAL}s...")
        while True:
            await check_loop()
            await asyncio.sleep(CHECK_INTERVAL)
    except Exception as e:
        print(f"[CRITIQUE] Erreur au démarrage : {e}")
        

# async def get_all_token_states():
#     state_collection = db.get_collection("token_state")
#     cursor = state_collection.find({})
#     states = await cursor.to_list(None)
#     return states
       
if __name__ == "__main__":
    asyncio.run(start_token_manager())
    app.run(host="0.0.0.0", port=5000)
    


