import discord

from discord import app_commands

from discord.ext import tasks

import aiohttp

import asyncio

import os

import logging

import time

import datetime

import json

import uuid

import base64

import aiosqlite

from collections import deque

from dotenv import load_dotenv

# ────── ⚙️ 1. Configuration & Setup ──────

load_dotenv()

# Logger Setup

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s [%(levelname)s] %(message)s",

    datefmt="%Y-%m-%d %H:%M:%S"

)

# Keys

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")

PPQ_API_KEY = os.getenv("PPQ_API_KEY")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# URLs

PPQ_API_URL = os.getenv("PPQ_API_URL", "https://api.ppq.ai/chat/completions")

OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434/v1/chat/completions")

GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/models")

AI_ICON_URL = os.getenv("AI_ICON_URL", "https://raw.githubusercontent.com/oomplay/Discrord-bot/main/image/image.png")

# Settings

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemini-2.0-flash")

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "Magic Ai")

CONVERSATION_HINT = "[Reply to continue the conversation.]"

DB_NAME = "chat_history.db"

# Constants & Limits

MAX_CONTEXT_MESSAGES = 10

LOCK_TTL = 600

GLOBAL_RATE_LIMIT = 40       

STREAM_BASE_INTERVAL = 1.5   

# ────── 🧬 2. Dynamic Model Loading ──────

MODELS_CONFIG = {}

MODEL_ROUTE = {}

LAST_MODEL_UPDATE = 0

def load_models_config():

    global MODELS_CONFIG, MODEL_ROUTE

    try:

        with open('model.json', 'r', encoding='utf-8') as f:

            MODELS_CONFIG = json.load(f)

        MODEL_ROUTE = {k: v["provider"] for k, v in MODELS_CONFIG.items()}

        logging.info(f"✅ Loaded {len(MODELS_CONFIG)} models.")

    except Exception as e:

        logging.error(f"⚠️ Error loading model.json: {e}")

        if not MODELS_CONFIG:

            MODELS_CONFIG = {DEFAULT_MODEL: {"provider": "gemini", "display_name": "Gemini Default", "vision": False}}

            MODEL_ROUTE = {DEFAULT_MODEL: "gemini"}

load_models_config()

if os.path.exists('model.json'):

    LAST_MODEL_UPDATE = os.path.getmtime('model.json')

# ────── 🧱 3. Database Layer ──────

