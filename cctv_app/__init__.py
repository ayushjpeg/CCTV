import os

from flask import Flask

from .config import (
    BASE_DIR,
    MOTION_CLIP_DIR,
    CLIP_RETENTION_SECONDS,
    CLIP_METADATA_FILENAME,
    CLIP_FAVORITES_LIMIT
)
from .extensions import socketio
from .routes import bp as routes_bp
from . import socket_handlers


def create_app():
    app = Flask(
        __name__,
        static_url_path='/static',
        static_folder=str(BASE_DIR / 'static'),
        template_folder=str(BASE_DIR / 'templates')
    )
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.secret_key = os.urandom(24)
    app.config['MOTION_CLIP_DIR'] = MOTION_CLIP_DIR
    app.config['CLIP_RETENTION_SECONDS'] = CLIP_RETENTION_SECONDS
    app.config['CLIP_METADATA_FILENAME'] = CLIP_METADATA_FILENAME
    app.config['CLIP_FAVORITES_LIMIT'] = CLIP_FAVORITES_LIMIT

    app.register_blueprint(routes_bp)

    socketio.init_app(app)
    socket_handlers.init_app(app)
    return app
