import asyncio
from aiohttp import web
import aiohttp
import aiohttp_jinja2
import jinja2
import json
import os
import traceback

import appdaemon.homeassistant as ha
import appdaemon.conf as conf
import appdaemon.dashboard as dashboard

# Setup WS handler

app = web.Application()
app['websockets'] = {}

def set_paths():

    if not os.path.exists(conf.compile_dir):
        os.makedirs(conf.compile_dir)
        
    if not os.path.exists(os.path.join(conf.compile_dir, "javascript")):
        os.makedirs(os.path.join(conf.compile_dir, "javascript"))

    if not os.path.exists(os.path.join(conf.compile_dir, "css")):
        os.makedirs(os.path.join(conf.compile_dir, "css"))
        
    conf.javascript_dir = os.path.join(conf.dash_dir, "assets", "javascript")
    conf.compiled_javascript_dir = os.path.join(conf.compile_dir, "javascript")
    conf.template_dir = os.path.join(conf.dash_dir, "assets", "templates")
    conf.css_dir = os.path.join(conf.dash_dir, "assets", "css")
    conf.compiled_css_dir = os.path.join(conf.compile_dir, "css")
    conf.fonts_dir = os.path.join(conf.dash_dir, "assets", "fonts")
    conf.images_dir = os.path.join(conf.dash_dir, "assets", "images")
    conf.base_url = "http://{}:{}".format(conf.dash_host, conf.dash_port)
    conf.stream_url = "ws://{}:{}/stream".format(conf.dash_host, conf.dash_port)

# Views

@asyncio.coroutine
@aiohttp_jinja2.template('dashboard.jinja2')
def list_dash(request):
    dash_list = dashboard.list_dashes()
    params = {"dash_list": dash_list, "stream_url": conf.stream_url}
    params["main"] = "1"
    return params

@asyncio.coroutine
@aiohttp_jinja2.template('dashboard.jinja2')
def load_dash(request):
    name = request.match_info.get('name', "Anonymous")

    # Set correct skin
    
    if "skin" in request.rel_url.query:
        skin = request.rel_url.query["skin"]
    else:
        skin = "default"

    #
    # Check skin exists
    #
    skindir = os.path.join(conf.config_dir, "custom_css", skin)
    if os.path.isdir(skindir):
        ha.log(conf.logger, "INFO", "Loading custom skin '{}'".format(skin))
    else:
        # Not a custom skin, try product skins
        skindir = os.path.join(conf.css_dir, skin)
        if not os.path.isdir(skindir):
            ha.log(conf.logger, "WARNING", "Skin '{}' does not exist".format(skin))
            skin = "default"
            skindir = os.path.join(conf.css_dir, "default")
    print(skindir)

    #
    # Conditionally compile Dashboard
    #
    
    dash = dashboard.compile_dash(name, skin, skindir)
    if dash == None:
        errors = []
        includes = []
    else:
        errors = dash["errors"]
        if "includes" in dash:
            includes = dash["includes"]
        else:
            includes = []

    if "widgets" in dash:
        widgets = dash["widgets"]
    else:
        widgets = {}
    #
    #return params
    #
    return {"errors": errors, "name": name.lower(), "skin": skin, "widgets": widgets, "includes": includes}

@asyncio.coroutine
def get_state(request):
    entity = request.match_info.get('entity')
    
    # Groups don't have the kind of state we need, so find a group member and
    # Substitute its state instead. 
    # This is a fix for controlling groups of lights
    
    if entity in conf.ha_state:
        parts = entity.split(".")
        if parts[0] == "group":
            # pick the first group member
            sub_entity = conf.ha_state[entity]["attributes"]["entity_id"][0]
            state = conf.ha_state[sub_entity]
        else:
            state = conf.ha_state[entity]
    else:
        state = None

    return web.json_response({"state": state})
    
@asyncio.coroutine
def call_service(request):
    data = yield from request.post()
    ha.call_service(**request.POST)
    return web.Response(status = 200)
    
@asyncio.coroutine
def not_found(request):
    return web.Response(status = 404)
    
# Websockets Handler

@asyncio.coroutine
def on_shutdown(app):
    for ws in app['websockets']:
        yield from ws.close(code=WSCloseCode.GOING_AWAY,
                       message='Server shutdown')
        
@asyncio.coroutine
def wshandler(request):
    ws = web.WebSocketResponse()
    yield from ws.prepare(request)

    request.app['websockets'][ws] = {}
    try:
        while True:
            msg = yield from ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                ha.log(conf.logger, "INFO", 
                       "New dashboard connected: {}".format(msg.data))
                request.app['websockets'][ws]["dashboard"] =  msg.data
            elif msg.type == aiohttp.WSMsgType.ERROR:
                ha.log(conf.logger, "INFO", 
                "ws connection closed with exception {}".format(ws.exception()))       
    except: 
                ha.log(conf.logger, "INFO", "Dashboard disconnected")
    finally:
        request.app['websockets'].pop(ws, None)

    return ws

def ws_update(data):
    ha.log(conf.logger, 
           "DEBUG", 
           "Sending data to {} dashes: {}".format(len(app['websockets']), 
           data))
           
    for ws in app['websockets']:
        ha.log(conf.logger, 
           "DEBUG", 
           "Found dashboard type {}".format(app['websockets'][ws]["dashboard"]))
        ws.send_str(json.dumps(data))
    
#Routes, Status and Templates

def setup_routes():
    app.router.add_get('/favicon.ico', not_found)
    app.router.add_get('/stream', wshandler)
    app.router.add_post('/call_service', call_service)
    app.router.add_get('/state/{entity}', get_state)
    app.router.add_get('/', list_dash)
    app.router.add_get('/{name}', load_dash)

   # Setup Templates
    aiohttp_jinja2.setup(app,
        loader=jinja2.FileSystemLoader(conf.template_dir))

    # Add static path for JavaScript
    
    app.router.add_static('/javascript', conf.javascript_dir)
    app.router.add_static('/compiled_javascript', conf.compiled_javascript_dir)
    
    # Add static path for css
    app.router.add_static('/css', conf.css_dir)
    app.router.add_static('/compiled_css', conf.compiled_css_dir)

    # Add static path for fonts
    app.router.add_static('/fonts', conf.fonts_dir)

    # Add static path for images
    app.router.add_static('/images', conf.images_dir)

# Setup  
  
def run_dash(loop):

    try:
        set_paths()
        setup_routes()    
        
        handler = app.make_handler()
        f = loop.create_server(handler, conf.dash_host, int(conf.dash_port))
        srv = loop.run_until_complete(f)
        ha.log(conf.logger, "INFO", 
               "Listening on {}".format(srv.sockets[0].getsockname()))
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.close()
            loop.run_until_complete(srv.wait_closed())
            loop.run_until_complete(app.shutdown())
            loop.run_until_complete(handler.shutdown(60.0))
            loop.run_until_complete(app.cleanup())
        loop.close()
    except:
        ha.log(conf.logger, "WARNING", '-' * 60)
        ha.log(conf.logger, "WARNING", "Unexpected error in dashboard thread")
        ha.log(conf.logger, "WARNING", '-' * 60)
        ha.log(conf.logger, "WARNING", traceback.format_exc())
        ha.log(conf.logger, "WARNING", '-' * 60)
    
