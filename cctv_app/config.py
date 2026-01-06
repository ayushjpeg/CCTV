import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CLIP_DIR = BASE_DIR / 'motion_clips'
MOTION_CLIP_DIR = Path(os.getenv('MOTION_CLIP_DIR', DEFAULT_CLIP_DIR))
MOTION_CLIP_DIR.mkdir(parents=True, exist_ok=True)

BROADCASTER_HEARTBEAT_TIMEOUT = int(os.getenv('BROADCASTER_HEARTBEAT_TIMEOUT', '300'))
CLIP_RETENTION_DAYS = max(0, int(os.getenv('CLIP_RETENTION_DAYS', '14')))
CLIP_RETENTION_SECONDS = CLIP_RETENTION_DAYS * 24 * 60 * 60
CLIP_METADATA_FILENAME = os.getenv('CLIP_METADATA_FILENAME', '_clips.json')
CLIP_FAVORITES_LIMIT = max(1, int(os.getenv('CLIP_FAVORITES_LIMIT', '100')))
