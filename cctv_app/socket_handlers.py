import threading
import time

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
        state.SESSION_ROLES.pop(sid, None)


@socketio.on('connect')
def on_connect():
    with state.BROADCASTERS_LOCK:
        socketio.emit('broadcasters_list', {'broadcasters': list(state.BROADCASTERS.keys())}, to=request.sid)
    _emit_viewer_counts(target_sid=request.sid)


@socketio.on('register_broadcaster')
def on_register_broadcaster(data):
    camera_id = data.get('camera_id')
    if not camera_id:
        return
    with state.BROADCASTERS_LOCK:
        is_new = camera_id not in state.BROADCASTERS
        state.BROADCASTERS[camera_id] = {
            'sid': request.sid,
            'name': data.get('name', camera_id),
            'timestamp': time.time()
        }
        broadcasters_list = list(state.BROADCASTERS.keys())
    with state.SESSION_ROLES_LOCK:
        state.SESSION_ROLES[request.sid] = {'type': 'broadcaster', 'camera_id': camera_id}
    socketio.emit('broadcasters_list', {'broadcasters': broadcasters_list})
    if is_new:
        socketio.emit(
            'camera_live',
            {'camera_id': camera_id, 'name': data.get('name', camera_id)},
            include_self=False
        )


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


