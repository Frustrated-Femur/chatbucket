"""
push_notifications.py — Background FCM push notification delivery for ChatBucket.

Handles:
1. Token registration / persistence in `push_tokens/`.
2. Token replacement on device rotation.
3. OAuth2 Service Account / FCM HTTP v1 message delivery to Android devices.
4. Automatic pruning of expired or unregistered tokens.
5. Message filtering: real messages only (no join/leave/typing/status/ping/pong/delete/edit).
"""

import os
import json
import time
import base64
import hashlib
import secrets
import threading
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

PUSH_TOKENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "push_tokens")
_tokens_lock = threading.Lock()

# Optional presence integration. When a recipient is actively connected over
# the live websocket, the app already surfaces the message in-app; sending a
# push too would double-notify. We import presence_state lazily/guarded so
# this module keeps working even if presence tracking is unavailable.
try:
    import presence_state
except Exception:  # pragma: no cover
    presence_state = None
_push_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fcm-push")

# Cache for Google OAuth2 access token: (token, expiry_epoch)
_oauth_token_cache = {"token": None, "expires_at": 0, "project_id": None}
_oauth_lock = threading.Lock()


def _ensure_dir():
    if not os.path.exists(PUSH_TOKENS_DIR):
        try:
            os.makedirs(PUSH_TOKENS_DIR, exist_ok=True)
        except Exception:
            pass


_ensure_dir()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def register_token(user: str, token: str, device_id: str = None, platform: str = "android") -> bool:
    """Register or update an FCM token for a user/device."""
    if not user or not token:
        return False
    user = user.strip()
    token = token.strip()
    if not user or not token:
        return False

    _ensure_dir()
    now_iso = datetime.now(timezone.utc).isoformat()
    t_hash = _token_hash(token)
    fpath = os.path.join(PUSH_TOKENS_DIR, f"{t_hash}.json")

    record = {
        "user": user,
        "token": token,
        "device_id": device_id or "",
        "platform": platform or "android",
        "registered_at": now_iso,
        "last_seen": now_iso,
    }

    with _tokens_lock:
        # If device_id is provided, check if an old token for this same device exists and clean it up
        if device_id:
            for fname in os.listdir(PUSH_TOKENS_DIR):
                if not fname.endswith(".json") or fname == f"{t_hash}.json":
                    continue
                old_path = os.path.join(PUSH_TOKENS_DIR, fname)
                try:
                    with open(old_path, "r", encoding="utf-8") as f:
                        old_rec = json.load(f)
                    if old_rec.get("device_id") == device_id and old_rec.get("user", "").lower() == user.lower():
                        try:
                            os.remove(old_path)
                        except Exception:
                            pass
                except Exception:
                    pass

        # Write current token
        tmp_path = f"{fpath}.tmp.{secrets.token_hex(4)}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
            os.replace(tmp_path, fpath)
            return True
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            print(f"[push_notifications] Failed to save token {t_hash}: {e}")
            return False


def unregister_token(token: str) -> bool:
    """Remove a specific token."""
    if not token:
        return False
    t_hash = _token_hash(token.strip())
    fpath = os.path.join(PUSH_TOKENS_DIR, f"{t_hash}.json")
    with _tokens_lock:
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
                return True
            except Exception:
                pass
    return False


def remove_token(token: str):
    """Alias for unregistering dead/invalid tokens."""
    unregister_token(token)


