import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

bp = Blueprint('main', __name__)


@bp.route('/')
def index():
    return render_template('index.html')


@bp.route('/api/motion-clips', methods=['GET', 'POST'])
def motion_clips():
    if request.method == 'POST':
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

    base_dir = Path(current_app.config['MOTION_CLIP_DIR'])
    camera_filter = secure_filename(request.args.get('camera_id', ''))
    limit = request.args.get('limit', '50')
    try:
        limit = max(1, min(500, int(limit)))
    except ValueError:
        limit = 50

    entries = []
    cameras = set()
    if base_dir.exists():
        for camera_dir in base_dir.iterdir():
            if not camera_dir.is_dir():
                continue
            camera_name = camera_dir.name
            cameras.add(camera_name)
            if camera_filter and camera_name != camera_filter:
                continue
            for clip_path in camera_dir.glob('*.webm'):
                try:
                    stat = clip_path.stat()
                except OSError:
                    continue
                entries.append({
                    'camera_id': camera_name,
                    'filename': clip_path.name,
                    'bytes': stat.st_size,
                    'recorded_at': datetime.utcfromtimestamp(stat.st_mtime).isoformat() + 'Z',
                    'clip_url': f"/motion-clips/{camera_name}/{clip_path.name}",
                    '_ts': stat.st_mtime
                })

    entries.sort(key=lambda item: item['_ts'], reverse=True)
    clips = [{k: v for k, v in entry.items() if k != '_ts'} for entry in entries[:limit]]
    return jsonify({
        'success': True,
        'clips': clips,
        'total': len(entries),
        'cameras': sorted(cameras)
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
