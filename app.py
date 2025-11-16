import os
import time
import threading
import uuid
from collections import defaultdict

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__, static_url_path='/static', static_folder='static')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.secret_key = os.urandom(24)

socketio = SocketIO(
    app,
    cors_allowed_origins='*',
    async_mode='threading',
    path='/socket.io'
)

# Track active broadcasters: camera_id -> {sid, name, timestamp}
BROADCASTERS = {}
BROADCASTERS_LOCK = threading.Lock()

# Track active viewers per camera
VIEWERS = defaultdict(set)
VIEWERS_LOCK = threading.Lock()

# Map socket SID to broadcaster/viewer metadata
SESSION_ROLES = {}
SESSION_ROLES_LOCK = threading.Lock()

# User presence for calling feature
USERS = {}
USERS_LOCK = threading.Lock()
SID_TO_USER = {}

# Active calls and pending invitations
CALLS = {}
CALL_LOCK = threading.Lock()
PENDING_CALLS = {}
USER_ACTIVE_CALL = {}

HEARTBEAT_TIMEOUT = int(os.getenv('BROADCASTER_HEARTBEAT_TIMEOUT', '300'))

# Cleanup stale broadcasters (no heartbeat for 30 seconds)
def cleanup_stale():
    while True:
        time.sleep(5)
        now = time.time()
        stale = []
        with BROADCASTERS_LOCK:
            stale = [cid for cid, data in BROADCASTERS.items() if now - data['timestamp'] > HEARTBEAT_TIMEOUT]
            for cid in stale:
                del BROADCASTERS[cid]
        # Emit to all clients about broadcaster leaving (requires app context)
        if stale:
            with app.app_context():
                for cid in stale:
                    socketio.emit('broadcaster_left', {'camera_id': cid})

cleanup_thread = threading.Thread(target=cleanup_stale, daemon=True)
cleanup_thread.start()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def on_connect():
    with BROADCASTERS_LOCK:
        emit('broadcasters_list', {'broadcasters': list(BROADCASTERS.keys())})
    _emit_viewer_counts(target_sid=request.sid)
    _broadcast_online_users(target_sid=request.sid)

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
    with SESSION_ROLES_LOCK:
        SESSION_ROLES[request.sid] = {'type': 'broadcaster', 'camera_id': camera_id}
    socketio.emit('broadcasters_list', {'broadcasters': broadcasters_list})

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
    if not camera_id or not sdp:
        return
    with BROADCASTERS_LOCK:
        broadcaster = BROADCASTERS.get(camera_id)
        if broadcaster:
            broadcaster_sid = broadcaster['sid']
            socketio.emit('viewer_offer', {
                'camera_id': camera_id,
                'viewer_sid': request.sid,
                'sdp': sdp
            }, to=broadcaster_sid)


@socketio.on('viewer_join')
def on_viewer_join(data):
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    with VIEWERS_LOCK:
        VIEWERS[camera_id].add(request.sid)
        count = len(VIEWERS[camera_id])
    with SESSION_ROLES_LOCK:
        SESSION_ROLES[request.sid] = {'type': 'viewer', 'camera_id': camera_id}
    socketio.emit('viewer_count', {'camera_id': camera_id, 'count': count})


@socketio.on('viewer_leave')
def on_viewer_leave(data):
    camera_id = data.get('camera_id')
    _remove_viewer(request.sid, camera_id)


@socketio.on('request_viewer_counts')
def on_request_viewer_counts():
    _emit_viewer_counts(target_sid=request.sid)


@socketio.on('request_online_users')
def on_request_online_users():
    _broadcast_online_users(target_sid=request.sid)

@socketio.on('broadcaster_answer')
def on_broadcaster_answer(data):
    viewer_sid = data.get('viewer_sid')
    sdp = data.get('sdp')
    if viewer_sid and sdp:
        socketio.emit('broadcaster_answer', {'sdp': sdp}, to=viewer_sid)

@socketio.on('ice_candidate')
def on_ice_candidate(data):
    target_sid = data.get('target_sid')
    candidate = data.get('candidate')
    if target_sid and candidate:
        socketio.emit('ice_candidate', {
            'candidate': candidate,
            'from_sid': request.sid
        }, to=target_sid)


@socketio.on('stop_broadcast')
def on_stop_broadcast(data):
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    with BROADCASTERS_LOCK:
        if camera_id in BROADCASTERS and BROADCASTERS[camera_id]['sid'] == request.sid:
            del BROADCASTERS[camera_id]
            socketio.emit('broadcasters_list', {'broadcasters': list(BROADCASTERS.keys())})

@socketio.on('disconnect')
def on_disconnect():
    _handle_disconnect(request.sid)


