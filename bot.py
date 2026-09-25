import os
import asyncio
import sqlite3
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes

client = genai.Client()
MODEL = "gemini-3-flash-preview"

# --- memory ---
conn = sqlite3.connect("agent_memory.db", check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS memory (
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")


def remember(key: str, value: str) -> str:
    conn.execute(
        "INSERT OR REPLACE INTO memory VALUES (?, ?, datetime('now'))", (key, value))
    conn.commit()
    return "saved"


def recall(key: str) -> str:
    row = conn.execute(
        "SELECT value FROM memory WHERE key=?", (key,)).fetchone()
    return row[0] if row else "nothing stored under that key"


def forget(key: str) -> str:
    conn.execute("DELETE FROM memory WHERE key=?", (key,))
    conn.commit()
    return "deleted"


TOOL_FUNCTIONS = {"remember": remember, "recall": recall, "forget": forget}
# add send_email, delete_file, etc. here later, same pattern
RISKY_TOOLS = {"forget"}

tools = [
    {"type": "google_search"},
    {"type": "function", "name": "remember", "description": "Save a fact for later",
     "parameters": {"type": "object", "properties": {
         "key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    {"type": "function", "name": "recall", "description": "Retrieve a previously saved fact",
     "parameters": {"type": "object", "properties": {
         "key": {"type": "string"}}, "required": ["key"]}},
    {"type": "function", "name": "forget", "description": "Permanently delete a saved fact",
     "parameters": {"type": "object", "properties": {
         "key": {"type": "string"}}, "required": ["key"]}},
]

# --- safety gate ---
PENDING_CONFIRM = {}


async def ask_confirmation(bot, chat_id, name, tool_args):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data="confirm_yes"),
        InlineKeyboardButton("No", callback_data="confirm_no"),
    ]])
    event = asyncio.Event()
    PENDING_CONFIRM[chat_id] = {"event": event, "approved": False}
    await bot.send_message(chat_id, f"Run {name} with {tool_args}?", reply_markup=keyboard)
    await event.wait()
    return PENDING_CONFIRM.pop(chat_id)["approved"]


async def handle_confirmation(
        update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if chat_id in PENDING_CONFIRM:
        PENDING_CONFIRM[chat_id]["approved"] = (query.data == "confirm_yes")
        PENDING_CONFIRM[chat_id]["event"].set()
    await query.edit_message_text("Got it." if query.data == "confirm_yes" else "Cancelled.")

# --- the agent loop ---
LAST_INTERACTION = {}


async def run_agent(bot, chat_id: int, goal: str) -> str:
    interaction = client.interactions.create(
        model=MODEL, input=goal, tools=tools,
        previous_interaction_id=LAST_INTERACTION.get(chat_id)
    )
    while True:
        fc_steps = [s for s in interaction.steps if s.type == "function_call"]
        if not fc_steps:
            LAST_INTERACTION[chat_id] = interaction.id
            return interaction.output_text

        step = fc_steps[0]
        if step.name in RISKY_TOOLS:
            approved = await ask_confirmation(bot, chat_id, step.name, step.arguments)
            output = TOOL_FUNCTIONS[step.name](
                **step.arguments) if approved else "User declined this action."
        else:
            output = TOOL_FUNCTIONS[step.name](**step.arguments)

        interaction = client.interactions.create(
            model=MODEL, tools=tools,
            previous_interaction_id=interaction.id,
            input=[{"type": "function_result", "name": step.name,
                    "call_id": step.id, "result": [{"type": "text", "text": str(output)}]}]
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await run_agent(context.bot, update.effective_chat.id, update.message.text)
    await update.message.reply_text(reply)

print("Bot is running...")
app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message))
app.add_handler(CallbackQueryHandler(handle_confirmation))
app.run_polling()
