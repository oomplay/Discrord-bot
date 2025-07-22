#bot.py
import discord

from discord import app_commands

from discord.ext import tasks

import aiohttp

import asyncio

import os

import logging

import time

import datetime

from collections import OrderedDict

from dotenv import load_dotenv

# Load environment variables from .env file

load_dotenv()

# ────── ⚙️ Config ──────

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

PPQ_API_KEY = os.getenv("PPQ_API_KEY")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

OPENROUTER_API_KEY =os.getenv("OPENROUTER_API_KEY")

DEFAULT_MODEL = "gemini-2.0-flash"

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT")

CONVERSATION_HINT = "[หากต้องการคุยต่อ ให้ Reply ที่ข้อความนี้]"

AI_ICON_URL = "https://raw.githubusercontent.com/oomplay/Discrord-bot/main/image/image.png"

# Conversation cache settings

CONVERSATION_CACHE_TTL = 7200

MAX_CONVERSATION_CACHE = 200

# ────── 🧬 Model Configuration (Single Source of Truth) ──────

MODELS_CONFIG = {

    "gemini-2.0-flash":  {"provider": "gemini", "display_name": "🌟 Gemini-2.0-flash -Default"},

    "gemini-2.5-flash": {"provider": "gemini", "display_name": "💫 Gemini-2.5-flash"},

    "gpt-4.1-nano": {"provider": "ppq", "display_name": "🧠 GPT-4.1 Nano"},

    "gpt-4.1-mini": {"provider": "ppq", "display_name": "💡 GPT-4.1 Mini"},

    "gpt-4.1":      {"provider": "ppq", "display_name": "💪 GPT-4.1"},

    "claude-3.7-sonnet": {"provider": "ppq", "display_name": "👨‍💻 Claude 3.7"},

    "claude-sonnet-4":   {"provider": "ppq", "display_name": "🔥 Claude 4"},
    
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free":   {"provider": "openrouter", "display_name": "🐟 dolphin-mistral-24b[UNCEN]"},

}

MODEL_ROUTE = {model_id: data["provider"] for model_id, data in MODELS_CONFIG.items()}

AI_MODEL_CHOICES = [

    app_commands.Choice(name=data["display_name"], value=model_id)

    for model_id, data in MODELS_CONFIG.items()

]

# ────── 🌐 Networking & Cache ──────

session: aiohttp.ClientSession = None

SEM = asyncio.Semaphore(128)

conversation_cache: OrderedDict[int, tuple] = OrderedDict()

class APIError(Exception):

    pass

# ────── Discord client setup ──────

intents = discord.Intents.default()

intents.message_content = True

client = discord.Client(intents=intents)

tree = app_commands.CommandTree(client)

# ────── Logging ──────

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ────── Core Logic (API Callers & Dispatch) ──────

async def call_api(url, headers, payload, path, retries=2):

    # Ensure session is available before making a call

    if not session or session.closed:

        logging.error("❌ aiohttp.ClientSession is not available or closed.")

        raise APIError("เกิดข้อผิดพลาดในการเชื่อมต่อภายใน (Session Closed)")

    for attempt in range(retries + 1):

        try:

            async with SEM, session.post(url, headers=headers, json=payload, timeout=90) as resp:

                if resp.status == 200:

                    data = await resp.json()

                    try:

                        for key in path: data = data[key]

                        return str(data)

                    except (KeyError, TypeError, IndexError) as e:

                        logging.error(f"❌ Error navigating API response path: {e}\nResponse: {await resp.text()}")

                        raise APIError("เกิดข้อผิดพลาดในการอ่านข้อมูลจาก AI: `path error`")

                else:

                    error_text = await resp.text()

                    logging.warning(f"⚠️ API {resp.status}: {error_text}")

                    raise APIError(f"API Error {resp.status}: `{error_text[:1000]}`")

        except asyncio.TimeoutError:

            logging.warning(f"🔁 API call timed out. Retry {attempt+1}/{retries}")

        except aiohttp.ClientError as e: # Catch session-related errors

            logging.warning(f"🔁 Network/Session error on attempt {attempt+1}/{retries}: {e}")

        except Exception as e:

            if isinstance(e, APIError): raise e

            logging.warning(f"🔁 Retry {attempt+1}/{retries}: {e}")

            await asyncio.sleep(0.5 * (attempt + 1))

    raise APIError("AI ไม่ตอบสนองหลังจากพยายามเชื่อมต่อหลายครั้ง")

