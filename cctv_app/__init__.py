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
import threading
import time


def _start_cleanup_thread(app, interval=3600):
    """Start a background daemon thread that periodically runs cleanup_expired_clips.

    Runs every `interval` seconds while the process is alive. Uses app.app_context
    so `current_app` and config are available to the cleanup routine.
    """
    def _worker():
        from .routes import cleanup_expired_clips
        while True:
            try:
                with app.app_context():
                    cleanup_expired_clips()
            except Exception:
                # Don't let background failures stop the loop; log via print as fallback
                try:
                    app.logger.exception('Periodic clip cleanup failed')
                except Exception:
                    pass
            time.sleep(interval)

    t = threading.Thread(target=_worker, name='clip-cleanup-thread', daemon=True)
    t.start()


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
    # start periodic cleanup (runs hourly by default)
    try:
        _start_cleanup_thread(app)
    except Exception:
        app.logger.exception('Failed to start cleanup thread')
    return app
