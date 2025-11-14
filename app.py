import os
import time
import threading
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from collections import defaultdict

app = Flask(__name__, static_url_path='/CCTV/static', static_folder='static')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.secret_key = os.urandom(24)

socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# Track active broadcasters: camera_id -> {sid, name, timestamp}
BROADCASTERS = {}
BROADCASTERS_LOCK = threading.Lock()

# Cleanup stale broadcasters (no heartbeat for 30 seconds)
def cleanup_stale():
    while True:
        time.sleep(5)
        now = time.time()
        stale = []
        with BROADCASTERS_LOCK:
            stale = [cid for cid, data in BROADCASTERS.items() if now - data['timestamp'] > 30]
            for cid in stale:
                del BROADCASTERS[cid]
        # Emit to all clients about broadcaster leaving (requires app context)
        if stale:
            with app.app_context():
                for cid in stale:
                    socketio.emit('broadcaster_left', {'camera_id': cid}, broadcast=True)

cleanup_thread = threading.Thread(target=cleanup_stale, daemon=True)
cleanup_thread.start()

@app.route('/CCTV/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def on_connect():
    print(f'[socket] Client connected: {request.sid}')
    with BROADCASTERS_LOCK:
        emit('broadcasters_list', {'broadcasters': list(BROADCASTERS.keys())})

@socketio.on('register_broadcaster')
def on_register_broadcaster(data):
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    with BROADCASTERS_LOCK:
        BROADCASTERS[camera_id] = {
            'sid': request.sid,
            'name': data.get('name', camera_id),
            'timestamp': time.time()
        }
        broadcasters_list = list(BROADCASTERS.keys())
    print(f'[socket] Broadcaster registered: {camera_id}')
    emit('broadcasters_list', {'broadcasters': broadcasters_list}, broadcast=True)

@socketio.on('broadcaster_heartbeat')
def on_heartbeat(data):
    camera_id = data.get('camera_id')
    if camera_id and camera_id in BROADCASTERS:
        with BROADCASTERS_LOCK:
            BROADCASTERS[camera_id]['timestamp'] = time.time()

@socketio.on('viewer_offer')
def on_viewer_offer(data):
    camera_id = data.get('camera_id')
    sdp = data.get('sdp')
    print(f'[socket] Received viewer_offer for camera: {camera_id}')
    if not camera_id or not sdp:
        print(f'[socket] Invalid viewer_offer: missing camera_id or sdp')
        return
    with BROADCASTERS_LOCK:
        broadcaster = BROADCASTERS.get(camera_id)
        if broadcaster:
            broadcaster_sid = broadcaster['sid']
            print(f'[socket] Forwarding viewer_offer to broadcaster {broadcaster_sid}')
            socketio.emit('viewer_offer', {
                'camera_id': camera_id,
                'viewer_sid': request.sid,
                'sdp': sdp
            }, to=broadcaster_sid)
        else:
            print(f'[socket] Broadcaster not found: {camera_id}')

@socketio.on('broadcaster_answer')
def on_broadcaster_answer(data):
    viewer_sid = data.get('viewer_sid')
    sdp = data.get('sdp')
    print(f'[socket] Received broadcaster_answer, forwarding to viewer {viewer_sid}')
    if viewer_sid and sdp:
        socketio.emit('broadcaster_answer', {'sdp': sdp}, to=viewer_sid)
    else:
        print(f'[socket] Invalid broadcaster_answer: missing viewer_sid or sdp')

@socketio.on('ice_candidate')
def on_ice_candidate(data):
    target_sid = data.get('target_sid')
    candidate = data.get('candidate')
    print(f'[socket] Received ICE candidate, forwarding to {target_sid}')
    if target_sid and candidate:
        socketio.emit('ice_candidate', {
            'candidate': candidate,
            'from_sid': request.sid
        }, to=target_sid)
    else:
        print(f'[socket] Invalid ICE candidate: missing target_sid or candidate')

@socketio.on('disconnect')
def on_disconnect():
    with BROADCASTERS_LOCK:
        camera_id = None
        for cid, data in BROADCASTERS.items():
            if data['sid'] == request.sid:
                camera_id = cid
                break
        if camera_id:
            del BROADCASTERS[camera_id]
            emit('broadcasters_list', {'broadcasters': list(BROADCASTERS.keys())}, broadcast=True)
            print(f'[socket] Broadcaster disconnected: {camera_id}')

if __name__ == '__main__':
    print('[CCTV] WebRTC CCTV System starting...')
    print('[CCTV] All endpoints under /CCTV/')
    socketio.run(app, host='0.0.0.0', port=8001, debug=False, allow_unsafe_werkzeug=True)