def get_recipient_tokens(sender_user: str = "", skip_online: bool = True) -> list:
    """Return all registered tokens for recipients other than sender_user.

    When skip_online is True (default) and presence tracking is available,
    recipients that are currently ONLINE over the live websocket are omitted:
    the connected app already surfaces the message in-app, so a push would be
    a duplicate. Devices with no live connection (app closed / tailscale up)
    still get the push — which is exactly the background-notification case.
    """
    _ensure_dir()
    recipients = []
    sender_lower = (sender_user or "").strip().lower()

    with _tokens_lock:
        if not os.path.exists(PUSH_TOKENS_DIR):
            return recipients
        for fname in os.listdir(PUSH_TOKENS_DIR):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(PUSH_TOKENS_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    rec = json.load(f)
                u = rec.get("user", "").strip()
                t = rec.get("token", "").strip()
                if t and u.lower() != sender_lower:
                    recipients.append(rec)
            except Exception:
                pass

    if skip_online and presence_state is not None:
        try:
            recipients = [
                r for r in recipients
                if not presence_state.is_online(r.get("user", ""))
            ]
        except Exception:
            # Presence unavailable — fall back to notifying everyone except sender.
            pass

    return recipients


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _find_service_account() -> dict:
    """Locate and load Firebase service account configuration."""
    candidates = []

    # 1. Environment variable: JSON payload or file path
    env_sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_sa:
        if env_sa.strip().startswith("{"):
            try:
                return json.loads(env_sa)
            except Exception:
                pass
        else:
            candidates.append(env_sa)

    # 2. Local directory candidates
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(cur_dir)
    candidates.extend([
        os.path.join(cur_dir, "fcm_service_account.json"),
        os.path.join(cur_dir, "service-account.json"),
        os.path.join(cur_dir, "firebase-adminsdk.json"),
        os.path.join(parent_dir, "fcm_service_account.json"),
        os.path.join(parent_dir, "service-account.json"),
        os.path.join(parent_dir, "firebase-adminsdk.json"),
    ])

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "client_email" in data and ("private_key" in data or "project_id" in data):
                    return data
            except Exception as e:
                print(f"[push_notifications] Error reading service account at {path}: {e}")

    return None


def _get_fcm_access_token(service_account: dict) -> tuple:
    """Generate or reuse Google OAuth2 access token using RS256 JWT signature."""
    global _oauth_token_cache
    now = int(time.time())

    with _oauth_lock:
        if _oauth_token_cache["token"] and _oauth_token_cache["expires_at"] > now + 60:
            return _oauth_token_cache["token"], _oauth_token_cache["project_id"]

    client_email = service_account.get("client_email")
    private_key = service_account.get("private_key")
    project_id = service_account.get("project_id")

    if not client_email or not private_key or not project_id:
        return None, None

    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    payload = _b64url(json.dumps({
        "iss": client_email,
        "scope": "https://www.googleapis.com/auth/firebase.messaging",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600,
        "iat": now
    }).encode("utf-8"))

    signing_input = f"{header}.{payload}".encode("ascii")

    # Sign using openssl CLI or python cryptography if available
    sig_b64 = None
    try:
        tmp_key = os.path.join(PUSH_TOKENS_DIR, f".tmp_key_{secrets.token_hex(4)}.pem")
        with open(tmp_key, "w", encoding="utf-8") as f:
            f.write(private_key)
        try:
            res = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", tmp_key],
                input=signing_input,
                capture_output=True,
                check=True
            )
            sig_b64 = _b64url(res.stdout)
        finally:
            if os.path.exists(tmp_key):
                try:
                    os.remove(tmp_key)
                except Exception:
                    pass
    except Exception as e:
        print(f"[push_notifications] OpenSSL signing failed: {e}")
        return None, None

    jwt_token = f"{header}.{payload}.{sig_b64}"

    # Request OAuth2 access token from Google
    token_url = "https://oauth2.googleapis.com/token"
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token
    }).encode("utf-8")

    req = urllib.request.Request(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = json.loads(resp.read().decode("utf-8"))
            access_token = resp_body.get("access_token")
            expires_in = resp_body.get("expires_in", 3600)
            with _oauth_lock:
                _oauth_token_cache = {
                    "token": access_token,
                    "expires_at": now + int(expires_in),
                    "project_id": project_id
                }
            return access_token, project_id
    except Exception as e:
        print(f"[push_notifications] Failed to fetch Google OAuth2 token: {e}")
        return None, None


def format_message_preview(msg: dict) -> str:
    """Format notification content according to ChatBucket message type."""
    msg_type = msg.get("type") or "text"
    is_sticker = bool(msg.get("isSticker"))
    filename = (msg.get("filename") or "").strip()
    caption = (msg.get("caption") or "").strip()
    text = (msg.get("text") or "").strip()

    if is_sticker:
        return "Sticker"

    if msg_type == "youtube":
        title = (msg.get("title") or "").strip()
        return f"🎬 {title}" if title else "Shared a YouTube video"

    if msg_type == "ytdlp_audio":
        title = (msg.get("title") or "").strip()
        return f"🎵 {title}" if title else "Shared an audio track"

    if msg_type == "file" or filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext == ".gif":
            return f"GIF: {caption}" if caption else "GIF"
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".avif"}:
            return f"📷 Photo: {caption}" if caption else "📷 Photo"
        if ext in {".mp4", ".webm", ".mov", ".mkv", ".avi"}:
            return f"🎥 Video: {caption}" if caption else "🎥 Video"
        if ext in {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}:
            return f"🎵 {caption}" if caption else f"🎵 {filename}"
        if ext == ".pdf":
            return f"📄 {filename}"
        return f"📄 {filename}"

    if text:
        return text

    return "New message"


