import os
import time
import threading
from functools import wraps
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, session, Response, abort, jsonify
from flask_socketio import SocketIO
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
# threshold in seconds after which a camera is considered stale and removed
STALE_THRESHOLD = int(os.environ.get('CCTV_STALE_THRESHOLD', '5'))

# Socket.IO for WebRTC signalling
socketio = SocketIO(app, cors_allowed_origins='*')

# publisher registry: camera_id -> publisher sid
PUBLISHERS = {}
# reverse map: sid -> camera_id
SID_TO_CAMERA = {}

# Simple auth decorator
# For now authentication is disabled — allow all requests through.
def login_required(f):
    return f


@app.route('/CCTV/')
def index():
    """Landing page: lets the user choose Camera (act as feeder) or Viewer (see cameras).
    Viewer will be directed to login if not authenticated.
    """
    return render_template('index.html')


@app.route('/CCTV/login', methods=['GET', 'POST'])
def login():
    """Authentication is disabled in this deployment mode — mark session as logged in and redirect.

    This keeps existing flow but does not require a password.
    """
    session['logged_in'] = True
    next_url = request.args.get('next') or request.form.get('next') or url_for('index')
    return redirect(next_url)


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
    # log a short message so container logs show feeder activity
    try:
        print(f"[push_frame] camera={camera_id} size={len(data)} ts={LAST_FEEDS[camera_id]}")
    except Exception:
        pass

    return ('', 204)


@app.route('/CCTV/feed_status')
def feed_status():
    """Return JSON status about all feeders/cameras.

    Returns a dict camera_id -> { age, alive }
    """
    now = time.time()
    out = {}
    with FRAME_LOCK:
        # Remove stale entries immediately so viewer lists stay clean
        to_delete = [cam_id for cam_id, ts in LAST_FEEDS.items() if now - ts > STALE_THRESHOLD]
        for cam_id in to_delete:
            LAST_FEEDS.pop(cam_id, None)
            LATEST_FRAMES.pop(cam_id, None)
        for cam_id, ts in LAST_FEEDS.items():
            age = now - ts
            out[cam_id] = {
                'age': age,
                'alive': age <= STALE_THRESHOLD
            }
    return jsonify(out)


# ----------------------
# Socket.IO signalling
# ----------------------

@socketio.on('register-publisher')
def handle_register_publisher(data):
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    PUBLISHERS[camera_id] = request.sid
    SID_TO_CAMERA[request.sid] = camera_id
    # mark the publisher as recently active so /CCTV/feed_status shows it as live
    with FRAME_LOCK:
        LAST_FEEDS[camera_id] = time.time()
    print(f"[socket] publisher registered camera={camera_id} sid={request.sid}")



@socketio.on('publisher-heartbeat')
def handle_publisher_heartbeat(data):
    """Heartbeat from browser-based publisher to indicate it's still online.

    The client should emit this periodically so the server's /CCTV/feed_status
    remains accurate for WebRTC publishers (which don't POST frames).
    """
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    with FRAME_LOCK:
        LAST_FEEDS[camera_id] = time.time()


@socketio.on('viewer-offer')
def handle_viewer_offer(data):
    camera_id = data.get('camera_id')
    sdp = data.get('sdp')
    print(f"[socket] viewer-offer for camera={camera_id} from sid={request.sid}")
    publisher_sid = PUBLISHERS.get(camera_id)
    if not publisher_sid:
        socketio.emit('error', {'msg': 'no publisher for camera'}, to=request.sid)
        return
    # forward viewer offer to publisher
    socketio.emit('viewer-offer', {'viewer_sid': request.sid, 'sdp': sdp}, to=publisher_sid)
    print(f"[socket] forwarded viewer-offer for camera={camera_id} to publisher_sid={publisher_sid}")


@socketio.on('publisher-answer')
def handle_publisher_answer(data):
    viewer_sid = data.get('viewer_sid')
    sdp = data.get('sdp')
    print(f"[socket] publisher-answer for viewer_sid={viewer_sid} from sid={request.sid}")
    if not viewer_sid or not sdp:
        return
    socketio.emit('publisher-answer', {'sdp': sdp}, to=viewer_sid)


@socketio.on('ice-candidate')
def handle_ice(data):
    # if 'to' provided, forward to that sid; otherwise route from viewer -> publisher by camera_id
    to = data.get('to')
    candidate = data.get('candidate')
    camera_id = data.get('camera_id')
    if to:
        socketio.emit('ice-candidate', {'candidate': candidate, 'from': request.sid}, to=to)
        print(f"[socket] forwarded ICE to sid={to} from sid={request.sid}")
    else:
        # viewer -> publisher
        publisher_sid = PUBLISHERS.get(camera_id)
        if publisher_sid:
            socketio.emit('ice-candidate', {'candidate': candidate, 'from': request.sid}, to=publisher_sid)
            print(f"[socket] forwarded ICE for camera={camera_id} to publisher_sid={publisher_sid} from sid={request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    cam = SID_TO_CAMERA.pop(sid, None)
    if cam:
        PUBLISHERS.pop(cam, None)
        print(f"[socket] publisher disconnected camera={cam} sid={sid}")


# Simple health endpoint
@app.route('/CCTV/health')
def health():
    return 'OK', 200


if __name__ == '__main__':
    # for local dev only
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True)