def _remove_viewer(sid, camera_id=None):
    with VIEWERS_LOCK:
        if camera_id:
            viewers = VIEWERS.get(camera_id)
            if viewers and sid in viewers:
                viewers.remove(sid)
                socketio.emit('viewer_count', {'camera_id': camera_id, 'count': len(viewers)})
                if not viewers:
                    VIEWERS.pop(camera_id, None)
            return

        for cid, viewers in list(VIEWERS.items()):
            if sid in viewers:
                viewers.remove(sid)
                socketio.emit('viewer_count', {'camera_id': cid, 'count': len(viewers)})
                if not viewers:
                    VIEWERS.pop(cid, None)


def _handle_disconnect(sid):
    camera_id = None
    with BROADCASTERS_LOCK:
        for cid, data in list(BROADCASTERS.items()):
            if data['sid'] == sid:
                camera_id = cid
                del BROADCASTERS[cid]
                break
        if camera_id:
            socketio.emit('broadcasters_list', {'broadcasters': list(BROADCASTERS.keys())})

    _remove_viewer(sid)

    with SESSION_ROLES_LOCK:
        role = SESSION_ROLES.pop(sid, None)

    if role and role.get('type') == 'viewer':
        # Already handled in _remove_viewer
        pass

    username = SID_TO_USER.pop(sid, None)
    if username:
        _leave_call(username)
        with USERS_LOCK:
            USERS.pop(username, None)
        _broadcast_online_users()


# ------------- Calling feature helpers -------------

def _broadcast_online_users(target_sid=None):
    with USERS_LOCK:
        payload = []
        for name, data in USERS.items():
            payload.append({
                'name': name,
                'auto_pickup': data.get('auto_pickup', False),
                'in_call': USER_ACTIVE_CALL.get(name) is not None
            })
    if target_sid:
        socketio.emit('online_users', {'users': payload}, to=target_sid)
    else:
        socketio.emit('online_users', {'users': payload})


def _emit_viewer_counts(target_sid=None):
    with VIEWERS_LOCK:
        snapshot = {cid: len(viewers) for cid, viewers in VIEWERS.items()}
    if target_sid:
        socketio.emit('viewer_counts', {'counts': snapshot}, to=target_sid)
    else:
        socketio.emit('viewer_counts', {'counts': snapshot})


def _join_call(call_id, username, initiator=None):
    with CALL_LOCK:
        call = CALLS.setdefault(call_id, {'participants': set()})
        if username in call['participants']:
            return
        call['participants'].add(username)
        USER_ACTIVE_CALL[username] = call_id

    user_sid = USERS.get(username, {}).get('sid')
    if not user_sid:
        return

    payload = {
        'call_id': call_id,
        'participants': list(call['participants']),
        'initiator': initiator or username
    }
    socketio.emit('call_joined', payload, to=user_sid)

    for participant in payload['participants']:
        if participant == username:
            continue
        peer_sid = USERS.get(participant, {}).get('sid')
        if peer_sid:
            socketio.emit('call_participant_joined', {
                'call_id': call_id,
                'user': username,
                'participants': payload['participants']
            }, to=peer_sid)


def _leave_call(username):
    call_id = USER_ACTIVE_CALL.pop(username, None)
    if not call_id:
        return

    with CALL_LOCK:
        call = CALLS.get(call_id)
        if not call:
            return
        call['participants'].discard(username)
        remaining = list(call['participants'])
        if not remaining:
            CALLS.pop(call_id, None)

    sid = USERS.get(username, {}).get('sid')
    if sid:
        socketio.emit('call_ended', {'call_id': call_id}, to=sid)

    for participant in remaining:
        peer_sid = USERS.get(participant, {}).get('sid')
        if peer_sid:
            socketio.emit('call_participant_left', {
                'call_id': call_id,
                'user': username,
                'participants': remaining
            }, to=peer_sid)


def _validate_call_members(call_id, sender, target):
    with CALL_LOCK:
        call = CALLS.get(call_id)
        if not call:
            return False
        return sender in call['participants'] and target in call['participants']


def _finalize_call(call_id, caller, target):
    PENDING_CALLS.pop(call_id, None)
    _join_call(call_id, caller, initiator=caller)
    _join_call(call_id, target, initiator=caller)
    _broadcast_online_users()


def _add_to_existing_call(call_id, username):
    _join_call(call_id, username)
    _broadcast_online_users()


@socketio.on('register_user')
def register_user(data):
    desired = (data or {}).get('name')
    auto_pickup = bool((data or {}).get('auto_pickup', True))
    base_name = (desired or '').strip()
    if not base_name:
        base_name = f'user-{str(uuid.uuid4())[:5]}'
    safe = ''.join(ch for ch in base_name if ch.isalnum() or ch in ('-', '_')) or 'guest'

    with USERS_LOCK:
        username = safe
        suffix = 1
        while username in USERS:
            username = f"{safe}-{suffix}"
            suffix += 1

        # Remove previous username for this SID
        existing = SID_TO_USER.get(request.sid)
        if existing and existing in USERS:
            USERS.pop(existing, None)

        USERS[username] = {
            'sid': request.sid,
            'auto_pickup': auto_pickup
        }
        SID_TO_USER[request.sid] = username

    emit('user_registered', {'name': username, 'auto_pickup': auto_pickup})
    _broadcast_online_users()


