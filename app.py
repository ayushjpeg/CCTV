import os
import time
import threading
from functools import wraps
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, session, Response, abort, jsonify
from werkzeug.security import check_password_hash
from streams import Camera

# Configure Flask to serve static under /CCTV so nginx can proxy /CCTV/ -> app
app = Flask(__name__, static_url_path='/CCTV/static', static_folder='static')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.secret_key = os.environ.get('CCTV_SECRET_KEY', os.urandom(24))

# Password hash must be provided via env var CCTV_PASSWORD_HASH (use create_password.py to generate)
PASSWORD_HASH = os.environ.get('CCTV_PASSWORD_HASH')

# Feed key for the feeder laptop (set this on server and feeder)
FEED_KEY = os.environ.get('CCTV_FEED_KEY')

# Camera index or device path (fallback if no feeder pushing frames)
CAMERA_INDEX = os.environ.get('CAMERA_INDEX', '0')

# In-memory frame buffers (latest pushed frame per camera_id) and synchronization
LATEST_FRAMES = {}  # camera_id -> bytes
LAST_FEEDS = {}     # camera_id -> timestamp
FRAME_LOCK = threading.Lock()

# Simple auth decorator
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route('/CCTV/')
def index():
    """Landing page: lets the user choose Camera (act as feeder) or Viewer (see cameras).
    Viewer will be directed to login if not authenticated.
    """
    return render_template('index.html')


@app.route('/CCTV/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if not PASSWORD_HASH:
            return "Server password not configured. Set CCTV_PASSWORD_HASH env var.", 500
        password = request.form.get('password', '')
        if check_password_hash(PASSWORD_HASH, password):
            session['logged_in'] = True
            next_url = request.args.get('next') or url_for('index')
            return redirect(next_url)
        else:
            return render_template('login.html', error='Invalid password')

    return render_template('login.html')


@app.route('/CCTV/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/CCTV/stream')
@login_required
def stream_page():
    return render_template('stream.html')


def gen(camera: Camera, camera_id: str = None):
    """Stream generator. If camera_id is provided and a pushed frame exists for that id,
    yield the pushed frame; otherwise fall back to the provided local camera.
    """
    while True:
        frame = None

        if camera_id:
            with FRAME_LOCK:
                frame = LATEST_FRAMES.get(camera_id)

        # Otherwise fall back to local camera
        if frame is None and camera is not None:
            frame = camera.get_frame()

        if frame is None:
            time.sleep(0.1)
            continue

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')


# Video feed endpoint
@app.route('/CCTV/video_feed')
@login_required
def video_feed():
    # Stream a specific camera. Request should include ?camera=<id>
    camera_id = request.args.get('camera')

    cam = None
    # If no camera_id provided, use the server's local camera
    if not camera_id:
        cam = Camera(camera_index=CAMERA_INDEX)

    return Response(gen(cam, camera_id=camera_id), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/CCTV/push_frame', methods=['POST'])
def push_frame():
    """Endpoint for feeder devices to POST JPEG frames.

    Expected identification: header 'X-Cam-ID' or form/query 'camera_id'.
    If CCTV_FEED_KEY is set on the server, the feeder must provide header 'X-Feed-Key' or form 'feed_key'.
    Accepts multipart file 'frame' or raw JPEG body.
    """
    # Validate feed key only if server requires one
    if FEED_KEY:
        provided = request.headers.get('X-Feed-Key') or request.form.get('feed_key')
        if not provided or provided != FEED_KEY:
            return ('Forbidden', 403)

    # Determine camera id
    camera_id = request.headers.get('X-Cam-ID') or request.form.get('camera_id') or request.args.get('camera')
    if not camera_id:
        return ('camera_id required (header X-Cam-ID or field camera_id)', 400)

    data = None
    f = request.files.get('frame') if hasattr(request, 'files') else None
    if f:
        data = f.read()
    else:
        data = request.get_data()

    if not data:
        return ('No frame', 400)

    with FRAME_LOCK:
        LATEST_FRAMES[camera_id] = data
        LAST_FEEDS[camera_id] = time.time()

    return ('', 204)


@app.route('/CCTV/feed_status')
def feed_status():
    """Return JSON status about all feeders/cameras.

    Returns a dict camera_id -> { age, alive }
    """
    now = time.time()
    out = {}
    with FRAME_LOCK:
        for cam_id, ts in LAST_FEEDS.items():
            age = now - ts
            out[cam_id] = {
                'age': age,
                'alive': age < 10.0
            }
    return jsonify(out)


# Simple health endpoint
@app.route('/CCTV/health')
def health():
    return 'OK', 200


if __name__ == '__main__':
    # for local dev only
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True)
