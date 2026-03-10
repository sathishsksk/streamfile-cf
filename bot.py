@bot.on_message()
async def debug_all(_, msg):
    log.info(f"📨 Update received: chat={msg.chat.id} type={msg.chat.type} text={msg.text}")
