"""
MeowGG Configuration Module
Tracked directly in GitHub. Works on Vercel and local machines without any .env file needed.
"""
import os

# ==================== DISCORD LINKS & GUILD IDS ====================
DISCORD_SERVER_INVITE = os.getenv("DISCORD_SERVER_INVITE", "https://discord.gg/meowgg")
MEOW_DISCORD_SERVER_INVITE = os.getenv("MEOW_DISCORD_SERVER_INVITE", "https://discord.gg/meowgg")
GUILD_ID = os.getenv("GUILD_ID", "1445228461668040756")
MEOW_GUILD_ID = os.getenv("MEOW_GUILD_ID", "1535587594610544640")

# ==================== DISCORD OAUTH2 & BOT VERIFICATION ====================
# Discord Developer Portal: https://discord.com/developers/applications -> General Information / OAuth2
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "1561024986851057715")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "cga15Q7liKu_hImsb6VxEvaATKnG8Cg9")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", os.getenv("DISCORD_TOKEN", "MTU2MTAyNDk4Njg1MTA1NzcxNQ.Gd0-aO.VOQxZDeczALBuKuWvxvoPLonGAZdB7Njq3lsrY"))
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")
REQUIRE_DISCORD_LOGIN = os.getenv("REQUIRE_DISCORD_LOGIN", "true").lower() in ("true", "1", "yes")

# ==================== WEB SESSION SECRET & PORT ====================
SECRET_KEY = os.getenv("SECRET_KEY", "meowgg_secure_session_key_2026")
PORT = int(os.getenv("PORT", 5000))

# ==================== UPSTREAM WHITELIST API CONFIG ====================
# Embedded directly so GitHub repo deployed to Vercel works immediately
API_KEY = os.getenv("API_KEY", "GTCAPI-BB05166D26D7EC81F86BA3C893998613")
API_URL = os.getenv("API_URL", "https://gtccheats.xyz/Api/uidbypassapi/api_user.php")

API_ACTION_ADD = os.getenv("API_ACTION_ADD", "add")
API_ACTION_EDIT = os.getenv("API_ACTION_EDIT", "change_uid")
API_ACTION_DELETE = os.getenv("API_ACTION_DELETE", "delete")

# ==================== ADMIN CONTROL PANEL (/admin) ====================
ADMIN_PANEL_USER = os.getenv("ADMIN_PANEL_USER", "72")
ADMIN_PANEL_PASS = os.getenv("ADMIN_PANEL_PASS", "12")