async def get_ai_response(history: list, model: str) -> str:

    provider = MODEL_ROUTE.get(model, "ppq")

    formatted_history = history

    if provider == "ppq":

        return await call_ppq_api(formatted_history, model)

    elif provider == "gemini":

        formatted_history = [

            {"role": "model" if item["role"] == "assistant" else "user", "parts": [{"text": item["content"]}]}
            for item in history

        ]

        return await call_gemini_api(formatted_history, model)

    elif provider == "openrouter":
        
        return await call_openrouter_api(formatted_history, model)

    else:

        raise APIError(f"ไม่รู้จัก provider ของโมเดลนี้: {provider}")

async def call_ppq_api(history: list, model: str):

    url = "https://api.ppq.ai/chat/completions"

    headers = {"Authorization": f"Bearer {PPQ_API_KEY}", "Content-Type": "application/json"}

    payload = {"model": model, "messages": history, "stream": False}

    return await call_api(url, headers, payload, path=["choices", 0, "message", "content"])

async def call_gemini_api(history: list, model: str):

    base = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    url = f"{base}?key={GEMINI_API_KEY}"

    headers = {"Content-Type": "application/json"}

    payload = {"contents": history}

    return await call_api(url, headers, payload, path=["candidates", 0, "content", "parts", 0, "text"])

async def call_openrouter_api(history: list, model: str):
    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://discord.com", "X-Title": "ฺDiscrord-bot"}

    payload = {
        "model": model, "messages": history, "stream": False}

    return await call_api(url, headers, payload, path=["choices", 0, "message", "content"])


# ────── Central Request Processor (Handles UI, Errors, and Cache) ──────

async def process_ai_request(model: str, history: list, interaction: discord.Interaction = None, message: discord.Message = None):

    sent_message = None

    reply_text = None

    async def get_and_build_reply():

        nonlocal reply_text

        start_time = time.time()

        reply_text = await get_ai_response(history, model)

        history.append({"role": "assistant", "content": reply_text})

        duration = time.time() - start_time

        

        embed = discord.Embed(

            color=discord.Color.from_str("#4e8df7"),

            description=reply_text[:4096],

            timestamp=datetime.datetime.now()

        )

        embed.set_author(name=f"คำตอบจาก: {MODELS_CONFIG.get(model, {'display_name': model.title()})['display_name']}", icon_url=AI_ICON_URL)

        embed.set_footer(text=f"ประมวลผลใน {duration:.2f} วินาที • {CONVERSATION_HINT}")

        return embed

    try:

        if interaction:

            # Handle the case where the interaction expires before we can defer.

            try:

                await interaction.response.defer(thinking=True)

            except discord.errors.NotFound:

                logging.warning(f"Interaction {interaction.id} expired before defer(). Aborting request.")

                # We can't send a message back because the interaction is dead. Just stop.

                return

            embed = await get_and_build_reply()

            await interaction.edit_original_response(embed=embed, content=None)

            sent_message = await interaction.original_response()

        

        elif message:

            async with message.channel.typing():

                embed = await get_and_build_reply()

            sent_message = await message.reply(embed=embed, mention_author=False) # Changed to reply for better context

    except APIError as e:

        logging.error(f"API Error during request processing: {e}")

        error_embed = discord.Embed(title="เกิดข้อผิดพลาด", description=str(e), color=discord.Color.red())

        if interaction and not interaction.is_expired():

            await interaction.edit_original_response(embed=error_embed, content=None)

        elif message:

            await message.channel.send(embed=error_embed)

    except Exception as e:

        logging.critical(f"An unexpected error occurred: {e}", exc_info=True)

        error_embed = discord.Embed(title="เกิดข้อผิดพลาดที่ไม่คาดคิด", description="เกิดปัญหาบางอย่างในระบบ โปรดลองอีกครั้ง", color=discord.Color.dark_red())

        if interaction and not interaction.is_expired():

            await interaction.edit_original_response(embed=error_embed, content=None)

        elif message:

            await message.channel.send(embed=error_embed)

    if sent_message and reply_text is not None:

        conversation_cache[sent_message.id] = ({'history': history, 'model': model}, time.time())

        if len(conversation_cache) > MAX_CONVERSATION_CACHE:

            conversation_cache.popitem(last=False)

