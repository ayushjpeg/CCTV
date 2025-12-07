import threading
from collections import defaultdict

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
