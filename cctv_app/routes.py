import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

bp = Blueprint('main', __name__)
CLIP_METADATA_LOCK = threading.Lock()


@bp.route('/')
def index():
    return render_template('index.html')


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _metadata_path():
    base_dir = Path(current_app.config['MOTION_CLIP_DIR'])
    filename = current_app.config.get('CLIP_METADATA_FILENAME', '_clips.json')
    return base_dir / filename


def _read_metadata():
    path = _metadata_path()
    if not path.exists():
        return {'clips': {}}
    try:
        with path.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {'clips': {}}
    clips = data.get('clips')
    if not isinstance(clips, dict):
        data['clips'] = {}
    return data


def _write_metadata(data):
    path = _metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix('.tmp')
    with temp_path.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2)
    temp_path.replace(path)


def _metadata_snapshot():
    with CLIP_METADATA_LOCK:
        data = _read_metadata()
    return data.get('clips', {})


def _clip_key(camera_id, filename):
    return f"{camera_id}/{filename}"


def _safe_camera(name):
    return secure_filename(name or 'camera') or 'camera'


def _safe_filename(name):
    candidate = secure_filename(name or '')
    if candidate:
        return candidate
    return f"clip-{int(time.time())}-{uuid4().hex[:6]}.webm"


def _mutate_clip_metadata(camera_id, filename, mutator):
    with CLIP_METADATA_LOCK:
        data = _read_metadata()
        key = _clip_key(camera_id, filename)
        entry = data['clips'].get(key)
        if not entry:
            entry = {
                'camera_id': camera_id,
                'filename': filename,
                'stats': {'views': 0, 'downloads': 0}
            }
        entry.setdefault('stats', {'views': 0, 'downloads': 0})
        mutator(entry)
        data['clips'][key] = entry
        _write_metadata(data)
        return entry


def _delete_clip_metadata(camera_id, filename):
    with CLIP_METADATA_LOCK:
        data = _read_metadata()
        data['clips'].pop(_clip_key(camera_id, filename), None)
        _write_metadata(data)


def _increment_clip_stat(camera_id, filename, field):
    valid = {'views', 'downloads'}
    if field not in valid:
        return None

    def mutate(entry):
        stats = entry.setdefault('stats', {'views': 0, 'downloads': 0})
        stats[field] = stats.get(field, 0) + 1
        stamp = _now_iso()
        if field == 'views':
            stats['last_viewed_at'] = stamp
        else:
            stats['last_downloaded_at'] = stamp

    return _mutate_clip_metadata(camera_id, filename, mutate)


def cleanup_expired_clips():
    retention = int(current_app.config.get('CLIP_RETENTION_SECONDS', 0) or 0)
    if retention <= 0:
        return
    now = time.time()
    cutoff = now - retention
    base_dir = Path(current_app.config['MOTION_CLIP_DIR'])
    if not base_dir.exists():
        return

    removed = False
    removed_list = []
    with CLIP_METADATA_LOCK:
        data = _read_metadata()
        # Use warning/print so messages appear in default Docker logs (info/debug may be filtered)
        current_app.logger.warning('Running cleanup_expired_clips: retention=%s seconds, cutoff=%s', retention, cutoff)
        try:
            print(f'Running cleanup_expired_clips: retention={retention} cutoff={cutoff}')
        except Exception:
            pass
        for camera_dir in base_dir.iterdir():
            if not camera_dir.is_dir():
                continue
            for clip_path in camera_dir.glob('*.webm'):
                try:
                    stat = clip_path.stat()
                except OSError:
                    continue

                key = _clip_key(camera_dir.name, clip_path.name)
                meta = data.get('clips', {}).get(key, {})
                # Prefer uploaded_at from metadata if present, otherwise fall back to file mtime
                uploaded_at = meta.get('uploaded_at')
                ts = None
                if uploaded_at:
                    try:
                        dt = _coerce_timestamp(uploaded_at)
                        ts = dt.timestamp()
                    except Exception:
                        ts = None
                if ts is None:
                    ts = stat.st_mtime

                if ts < cutoff:
                    try:
                        clip_path.unlink()
                        removed_list.append(str(clip_path))
                    except OSError:
                        current_app.logger.warning('Failed to unlink expired clip: %s', clip_path)
                        continue
                    data['clips'].pop(key, None)
                    removed = True

        if removed:
            try:
                _write_metadata(data)
            except Exception:
                current_app.logger.exception('Failed to write clip metadata after cleanup')

    if removed_list:
        current_app.logger.warning('Removed %d expired clips', len(removed_list))
        try:
            print(f'Removed {len(removed_list)} expired clips')
            for p in removed_list:
                print(p)
        except Exception:
            pass


