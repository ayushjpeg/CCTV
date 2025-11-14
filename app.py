import os
import time
import threading
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify

# Simple MJPEG push/poll CCTV app (no Socket.IO / WebRTC)
# - Feeders POST JPEG frames to /CCTV/push_frame with header X-Cam-ID
# - Server keeps latest frame per camera in memory
# - Viewers open /CCTV/video_feed?camera=<id> to receive an MJPEG stream

app = Flask(__name__, static_url_path='/CCTV/static', static_folder='static')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.secret_key = os.environ.get('CCTV_SECRET_KEY', os.urandom(24))

LATEST_FRAMES = {}  # camera_id -> bytes
LAST_FEEDS = {}     # camera_id -> timestamp
FRAME_LOCK = threading.Lock()
STALE_THRESHOLD = int(os.environ.get('CCTV_STALE_THRESHOLD', '5'))

# Routes
@app.route('/CCTV/')
def index():
    return render_template('index.html', STALE_THRESHOLD=STALE_THRESHOLD)


@app.route('/CCTV/stream')
def stream_page():
    return render_template('stream.html', STALE_THRESHOLD=STALE_THRESHOLD)


@app.route('/CCTV/push_frame', methods=['POST'])
def push_frame():
    """Accept a JPEG frame POST from a feeder.

    Headers:
      X-Cam-ID: identifier for the camera
    Body:
      raw JPEG bytes (content-type image/jpeg)
    """
    cam = request.headers.get('X-Cam-ID') or request.args.get('camera') or request.form.get('camera')
    if not cam:
        return ('Missing X-Cam-ID header or camera param', 400)
    data = request.get_data()
    if not data:
        return ('Empty frame', 400)
    with FRAME_LOCK:
        LATEST_FRAMES[cam] = data
        LAST_FEEDS[cam] = time.time()
    return ('', 204)


def gen(camera_id):
    boundary = b'--frame\r\n'
    while True:
        frame = None
        with FRAME_LOCK:
            frame = LATEST_FRAMES.get(camera_id)
        if frame:
            yield boundary
            yield b'Content-Type: image/jpeg\r\n'
            yield b'Content-Length: ' + str(len(frame)).encode() + b'\r\n\r\n'
            yield frame
            yield b'\r\n'
        else:
            # no frame yet - send a small pause
            time.sleep(0.2)
            continue
        # throttle to ~30fps max if frames are updated faster
        time.sleep(1 / 30.0)


@app.route('/CCTV/video_feed')
def video_feed():
    camera = request.args.get('camera')
    if not camera:
        return ('camera query param required', 400)
    return Response(gen(camera), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/CCTV/feed_status')
def feed_status():
    """Return current feeds and ages in seconds."""
    now = time.time()
    with FRAME_LOCK:
        feeds = []
        for cam, ts in LAST_FEEDS.items():
            feeds.append({'camera': cam, 'age': now - ts, 'has_frame': cam in LATEST_FRAMES})
    return jsonify({'feeds': feeds})


@app.route('/CCTV/debug_feeds')
def debug_feeds():
    with FRAME_LOCK:
        return jsonify({ 'last_feeds': {k: LAST_FEEDS.get(k) for k in LAST_FEEDS} })


@app.route('/CCTV/clear_state', methods=['POST'])
def http_clear_state():
    remote = request.remote_addr
    if remote not in ('127.0.0.1', '::1', 'localhost'):
        return ('Forbidden', 403)
    with FRAME_LOCK:
        LATEST_FRAMES.clear()
        LAST_FEEDS.clear()
    return ('', 204)


def _janitor_loop():
    print('[server] janitor thread started')
    while True:
        try:
            now = time.time()
            removed = False
            with FRAME_LOCK:
                to_delete = [cam for cam, ts in LAST_FEEDS.items() if now - ts > STALE_THRESHOLD]
                for cam in to_delete:
                    LAST_FEEDS.pop(cam, None)
                    LATEST_FRAMES.pop(cam, None)
                    removed = True
            if removed:
                print('[server] janitor removed stale feeds:', to_delete)
        except Exception as e:
            print('[server] janitor error', e)
        time.sleep(1.0)


if __name__ == '__main__':
    # clean start
    with FRAME_LOCK:
        LATEST_FRAMES.clear()
        LAST_FEEDS.clear()
    # start janitor
    t = threading.Thread(target=_janitor_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True)
