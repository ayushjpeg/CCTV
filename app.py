from cctv_app import create_app
from cctv_app.extensions import socketio

app = create_app()


if __name__ == '__main__':
    print('[CCTV] WebRTC CCTV System starting...')
    print('[CCTV] Serving app at root path /')
    socketio.run(app, host='0.0.0.0', port=8001, debug=False, allow_unsafe_werkzeug=True)