def _coerce_float(value):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value):
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_timestamp(value):
    if value in (None, '', '0'):
        return datetime.now(timezone.utc)
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        stamp = str(value).replace('Z', '+00:00')
        dt = datetime.fromisoformat(stamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, OSError):
        return datetime.now(timezone.utc)


def _handle_clip_upload(source):
    clip = request.files.get('clip')
    if not clip:
        return jsonify({'success': False, 'error': 'Missing clip payload.'}), 400

    camera_id = _safe_camera(request.form.get('camera_id', 'camera'))
    recorded_at = _coerce_timestamp(request.form.get('timestamp'))
    recorded_at_iso = recorded_at.isoformat().replace('+00:00', 'Z')
    filename = _safe_filename(request.form.get('filename'))

    clip_dir = Path(current_app.config['MOTION_CLIP_DIR']) / camera_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    file_path = clip_dir / filename
    if file_path.exists():
        unique_name = f"{Path(filename).stem}-{uuid4().hex[:4]}.webm"
        filename = _safe_filename(unique_name)
        file_path = clip_dir / filename
    clip.save(file_path)
    file_size = file_path.stat().st_size if file_path.exists() else 0

    duration = _coerce_float(request.form.get('duration') or request.form.get('duration_seconds'))
    motion_percent = _coerce_float(request.form.get('motion_percent'))
    motion_avg_percent = _coerce_float(request.form.get('motion_avg_percent'))
    motion_std_percent = _coerce_float(request.form.get('motion_std_percent'))
    motion_sample_count = _coerce_int(request.form.get('motion_sample_count'))
    frequency_minutes = _coerce_float(request.form.get('frequency_minutes'))
    note = request.form.get('note', '').strip() or None
    recorded_by = request.form.get('recorded_by') or camera_id
    favorite_flag = str(request.form.get('favorite', 'false')).lower() in {'1', 'true', 'yes', 'on'}
    uploaded_at = _now_iso()

    payload = {
        'camera_id': camera_id,
        'filename': filename,
        'recorded_at': recorded_at_iso,
        'uploaded_at': uploaded_at,
        'bytes': file_size,
        'source': source,
        'recorded_by': recorded_by,
        'favorite': favorite_flag
    }
    if duration is not None:
        payload['duration_seconds'] = duration
    if motion_percent is not None:
        payload['motion_percent'] = motion_percent
    if motion_avg_percent is not None:
        payload['motion_avg_percent'] = motion_avg_percent
    if motion_std_percent is not None:
        payload['motion_std_percent'] = motion_std_percent
    if motion_sample_count is not None and motion_sample_count >= 0:
        payload['motion_sample_count'] = motion_sample_count
    if frequency_minutes is not None:
        payload['frequency_minutes'] = max(0.0, frequency_minutes)
    if note:
        payload['note'] = note

    def mutate(entry):
        entry.update(payload)
        entry.setdefault('stats', {'views': 0, 'downloads': 0})
        if favorite_flag:
            entry['favorite_at'] = uploaded_at
        else:
            entry.pop('favorite_at', None)

    record = _mutate_clip_metadata(camera_id, filename, mutate)

    response = {
        **payload,
        'clip_url': f"/motion-clips/{camera_id}/{filename}",
        'stats': record.get('stats', {})
    }
    if motion_percent is not None:
        response['motion_percent'] = motion_percent
    if motion_avg_percent is not None:
        response['motion_avg_percent'] = motion_avg_percent
    if motion_std_percent is not None:
        response['motion_std_percent'] = motion_std_percent
    if motion_sample_count is not None and motion_sample_count >= 0:
        response['motion_sample_count'] = motion_sample_count
    if duration is not None:
        response['duration_seconds'] = duration
    if frequency_minutes is not None:
        response['frequency_minutes'] = max(0.0, frequency_minutes)
    if note:
        response['note'] = note

    return jsonify({'success': True, **response})