# ────── Discord Events & Commands ──────

# Use setup_hook to create the session ONCE.

# This runs once before the bot logs in and is the correct place for setup.

@client.event

async def setup_hook():

    global session

    session = aiohttp.ClientSession()

    logging.info("✅ aiohttp session created successfully.")

@client.event

async def on_ready():

    # Session creation is now in setup_hook.

    await tree.sync()

    if not clear_expired_conversations.is_running():

        clear_expired_conversations.start()

    logging.info(f"✅ Logged in as {client.user}")

    logging.info(f"🌴 Synced {len(await tree.fetch_commands())} application commands.")

    logging.info(f"🧹 Cache clearing task started. Checking every 30 minutes.")

@tasks.loop(minutes=30)

async def clear_expired_conversations():

    now = time.time()

    expired_keys = [k for k, v in conversation_cache.items() if now - v[1] > CONVERSATION_CACHE_TTL]

    if expired_keys:

        for k in expired_keys: del conversation_cache[k]

        logging.info(f"🧹 Cleared {len(expired_keys)} expired conversation(s) from cache.")

@client.event

async def on_message(msg: discord.Message):

    if msg.author.bot or not msg.content:

        return

    # Case 1: Continuing a conversation via Reply

    if msg.reference and msg.reference.resolved and msg.reference.resolved.author == client.user:

        ref_id = msg.reference.message_id

        if ref_id in conversation_cache:

            logging.info(f"💬 Continuing conversation from message {ref_id}")

            cached_data, _ = conversation_cache[ref_id]

            history = cached_data['history']

            model = cached_data['model']

            history.append({"role": "user", "content": msg.content})

            

            await process_ai_request(model=model, history=history, message=msg)

            return

        else:

            logging.info(f"Tried to reply to an old/uncached message {ref_id}.")

            await msg.reply("😕 ขออภัย บทสนทนานี้หมดอายุแล้วหรือไม่พบในระบบ\nกรุณาเริ่มต้นใหม่ด้วย `/ai` หรือ `!ai` ครับ", mention_author=False)

            return

    # Case 2: Starting a new conversation with !ai

    elif msg.content.startswith("!ai"):

        args = msg.content[len("!ai"):].strip()

        if not args:

            await msg.channel.send("💬 ใช้ `!ai <ข้อความ>` หรือ `!ai m:<model> <ข้อความ>`")

            return

        

        prompt: str

        model: str

        if args.startswith("m:"):

            parts = args.split(" ", 1)

            model_str = parts[0][2:].strip()

            if len(parts) != 2 or not model_str or not parts[1].strip() or model_str not in MODELS_CONFIG:

                await msg.channel.send(f"❌ ใช้ `!ai m:<model> <ข้อความ>` และตรวจสอบว่า model (`{model_str}`) ถูกต้อง")

                return

            model, prompt = model_str, parts[1].strip()

        else:

            model, prompt = DEFAULT_MODEL, args

        

        history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]

        await process_ai_request(model=model, history=history, message=msg)

        return

@tree.command(name="ai", description="ถาม AI (สามารถ Reply เพื่อคุยต่อได้)")

@app_commands.describe(prompt="ข้อความที่ต้องการถาม AI", model="เลือกรุ่นโมเดล")

@app_commands.choices(model=AI_MODEL_CHOICES)

async def ai_slash(interaction: discord.Interaction, prompt: str, model: str = None):

    model = model or DEFAULT_MODEL

    history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]

    await process_ai_request(model=model, history=history, interaction=interaction)

#  Removed the on_disconnect event.

# It was causing the "Session is closed" error on reconnects.

# The session created in setup_hook will be properly closed by discord.py on shutdown.

if __name__ == "__main__":

    if not DISCORD_TOKEN:

        logging.error("❌ DISCORD_TOKEN environment variable not set!")

        exit(1)

    if not PPQ_API_KEY: logging.warning("⚠️ PPQ_API_KEY not set. Some models will not work.")

    if not GEMINI_API_KEY: logging.warning("⚠️ GEMINI_API_KEY not set. Some models will not work.")

        

    try:

        client.run(DISCORD_TOKEN, log_handler=None)

    except Exception as e:

        logging.error(f"❌ Failed to start bot: {e}", exc_info=True)