@socketio.on('update_auto_pickup')
def update_auto_pickup(data):
    username = SID_TO_USER.get(request.sid)
    if not username:
        return
    enabled = bool(data.get('enabled'))
    with USERS_LOCK:
        if username in USERS:
            USERS[username]['auto_pickup'] = enabled
    _broadcast_online_users()


@socketio.on('call_user')
def call_user(data):
    caller = SID_TO_USER.get(request.sid)
    target = (data or {}).get('target')
    if not caller:
        emit('call_error', {'message': 'Please set your profile before calling.'})
        return
    if not target or target == caller:
        emit('call_error', {'message': 'Select a valid person to call.'})
        return

    with USERS_LOCK:
        target_info = USERS.get(target)
        caller_info = USERS.get(caller)

    if not target_info:
        emit('call_error', {'message': 'User is offline.'})
        return

    caller_call = USER_ACTIVE_CALL.get(caller)
    target_call = USER_ACTIVE_CALL.get(target)

    if caller_call and target_call and caller_call != target_call:
        emit('call_error', {'message': 'You are already in another call.'})
        return

    if target_call:
        _add_to_existing_call(target_call, caller)
        return

    call_id = str(uuid.uuid4())
    PENDING_CALLS[call_id] = {'caller': caller, 'target': target}

    if target_info.get('auto_pickup'):
        _finalize_call(call_id, caller, target)
    else:
        socketio.emit('incoming_call', {
            'call_id': call_id,
            'from': caller
        }, to=target_info['sid'])
        emit('call_pending', {'call_id': call_id, 'target': target})


@socketio.on('respond_call')
def respond_call(data):
    call_id = data.get('call_id')
    accept = bool(data.get('accept'))
    info = PENDING_CALLS.get(call_id)
    responder = SID_TO_USER.get(request.sid)
    if not info or not responder:
        return

    caller = info['caller']
    target = info['target']

    if not accept:
        socketio.emit('call_declined', {
            'call_id': call_id,
            'target': target
        }, to=USERS.get(caller, {}).get('sid'))
        PENDING_CALLS.pop(call_id, None)
        return

    _finalize_call(call_id, caller, target)


@socketio.on('leave_call')
def leave_call():
    username = SID_TO_USER.get(request.sid)
    if not username:
        return
    _leave_call(username)
    _broadcast_online_users()


@socketio.on('call_webrtc_offer')
def call_webrtc_offer(data):
    call_id = data.get('call_id')
    target = data.get('target')
    sdp = data.get('sdp')
    sender = SID_TO_USER.get(request.sid)
    if not (call_id and target and sdp and sender):
        return
    if not _validate_call_members(call_id, sender, target):
        return
    target_sid = USERS.get(target, {}).get('sid')
    if target_sid:
        socketio.emit('call_webrtc_offer', {
            'call_id': call_id,
            'from': sender,
            'sdp': sdp
        }, to=target_sid)


@socketio.on('call_webrtc_answer')
def call_webrtc_answer(data):
    call_id = data.get('call_id')
    target = data.get('target')
    sdp = data.get('sdp')
    sender = SID_TO_USER.get(request.sid)
    if not (call_id and target and sdp and sender):
        return
    if not _validate_call_members(call_id, sender, target):
        return
    target_sid = USERS.get(target, {}).get('sid')
    if target_sid:
        socketio.emit('call_webrtc_answer', {
            'call_id': call_id,
            'from': sender,
            'sdp': sdp
        }, to=target_sid)


@socketio.on('call_webrtc_ice')
def call_webrtc_ice(data):
    call_id = data.get('call_id')
    target = data.get('target')
    candidate = data.get('candidate')
    sender = SID_TO_USER.get(request.sid)
    if not (call_id and target and candidate and sender):
        return
    if not _validate_call_members(call_id, sender, target):
        return
    target_sid = USERS.get(target, {}).get('sid')
    if target_sid:
        socketio.emit('call_webrtc_ice', {
            'call_id': call_id,
            'from': sender,
            'candidate': candidate
        }, to=target_sid)

if __name__ == '__main__':
    print('[CCTV] WebRTC CCTV System starting...')
    print('[CCTV] Serving app at root path /')
    socketio.run(app, host='0.0.0.0', port=8001, debug=False, allow_unsafe_werkzeug=True)
