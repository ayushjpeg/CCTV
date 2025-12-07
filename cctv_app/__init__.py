import os

from flask import Flask

from .config import MOTION_CLIP_DIR
from .extensions import socketio
from .routes import bp as routes_bp
from . import socket_handlers


def create_app():
    app = Flask(__name__, static_url_path='/static', static_folder='static')
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.secret_key = os.urandom(24)
    app.config['MOTION_CLIP_DIR'] = MOTION_CLIP_DIR

    app.register_blueprint(routes_bp)

    socketio.init_app(app)
    socket_handlers.init_app(app)
    return app