def _send_fcm_v1(token: str, title: str, body: str, msg: dict, access_token: str, project_id: str):
    """Send a single push notification via FCM HTTP v1 API."""
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body
            },
            "data": {
                "msg_id": str(msg.get("id") or ""),
                "user": str(msg.get("user") or ""),
                "type": str(msg.get("type") or "text"),
                "text": str(msg.get("text") or ""),
                "filename": str(msg.get("filename") or ""),
                "caption": str(msg.get("caption") or ""),
                "is_sticker": "true" if msg.get("isSticker") else "false",
                "timestamp": str(msg.get("timestamp") or "")
            },
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": "chatbucket_messages",
                    "default_sound": True,
                    "default_vibrate_timings": True
                }
            }
        }
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; UTF-8",
            "Authorization": f"Bearer {access_token}"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            # Successfully delivered to FCM
            pass
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"[push_notifications] FCM send error {e.code} for token {token[:12]}...: {err_body}")

        # Check for unregistered or invalid tokens
        if e.code in (404, 400) or "UNREGISTERED" in err_body or "NOT_FOUND" in err_body or "INVALID_ARGUMENT" in err_body:
            print(f"[push_notifications] Removing dead/unregistered token {token[:12]}...")
            remove_token(token)
    except Exception as e:
        print(f"[push_notifications] FCM delivery network error: {e}")


def _dispatch_push_worker(sender: str, msg: dict):
    """Worker function executed in background thread pool."""
    service_account = _find_service_account()
    if not service_account:
        # No service account configured yet on server
        return

    access_token, project_id = _get_fcm_access_token(service_account)
    if not access_token or not project_id:
        return

    tokens = get_recipient_tokens(sender)
    if not tokens:
        return

    title = sender or "ChatBucket"
    preview = format_message_preview(msg)

    for rec in tokens:
        t = rec.get("token")
        if t:
            try:
                _send_fcm_v1(t, title, preview, msg, access_token, project_id)
            except Exception as e:
                print(f"[push_notifications] Worker dispatch error: {e}")


def notify_new_message(msg: dict):
    """
    Public trigger: filters events and asynchronously sends background push notifications.
    Safe to call from any route / websocket handler without blocking.
    """
    if not msg or not isinstance(msg, dict):
        return

    msg_type = msg.get("type")
    # Never push ephemeral or system control events
    if msg_type in ("ping", "pong", "typing", "status", "init", "joined", "left", "delete", "edit", "delete_result", "edit_rejected"):
        return

    if msg.get("deleted"):
        return

    sender = (msg.get("user") or "").strip()
    if not sender:
        return

    # Asynchronously dispatch in thread pool
    _push_executor.submit(_dispatch_push_worker, sender, dict(msg))
