import os
import json
import logging
import asyncio
from http import HTTPStatus
from typing import Optional, Union
from dotenv import load_dotenv
from pyrogram.types import Message, Document, Video, Audio
from pyrogram.handlers import MessageHandler, EditedMessageHandler
from pyrogram import Client, enums, errors, idle, filters
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
web_app = web.Application()
routes = web.RouteTableDef()
loop = asyncio.get_event_loop()
lock = asyncio.Lock()
CONFIG_FILE_URL = os.getenv("CONFIG_FILE_URL")
SERVER_PORT = int(os.environ.get('SERVER_PORT', 8080))
BOT_TOKEN = None
TG_API_ID = None
TG_API_HASH = None
AUTHORIZED_USERS = list()
FILE_LINK_DICT = dict()
bot: Optional[Client] = None
server: Optional[web.AppRunner] = None


async def setup_config():
    global BOT_TOKEN
    global TG_API_ID
    global TG_API_HASH
    global AUTHORIZED_USERS
    if CONFIG_FILE_URL is not None:
        logger.info("Downloading config file")
        try:
            async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.get(url=CONFIG_FILE_URL) as response:
                    if response.ok:
                        with open('config.env', 'wt', encoding='utf-8') as f:
                            f.write(await response.text(encoding='utf-8'))
                        logger.info("Loading config values")
                        if load_dotenv('config.env', override=True):
                            BOT_TOKEN = os.getenv(key='BOT_TOKEN')
                            TG_API_ID = os.getenv(key='TG_API_ID')
                            TG_API_HASH = os.getenv(key='TG_API_HASH')
                            AUTHORIZED_USERS = json.loads(os.environ['USER_LIST'])
                    else:
                        logger.error("Error while downloading config file")
        except TimeoutError:
            logger.error("Failed to download config file")
    else:
        logger.error("CONFIG_FILE_URL is None")


@routes.get("/")
async def root_route(request: web.Request):
    return web.json_response({
        "msg": "Hello from TG File Listener Service"
    })


@routes.get("/status")
async def status_route(request: web.Request):
    resp = {
        'bot': {},
        'server': {}
    }
    try:
        if bot and bot.me:
            resp['bot']['msg'] = f'Bot is running with username:: {bot.me.username}'
            resp['bot']['device'] = bot.device_model
            resp['bot']['version'] = bot.system_version
        else:
            resp['bot']['msg'] = 'pyrogram session is not initialized properly'
    except errors.RPCError as e:
        resp['bot']['msg'] = 'pyrogram session not initialized'
        resp['bot']['error'] = e.MESSAGE
    if await ping_server() is HTTPStatus.OK:
        resp['server']['msg'] = f'Web server is running on port:: {SERVER_PORT}'
    else:
        resp['server']['msg'] = 'Web server is not reachable'
    return web.json_response(resp)


@routes.get('/getLink/{file_id}')
async def fetch_link(request: web.Request):
    try:
        file_id = request.match_info['file_id']
        logger.info(f"Searching download link for:: {file_id}")
    except KeyError:
        logger.error("No file_id provided")
        return web.json_response(data={
            'error': 'no fileId is provided'
        }, status=HTTPStatus.BAD_REQUEST.value)
    else:
        async with lock:
            if file_id in FILE_LINK_DICT:
                logger.info(f"Found link for:: {file_id}")
                return web.json_response(data={
                    'fileId': file_id,
                    'fileLink': FILE_LINK_DICT[file_id]
                }, status=HTTPStatus.OK.value)
            else:
                logger.error(f"No link found for:: {file_id}")
                return web.json_response(data={
                    'error': 'No link found for given fileId'
                }, status=HTTPStatus.BAD_REQUEST.value)


async def ping_server():
    status = HTTPStatus.INTERNAL_SERVER_ERROR
    try:
        async with ClientSession(timeout=ClientTimeout(total=5)) as session:
            async with session.get(f'http://localhost:{SERVER_PORT}') as resp:
                logger.info(f"Pinged server with response:: {resp.status}")
                status = HTTPStatus.OK
    except TimeoutError:
        logger.warning("Couldn't connect to the site URL..")
    except Exception as e:
        logger.error(f"Unexpected error while ping:: {e.__class__.__name__}", exc_info=True)
    return status


