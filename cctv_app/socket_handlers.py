import threading
import time
import uuid

from flask import request

from .config import BROADCASTER_HEARTBEAT_TIMEOUT
from .extensions import socketio
from . import state

_app_ref = None
_cleanup_started = False


def init_app(app):
    global _app_ref, _cleanup_started
    _app_ref = app
    if not _cleanup_started:
        _cleanup_started = True
        cleanup_thread = threading.Thread(target=_cleanup_stale_broadcasters, daemon=True)
        cleanup_thread.start()


def _cleanup_stale_broadcasters():
    while True:
        time.sleep(5)
        now = time.time()
        stale = []
        with state.BROADCASTERS_LOCK:
            stale = [cid for cid, data in state.BROADCASTERS.items() if now - data['timestamp'] > BROADCASTER_HEARTBEAT_TIMEOUT]
            for cid in stale:
                del state.BROADCASTERS[cid]
        if stale and _app_ref:
            with _app_ref.app_context():
                for cid in stale:
                    socketio.emit('broadcaster_left', {'camera_id': cid})


def _emit_viewer_counts(target_sid=None):
    with state.VIEWERS_LOCK:
        snapshot = {cid: len(viewers) for cid, viewers in state.VIEWERS.items()}
    if target_sid:
        socketio.emit('viewer_counts', {'counts': snapshot}, to=target_sid)
    else:
        socketio.emit('viewer_counts', {'counts': snapshot})


def _broadcast_online_users(target_sid=None):
    with state.USERS_LOCK:
        payload = []
        for name, data in state.USERS.items():
            payload.append({
                'name': name,
                'auto_pickup': data.get('auto_pickup', False),
                'in_call': state.USER_ACTIVE_CALL.get(name) is not None
            })
    if target_sid:
        socketio.emit('online_users', {'users': payload}, to=target_sid)
    else:
        socketio.emit('online_users', {'users': payload})


def _remove_viewer(sid, camera_id=None):
    with state.VIEWERS_LOCK:
        if camera_id:
            viewers = state.VIEWERS.get(camera_id)
            if viewers and sid in viewers:
                viewers.remove(sid)
                socketio.emit('viewer_count', {'camera_id': camera_id, 'count': len(viewers)})
                if not viewers:
                    state.VIEWERS.pop(camera_id, None)
            return

        for cid, viewers in list(state.VIEWERS.items()):
            if sid in viewers:
                viewers.remove(sid)
                socketio.emit('viewer_count', {'camera_id': cid, 'count': len(viewers)})
                if not viewers:
                    state.VIEWERS.pop(cid, None)


def _handle_disconnect(sid):
    camera_id = None
    with state.BROADCASTERS_LOCK:
        for cid, data in list(state.BROADCASTERS.items()):
            if data['sid'] == sid:
                camera_id = cid
                del state.BROADCASTERS[cid]
                break
        if camera_id:
            socketio.emit('broadcasters_list', {'broadcasters': list(state.BROADCASTERS.keys())})

    _remove_viewer(sid)

    with state.SESSION_ROLES_LOCK:
        role = state.SESSION_ROLES.pop(sid, None)

    if role and role.get('type') == 'viewer':
        pass

    username = state.SID_TO_USER.pop(sid, None)
    if username:
        _leave_call(username)
        with state.USERS_LOCK:
            state.USERS.pop(username, None)
        _broadcast_online_users()


def _join_call(call_id, username, initiator=None):
    with state.CALL_LOCK:
        call = state.CALLS.setdefault(call_id, {'participants': set()})
        if username in call['participants']:
            return
        call['participants'].add(username)
        state.USER_ACTIVE_CALL[username] = call_id

    user_sid = state.USERS.get(username, {}).get('sid')
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
        peer_sid = state.USERS.get(participant, {}).get('sid')
        if peer_sid:
            socketio.emit('call_participant_joined', {
                'call_id': call_id,
                'user': username,
                'participants': payload['participants']
            }, to=peer_sid)