@bp.route('/api/motion-clips', methods=['GET', 'POST'])
def motion_clips():
    if request.method == 'POST':
        return _handle_clip_upload('motion')

    cleanup_expired_clips()
    base_dir = Path(current_app.config['MOTION_CLIP_DIR'])
    camera_filter = _safe_camera(request.args.get('camera_id', '')) if request.args.get('camera_id') else ''
    limit = request.args.get('limit', '50')
    try:
        limit = max(1, min(500, int(limit)))
    except ValueError:
        limit = 50

    metadata = _metadata_snapshot()
    entries = []
    cameras = set()
    total_bytes = 0

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
                key = _clip_key(camera_name, clip_path.name)
                meta = metadata.get(key, {})
                entry = {
                    'camera_id': camera_name,
                    'filename': clip_path.name,
                    'bytes': stat.st_size,
                    'recorded_at': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'clip_url': f"/motion-clips/{camera_name}/{clip_path.name}",
                    '_ts': stat.st_mtime,
                    'favorite': bool(meta.get('favorite', False)),
                    'source': meta.get('source', 'motion'),
                    'recorded_by': meta.get('recorded_by', camera_name),
                    'stats': meta.get('stats', {'views': 0, 'downloads': 0})
                }
                if 'duration_seconds' in meta:
                    entry['duration_seconds'] = meta['duration_seconds']
                if 'motion_percent' in meta:
                    entry['motion_percent'] = meta['motion_percent']
                if 'motion_avg_percent' in meta:
                    entry['motion_avg_percent'] = meta['motion_avg_percent']
                if 'motion_std_percent' in meta:
                    entry['motion_std_percent'] = meta['motion_std_percent']
                if 'motion_sample_count' in meta:
                    entry['motion_sample_count'] = meta['motion_sample_count']
                if 'uploaded_at' in meta:
                    entry['uploaded_at'] = meta['uploaded_at']
                if 'favorite_at' in meta:
                    entry['favorite_at'] = meta['favorite_at']
                if 'note' in meta:
                    entry['note'] = meta['note']
                if 'frequency_minutes' in meta:
                    entry['frequency_minutes'] = meta['frequency_minutes']
                entries.append(entry)
                total_bytes += stat.st_size

    entries.sort(key=lambda item: item['_ts'], reverse=True)
    favorites_limit = current_app.config.get('CLIP_FAVORITES_LIMIT', 100)
    favorites = [
        {k: v for k, v in entry.items() if k != '_ts'}
        for entry in entries
        if entry.get('favorite')
    ][:favorites_limit]

    clips = []
    for entry in entries[:limit]:
        clips.append({k: v for k, v in entry.items() if k != '_ts'})

    summary = {
        'total_clips': len(entries),
        'favorite_clips': sum(1 for entry in entries if entry.get('favorite')),
        'total_bytes': total_bytes,
        'last_scanned_at': _now_iso()
    }

    return jsonify({
        'success': True,
        'clips': clips,
        'total': len(entries),
        'cameras': sorted(cameras),
        'favorites': favorites,
        'summary': summary
    })


@bp.route('/api/motion-clips/manual', methods=['POST'])
def manual_clip_upload():
    return _handle_clip_upload('manual')


@bp.route('/api/motion-clips/scheduled', methods=['POST'])
def scheduled_clip_upload():
    return _handle_clip_upload('scheduled')


@bp.route('/api/motion-clips/<camera_id>/<filename>', methods=['DELETE'])
def delete_clip(camera_id, filename):
    safe_camera = _safe_camera(camera_id)
    safe_name = _safe_filename(filename)
    clip_dir = Path(current_app.config['MOTION_CLIP_DIR']) / safe_camera
    file_path = clip_dir / safe_name
    if not file_path.exists():
        return jsonify({'success': False, 'error': 'Clip not found.'}), 404
    try:
        file_path.unlink()
    except OSError:
        return jsonify({'success': False, 'error': 'Unable to delete clip file.'}), 500
    _delete_clip_metadata(safe_camera, safe_name)
    return jsonify({'success': True})


@bp.route('/api/motion-clips/<camera_id>/<filename>/favorite', methods=['PATCH'])
def toggle_favorite(camera_id, filename):
    safe_camera = _safe_camera(camera_id)
    safe_name = _safe_filename(filename)
    clip_dir = Path(current_app.config['MOTION_CLIP_DIR']) / safe_camera
    file_path = clip_dir / safe_name
    if not file_path.exists():
        return jsonify({'success': False, 'error': 'Clip not found.'}), 404

    payload = request.get_json(silent=True) or {}
    favorite = bool(payload.get('favorite'))

    def mutate(entry):
        entry['favorite'] = favorite
        if favorite:
            entry['favorite_at'] = _now_iso()
        else:
            entry.pop('favorite_at', None)

    record = _mutate_clip_metadata(safe_camera, safe_name, mutate)
    return jsonify({
        'success': True,
        'favorite': favorite,
        'clip': record,
        'clip_url': f"/motion-clips/{safe_camera}/{safe_name}"
    })


@bp.route('/motion-clips/<camera_id>/<filename>')
def serve_motion_clip(camera_id, filename):
    safe_camera = _safe_camera(camera_id)
    safe_name = _safe_filename(filename)
    clip_dir = Path(current_app.config['MOTION_CLIP_DIR']) / safe_camera
    file_path = clip_dir / safe_name
    if not file_path.exists():
        return jsonify({'success': False, 'error': 'Clip not found.'}), 404
    _increment_clip_stat(safe_camera, safe_name, 'downloads')
    return send_from_directory(clip_dir, safe_name)