async def start_msg(client: Client, message: Message):
    logger.info(f"/start sent by:: {message.chat.id if not message.chat.username else message.chat.username}")
    username = f"<b><i>{message.chat.username}</i></b>" if message.chat.username else ''
    msg = (f"Hey {username}\nWelcome to <b>File Listener Bot</b>.\nAdd me to a Telegram Channel and I will monitor for "
           f"document & video files sent into it. I will store the file details and serve them using API.")
    try:
        await message.reply(text=msg, quote=True, disable_notification=True)
    except errors.RPCError as er:
        logger.error(f"Failed to send start message:: [{er.CODE}] {er.NAME} [{er.MESSAGE}]")


async def file_listener(client: Client, message: Message):
    global FILE_LINK_DICT
    file_obj: Optional[Union[Document, Video, Audio]] = None
    logger.info(f"Received file of type:: {'UNKNOWN' if message.media is None else message.media.name}")
    try:
        if message.document:
            file_obj = message.document
        elif message.video:
            file_obj = message.video
        elif message.audio:
            file_obj = message.audio
        else:
            logger.error("Unable to determine file type")
        if file_obj:
            logger.info(f"FileName:: {file_obj.file_name} FileUniqueId:: {file_obj.file_unique_id}")
            if message.reply_markup and message.reply_markup.inline_keyboard:
                for button_list in message.reply_markup.inline_keyboard:
                    for button in button_list:
                        if button.text and "DL Link" in button.text and button.url:
                            logger.info(f"Found download link:: {button.url}")
                            async with lock:
                                FILE_LINK_DICT[file_obj.file_unique_id] = {
                                    "fileName": file_obj.file_name,
                                    "downloadLink": button.url
                                }
                            logger.info(f"Updated FILE_LINK_DICT current size is:: {len(FILE_LINK_DICT)}")
            else:
                logger.error(f"No reply_markup found for:: {file_obj.file_name}")
    except errors.RPCError as e:
        logger.error(f"Failed to get file info:: {e.MESSAGE}")
    except (KeyError, ValueError, AttributeError):
        logger.error("Failed to update the FILE_LINK_DICT")


async def start_services():
    global bot
    global server
    await setup_config()
    logger.info("Creating bot client")
    try:
        bot = Client(
            name="FileListenerBot",
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
            bot_token=BOT_TOKEN,
            no_updates=False,
            parse_mode=enums.ParseMode.HTML,
            in_memory=False,
            takeout=False,
            max_concurrent_transmissions=1)
        await bot.start()
        logger.info(f"Initialized bot:: {bot.me.username}")
        logger.info("Registering bot command handlers")
        bot.add_handler(MessageHandler(callback=start_msg,
                                       filters=filters.command("start") & filters.chat(AUTHORIZED_USERS)))
        bot.add_handler(EditedMessageHandler(callback=file_listener,
                                             filters=(filters.audio | filters.video | filters.document) &
                                             filters.chat(AUTHORIZED_USERS) & (filters.channel | filters.private)))
    except ConnectionError:
        logger.error("Pyrogram session already started, terminate that one to continue")
    except errors.RPCError as e:
        logger.error(f"Failed to start pyrogram session, error:: {e.MESSAGE}")
    logger.info("Setting up web server")
    web_app.add_routes(routes)
    server = web.AppRunner(web_app)
    await server.setup()
    await web.TCPSite(runner=server, host='0.0.0.0', port=SERVER_PORT).start()
    logger.info(f"Web server started on port:: {SERVER_PORT}")
    await idle()


async def cleanup():
    if server is not None and bot is not None:
        await server.cleanup()
        await bot.stop()
        logger.info("Web server and bot stopped")
    else:
        logger.warning("Unable to run cleanup process")

if __name__ == "__main__":
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        pass
    except Exception as err:
        logger.error(err.with_traceback(None))
    finally:
        loop.run_until_complete(cleanup())
        loop.stop()
        logger.info("Stopped Services")