def _leave_call(username):
    call_id = state.USER_ACTIVE_CALL.pop(username, None)
    if not call_id:
        return

    with state.CALL_LOCK:
        call = state.CALLS.get(call_id)
        if not call:
            return
        call['participants'].discard(username)
        remaining = list(call['participants'])
        if not remaining:
            state.CALLS.pop(call_id, None)

    sid = state.USERS.get(username, {}).get('sid')
    if sid:
        socketio.emit('call_ended', {'call_id': call_id}, to=sid)

    for participant in remaining:
        peer_sid = state.USERS.get(participant, {}).get('sid')
        if peer_sid:
            socketio.emit('call_participant_left', {
                'call_id': call_id,
                'user': username,
                'participants': remaining
            }, to=peer_sid)


def _validate_call_members(call_id, sender, target):
    with state.CALL_LOCK:
        call = state.CALLS.get(call_id)
        if not call:
            return False
        return sender in call['participants'] and target in call['participants']


def _finalize_call(call_id, caller, target):
    state.PENDING_CALLS.pop(call_id, None)
    _join_call(call_id, caller, initiator=caller)
    _join_call(call_id, target, initiator=caller)
    _broadcast_online_users()


def _add_to_existing_call(call_id, username):
    _join_call(call_id, username)
    _broadcast_online_users()


@socketio.on('connect')
def on_connect():
    with state.BROADCASTERS_LOCK:
        socketio.emit('broadcasters_list', {'broadcasters': list(state.BROADCASTERS.keys())}, to=request.sid)
    _emit_viewer_counts(target_sid=request.sid)
    _broadcast_online_users(target_sid=request.sid)


@socketio.on('register_broadcaster')
def on_register_broadcaster(data):
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    with state.BROADCASTERS_LOCK:
        state.BROADCASTERS[camera_id] = {
            'sid': request.sid,
            'name': data.get('name', camera_id),
            'timestamp': time.time()
        }
        broadcasters_list = list(state.BROADCASTERS.keys())
    with state.SESSION_ROLES_LOCK:
        state.SESSION_ROLES[request.sid] = {'type': 'broadcaster', 'camera_id': camera_id}
    socketio.emit('broadcasters_list', {'broadcasters': broadcasters_list})


@socketio.on('broadcaster_heartbeat')
def on_heartbeat(data):
    camera_id = data.get('camera_id')
    if camera_id and camera_id in state.BROADCASTERS:
        with state.BROADCASTERS_LOCK:
            state.BROADCASTERS[camera_id]['timestamp'] = time.time()


@socketio.on('viewer_offer')
def on_viewer_offer(data):
    camera_id = data.get('camera_id')
    sdp = data.get('sdp')
    if not camera_id or not sdp:
        return
    with state.BROADCASTERS_LOCK:
        broadcaster = state.BROADCASTERS.get(camera_id)
        if broadcaster:
            socketio.emit('viewer_offer', {
                'camera_id': camera_id,
                'viewer_sid': request.sid,
                'sdp': sdp
            }, to=broadcaster['sid'])


@socketio.on('viewer_join')
def on_viewer_join(data):
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    with state.VIEWERS_LOCK:
        state.VIEWERS[camera_id].add(request.sid)
        count = len(state.VIEWERS[camera_id])
    with state.SESSION_ROLES_LOCK:
        state.SESSION_ROLES[request.sid] = {'type': 'viewer', 'camera_id': camera_id}
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
    with state.BROADCASTERS_LOCK:
        if camera_id in state.BROADCASTERS and state.BROADCASTERS[camera_id]['sid'] == request.sid:
            del state.BROADCASTERS[camera_id]
            socketio.emit('broadcasters_list', {'broadcasters': list(state.BROADCASTERS.keys())})


@socketio.on('disconnect')
def on_disconnect():
    _handle_disconnect(request.sid)


@socketio.on('register_user')
def register_user(data):
    desired = (data or {}).get('name')
    auto_pickup = bool((data or {}).get('auto_pickup', True))
    base_name = (desired or '').strip()
    if not base_name:
        base_name = f"user-{str(uuid.uuid4())[:5]}"
    safe = ''.join(ch for ch in base_name if ch.isalnum() or ch in ('-', '_')) or 'guest'

    with state.USERS_LOCK:
        username = safe
        suffix = 1
        while username in state.USERS:
            username = f"{safe}-{suffix}"
            suffix += 1

        existing = state.SID_TO_USER.get(request.sid)
        if existing and existing in state.USERS:
            state.USERS.pop(existing, None)

        state.USERS[username] = {
            'sid': request.sid,
            'auto_pickup': auto_pickup
        }
        state.SID_TO_USER[request.sid] = username

    socketio.emit('user_registered', {'name': username, 'auto_pickup': auto_pickup}, to=request.sid)
    _broadcast_online_users()