async def init_db():

    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute("PRAGMA journal_mode=WAL;")

        await db.execute("""

            CREATE TABLE IF NOT EXISTS conversations (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                message_id INTEGER UNIQUE, 

                history TEXT,

                model TEXT,

                created_at REAL

            )

        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON conversations(created_at);")

        await db.commit()

    logging.info("💾 Database initialized.")

async def save_to_db(msg_id, history, model):

    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute(

            "INSERT OR REPLACE INTO conversations (message_id, history, model, created_at) VALUES (?, ?, ?, ?)",

            (msg_id, json.dumps(history, ensure_ascii=False), model, time.time())

        )

        await db.commit()

async def load_from_db(msg_id):

    async with aiosqlite.connect(DB_NAME) as db:

        async with db.execute("SELECT history, model FROM conversations WHERE message_id = ?", (msg_id,)) as cursor:

            row = await cursor.fetchone()

            if row: return json.loads(row[0]), row[1]

    return None, None

async def clean_old_db_records():

    cutoff = time.time() - (365 * 24 * 3600) 

    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute("DELETE FROM conversations WHERE created_at < ?", (cutoff,))

        await db.commit()

    logging.info("🧹 Cleaned old database records.")

# ────── 🔒 4. Security & Rate Limiting ──────

user_locks = {} 

global_rate_buffer = deque()

def check_global_rate_limit() -> bool:

    now = time.time()

    while global_rate_buffer and now - global_rate_buffer[0] > 60:

        global_rate_buffer.popleft()

    if len(global_rate_buffer) >= GLOBAL_RATE_LIMIT: return False

    global_rate_buffer.append(now)

    return True

async def get_user_lock(user_id):

    current_time = time.time()

    if user_id not in user_locks:

        user_locks[user_id] = {'lock': asyncio.Lock(), 'last_active': current_time}

    else:

        user_locks[user_id]['last_active'] = current_time

    return user_locks[user_id]['lock']

@tasks.loop(minutes=5)

async def prune_user_locks():

    now = time.time()

    expired_users = [uid for uid, data in user_locks.items() if now - data['last_active'] > LOCK_TTL]

    removed = 0

    for uid in expired_users:

        if uid in user_locks and not user_locks[uid]['lock'].locked():

            del user_locks[uid]

            removed += 1

    if removed > 0: logging.info(f"🗑️ GC: Released {removed} locks.")

# ────── 🧠 5. API Logic (Polished) ──────

session: aiohttp.ClientSession = None

class APIError(Exception): pass

def sanitize_error(e: Exception) -> str:

    err_id = uuid.uuid4().hex[:8]

    logging.error(f"ERR-{err_id}: {str(e)}", exc_info=True)

    return f"System malfunction (Ref: `{err_id}`) Please try again"

def trim_history(history):

    if len(history) <= MAX_CONTEXT_MESSAGES + 1: return history

    sys = [m for m in history if m['role'] == 'system']

    chat = [m for m in history if m['role'] != 'system']

    return sys + chat[-MAX_CONTEXT_MESSAGES:]

async def stream_openai_format(url, headers, payload):

    payload["stream"] = True

    async with session.post(url, headers=headers, json=payload, timeout=60) as resp:

        if resp.status != 200: 

            err_text = await resp.text()

            raise APIError(f"API Error {resp.status}: {err_text[:200]}")

            

        async for line in resp.content:

            line = line.decode('utf-8').strip()

            if line.startswith("data: ") and line != "data: [DONE]":

                try:

                    data = json.loads(line[6:])

                    chunk = data["choices"][0]["delta"].get("content", "")

                    if chunk: yield chunk

                except (json.JSONDecodeError, KeyError): continue

async def standard_gemini_call(history, model, image_url=None):

    system_prompt_text = next((m['content'] for m in history if m['role'] == 'system'), None)

    gemini_contents = []

    

    for m in history:

        if m['role'] != 'system':

            gemini_contents.append({

                "role": "model" if m["role"] == "assistant" else "user",

                "parts": [{"text": str(m['content'])}]

            })

            

    if image_url:

        try:

            async with session.get(image_url) as resp:

                if resp.status == 200:

                    img_data = await resp.read()

                    b64_data = base64.b64encode(img_data).decode('utf-8')

                    mime_type = resp.headers.get("Content-Type", "image/jpeg")

                    if gemini_contents and gemini_contents[-1]["role"] == "user":

                        gemini_contents[-1]["parts"].append({

                            "inline_data": {"mime_type": mime_type, "data": b64_data}

                        })

        except Exception as e:

            logging.error(f"❌ Gemini Image Error: {e}")

    payload = {"contents": gemini_contents}

    if system_prompt_text:

        payload["systemInstruction"] = {"parts": [{"text": system_prompt_text}]}

    

    url = f"{GEMINI_BASE_URL}/{model}:generateContent?key={GEMINI_API_KEY}"

    async with session.post(url, json=payload, timeout=90) as resp:

        if resp.status != 200: 

            raise APIError(f"Gemini Error {resp.status}: {await resp.text()}")

        data = await resp.json()

        try: 

            return data["candidates"][0]["content"]["parts"][0]["text"]

        except (KeyError, IndexError):

            if "promptFeedback" in data: raise APIError("The content has been blocked by AI Safety Filter")

            raise APIError("Gemini did not reply.")

# ────── 🚀 6. Core Processor (Final Flow) ──────

async def process_ai_request(model: str, history: list, interaction: discord.Interaction = None, message: discord.Message = None, user_id: int = None, image_url: str = None):

    

    # 1. ACKNOWLEDGE IMMEDIATELY

    target_msg = None

    try:

        if interaction:

            if not interaction.response.is_done():

                await interaction.response.defer(thinking=True)

        elif message:

            await message.channel.typing()

    except Exception as e:

        logging.warning(f"Failed to defer: {e}")

        return

    # 2. Check Limits

    if not check_global_rate_limit():

        msg = "⚠️ Server Busy: Please wait. (Global Rate Limit)"

        if interaction: await interaction.followup.send(msg, ephemeral=True)

        elif message: await message.reply(msg, delete_after=5)

        return

    lock = await get_user_lock(user_id)

    if lock.locked():

        msg = "⏳ Please wait for the previous message to finish processing."

        if interaction: await interaction.followup.send(msg, ephemeral=True)

        elif message: await message.reply(msg, delete_after=5)

        return

    async with lock:

        start_time = time.time()

        optimized_history = trim_history(history)

        

        # 3. Model & Vision Config

        if model not in MODELS_CONFIG: model = DEFAULT_MODEL

        model_config = MODELS_CONFIG.get(model, {})

        provider = MODEL_ROUTE.get(model, "gemini")

        supports_vision = model_config.get("vision", False)

        

        if image_url and not supports_vision:

             warn = f"⚠️ Model `{model}` Images are not supported (only text replies will be sent)"

             if interaction: await interaction.followup.send(warn, ephemeral=True)

             elif message: await message.channel.send(warn, delete_after=5)

             image_url = None 

        full_response = ""

        model_name = model_config.get('display_name', model)

        

        embed = discord.Embed(description="*Thinking...* 💭", color=discord.Color.from_str("#4e8df7"))

        embed.set_author(name=f"🤖 {model_name}", icon_url=AI_ICON_URL)

        if image_url: embed.set_thumbnail(url=image_url)

        

        if interaction:

            target_msg = await interaction.followup.send(embed=embed, wait=True)

        elif message:

            target_msg = await message.reply(embed=embed, mention_author=False)

        try:

            if provider in ["openrouter", "ollama", "ppq"]:

                # ✨ POLISH: Headers Update

                url = ""

                headers = {}

                if provider == "openrouter": 

                    url = OPENROUTER_API_URL

                    headers = {

                        "Authorization": f"Bearer {OPENROUTER_API_KEY}", 

                        "HTTP-Referer": "https://discord.com",

                        "X-Title": "Discord AI Bot"

                    }

                    if user_id: headers["X-User-ID"] = str(user_id)

                elif provider == "ppq": 

                    url = PPQ_API_URL

                    headers = {"Authorization": f"Bearer {PPQ_API_KEY}"}

                elif provider == "ollama": 

                    url = OLLAMA_API_URL

                    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"}

                

                # ✨ POLISH: Safe Content & Default Prompt

                messages_payload = list(optimized_history)

                if image_url:

                    last_msg = messages_payload[-1]

                    if last_msg["role"] == "user":

                        vision_msg = last_msg.copy() # Safe Copy

                        

                        raw_content = vision_msg.get("content", "")

                        if not isinstance(raw_content, str): raw_content = str(raw_content)

                        if not raw_content.strip(): raw_content = "Describe this image." # Default Prompt

                        

                        vision_msg["content"] = [

                            {"type": "text", "text": raw_content},

                            {"type": "image_url", "image_url": {"url": image_url}}

                        ]

                        messages_payload[-1] = vision_msg

                

                payload = {"model": model, "messages": messages_payload, "stream": True}

                last_update = time.time()

                current_interval = STREAM_BASE_INTERVAL 

                async for chunk in stream_openai_format(url, headers, payload):

                    full_response += chunk

                    if len(full_response) > 2000: current_interval = 3.0

                    elif len(full_response) > 1000: current_interval = 2.0

                    

                    if time.time() - last_update > current_interval:

                        embed.description = full_response + " ▌"

                        if len(embed.description) > 4000: embed.description = embed.description[:4000] + "..."

                        await target_msg.edit(embed=embed)

                        last_update = time.time()

                        

            elif provider == "gemini":

                full_response = await standard_gemini_call(optimized_history, model, image_url)

            else:

                raise APIError("Unknown Provider")

            # Finalize

            duration = time.time() - start_time

            embed.description = full_response[:4096]

            embed.set_footer(text=f"⏱️ {duration:.2f}s • {CONVERSATION_HINT}")

            await target_msg.edit(embed=embed)

            # Save

            new_history = list(history)

            if image_url: new_history[-1]["content"] = f"{new_history[-1]['content']} [แนบรูปภาพ]"

            new_history.append({"role": "assistant", "content": full_response})

            await save_to_db(target_msg.id, new_history, model)

        except Exception as e:

            safe_err = sanitize_error(e)

            embed.color = discord.Color.red()

            embed.title = "❌ An error occurred."

            embed.description = safe_err

            if target_msg: await target_msg.edit(embed=embed)

# ────── 🕵️ 7. Recovery ──────

async def recover_history(message: discord.Message):

    if not message.reference: return None, None

    ref_id = message.reference.message_id

    

    history, model = await load_from_db(ref_id)

    if history:

        logging.info(f"💾 DB Hit: {ref_id}")

        return history, model

    logging.info(f"⛏️ DB Miss: Crawling chain from {ref_id}")

    chain = []

    curr = message.reference.resolved

    if not curr:

        try: curr = await message.channel.fetch_message(ref_id)

        except: return None, None

    

    if curr.author.id != client.user.id: return None, None

    loop_count = 0

    detected_model = DEFAULT_MODEL

    

    while curr and loop_count < 20:

        if curr.author.id == client.user.id:

            role = "assistant"

            if curr.embeds:

                content = curr.embeds[0].description

                if loop_count == 0 and curr.embeds[0].author.name:

                    clean_name = curr.embeds[0].author.name.replace("🤖 ", "").strip()

                    for mid, data in MODELS_CONFIG.items():

                        if data["display_name"] == clean_name: detected_model = mid

            else: content = curr.content

        else:

            role = "user"

            content = curr.content

            

        if content: chain.insert(0, {"role": role, "content": content})

        

        if curr.reference:

            try: curr = curr.reference.resolved or await curr.channel.fetch_message(curr.reference.message_id)

            except: break

        else: break

        loop_count += 1

    

    if not chain: return None, None

    return [{"role": "system", "content": SYSTEM_PROMPT}] + chain, detected_model

# ────── 🔌 8. Life Cycle Management ──────

class BotClient(discord.Client):

    async def setup_hook(self):

        global session

        session = aiohttp.ClientSession()

        await init_db()

        await self.tree.sync()

        clean_db_task.start()

        prune_user_locks.start()

        auto_reload_models_task.start()

        logging.info(f"✅ Bot Online: {self.user}")

    async def close(self):

        logging.info("🛑 Shutting down...")

        if session: await session.close()

        await super().close()

intents = discord.Intents.default()

intents.message_content = True

client = BotClient(intents=intents)

tree = app_commands.CommandTree(client)

client.tree = tree

@tasks.loop(hours=24)

async def clean_db_task(): await clean_old_db_records()

@tasks.loop(seconds=30)

async def auto_reload_models_task():

    global LAST_MODEL_UPDATE

    if not os.path.exists('model.json'): return

    try:

        current_mtime = os.path.getmtime('model.json')

        if current_mtime > LAST_MODEL_UPDATE:

            logging.info("♻️ Config changed. Reloading...")

            load_models_config()

            LAST_MODEL_UPDATE = current_mtime

    except Exception: pass

@client.event

async def on_message(msg: discord.Message):

    if msg.author.bot or not msg.content: return

    

    image_url = None

    if msg.attachments:

        valid = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

        for att in msg.attachments:

            if os.path.splitext(att.filename)[1].lower() in valid:

                image_url = att.url

                break

    if msg.reference:

        history, model = await recover_history(msg)

        if history:

            history.append({"role": "user", "content": msg.content})

            await process_ai_request(model, history, message=msg, user_id=msg.author.id, image_url=image_url)

async def model_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:

    return [

        app_commands.Choice(name=data["display_name"], value=model_id)

        for model_id, data in MODELS_CONFIG.items()

        if current.lower() in data["display_name"].lower()

    ][:25]

@tree.command(name="ai", description="Start talking to AI.")

@app_commands.describe(prompt="Message", model="Select a model", image="Attach image (Optional)")

@app_commands.autocomplete(model=model_autocomplete)

async def ai_slash(interaction: discord.Interaction, prompt: str, model: str = None, image: discord.Attachment = None):

    model = model or DEFAULT_MODEL

    if model not in MODELS_CONFIG: model = DEFAULT_MODEL

    

    image_url = None

    if image and os.path.splitext(image.filename)[1].lower() in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:

        image_url = image.url

        

    history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]

    await process_ai_request(model, history, interaction=interaction, user_id=interaction.user.id, image_url=image_url)

@tree.command(name="reload", description="[Admin] Reload Config")

@app_commands.default_permissions(administrator=True)

async def reload_cmd(interaction: discord.Interaction):

    load_models_config()

    await interaction.response.send_message("✅ Config Reloaded.", ephemeral=True)

if __name__ == "__main__":

    if not DISCORD_TOKEN: exit("❌ TOKEN missing")

    try: client.run(DISCORD_TOKEN)

    except KeyboardInterrupt: pass
