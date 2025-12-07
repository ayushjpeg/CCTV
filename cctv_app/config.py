import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CLIP_DIR = BASE_DIR / 'motion_clips'
MOTION_CLIP_DIR = Path(os.getenv('MOTION_CLIP_DIR', DEFAULT_CLIP_DIR))
MOTION_CLIP_DIR.mkdir(parents=True, exist_ok=True)

BROADCASTER_HEARTBEAT_TIMEOUT = int(os.getenv('BROADCASTER_HEARTBEAT_TIMEOUT', '300'))