@socketio.on('update_auto_pickup')
def update_auto_pickup(data):
    username = state.SID_TO_USER.get(request.sid)
    if not username:
        return
    enabled = bool(data.get('enabled'))
    with state.USERS_LOCK:
        if username in state.USERS:
            state.USERS[username]['auto_pickup'] = enabled
    _broadcast_online_users()


@socketio.on('call_user')
def call_user(data):
    caller = state.SID_TO_USER.get(request.sid)
    target = (data or {}).get('target')
    if not caller:
        socketio.emit('call_error', {'message': 'Please set your profile before calling.'}, to=request.sid)
        return
    if not target or target == caller:
        socketio.emit('call_error', {'message': 'Select a valid person to call.'}, to=request.sid)
        return

    with state.USERS_LOCK:
        target_info = state.USERS.get(target)
        caller_info = state.USERS.get(caller)

    if not target_info:
        socketio.emit('call_error', {'message': 'User is offline.'}, to=request.sid)
        return

    caller_call = state.USER_ACTIVE_CALL.get(caller)
    target_call = state.USER_ACTIVE_CALL.get(target)

    if caller_call and target_call and caller_call != target_call:
        socketio.emit('call_error', {'message': 'You are already in another call.'}, to=request.sid)
        return

    if target_call:
        _add_to_existing_call(target_call, caller)
        return

    call_id = str(uuid.uuid4())
    state.PENDING_CALLS[call_id] = {'caller': caller, 'target': target}

    if target_info.get('auto_pickup'):
        _finalize_call(call_id, caller, target)
    else:
        socketio.emit('incoming_call', {
            'call_id': call_id,
            'from': caller
        }, to=target_info['sid'])
        socketio.emit('call_pending', {'call_id': call_id, 'target': target}, to=request.sid)


@socketio.on('respond_call')
def respond_call(data):
    call_id = data.get('call_id')
    accept = bool(data.get('accept'))
    info = state.PENDING_CALLS.get(call_id)
    responder = state.SID_TO_USER.get(request.sid)
    if not info or not responder:
        return

    caller = info['caller']
    target = info['target']

    if not accept:
        caller_sid = state.USERS.get(caller, {}).get('sid')
        if caller_sid:
            socketio.emit('call_declined', {
                'call_id': call_id,
                'target': target
            }, to=caller_sid)
        state.PENDING_CALLS.pop(call_id, None)
        return

    _finalize_call(call_id, caller, target)


@socketio.on('leave_call')
def leave_call():
    username = state.SID_TO_USER.get(request.sid)
    if not username:
        return
    _leave_call(username)
    _broadcast_online_users()


@socketio.on('call_webrtc_offer')
def call_webrtc_offer(data):
    call_id = data.get('call_id')
    target = data.get('target')
    sdp = data.get('sdp')
    sender = state.SID_TO_USER.get(request.sid)
    if not (call_id and target and sdp and sender):
        return
    if not _validate_call_members(call_id, sender, target):
        return
    target_sid = state.USERS.get(target, {}).get('sid')
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
    sender = state.SID_TO_USER.get(request.sid)
    if not (call_id and target and sdp and sender):
        return
    if not _validate_call_members(call_id, sender, target):
        return
    target_sid = state.USERS.get(target, {}).get('sid')
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
    sender = state.SID_TO_USER.get(request.sid)
    if not (call_id and target and candidate and sender):
        return
    if not _validate_call_members(call_id, sender, target):
        return
    target_sid = state.USERS.get(target, {}).get('sid')
    if target_sid:
        socketio.emit('call_webrtc_ice', {
            'call_id': call_id,
            'from': sender,
            'candidate': candidate
        }, to=target_sid)
