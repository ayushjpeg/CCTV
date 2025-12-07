import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

bp = Blueprint('main', __name__)


@bp.route('/')
def index():
    return render_template('index.html')


@bp.route('/api/motion-clips', methods=['POST'])
def upload_motion_clip():
    clip = request.files.get('clip')
    if not clip:
        return jsonify({'success': False, 'error': 'Missing clip payload.'}), 400

    camera_id = secure_filename(request.form.get('camera_id', 'camera')) or 'camera'
    timestamp = request.form.get('timestamp') or str(int(time.time()))
    safe_name = secure_filename(f"{timestamp}.webm") or f"{int(time.time())}.webm"

    clip_dir = Path(current_app.config['MOTION_CLIP_DIR']) / camera_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    file_path = clip_dir / safe_name
    clip.save(file_path)
    file_size = file_path.stat().st_size if file_path.exists() else 0

    return jsonify({
        'success': True,
        'camera_id': camera_id,
        'clip_url': f"/motion-clips/{camera_id}/{safe_name}",
        'timestamp': timestamp,
        'bytes': file_size
    })


@bp.route('/motion-clips/<camera_id>/<filename>')
def serve_motion_clip(camera_id, filename):
    safe_camera = secure_filename(camera_id) or 'camera'
    safe_name = secure_filename(filename)
    clip_dir = Path(current_app.config['MOTION_CLIP_DIR']) / safe_camera
    file_path = clip_dir / safe_name
    if not file_path.exists():
        return jsonify({'success': False, 'error': 'Clip not found.'}), 404
    return send_from_directory(clip_dir, safe_name)
