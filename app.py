"""
Free Fire Player Info API
Credentials: Guest account with full player data extraction
Supports: High concurrency, token pooling, response caching, rate limiting
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from threading import BoundedSemaphore, Lock
from typing import Any, Dict, List, Tuple

import httpx
from Crypto.Cipher import AES
from cachetools import TTLCache
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from google.protobuf import json_format

sys.path.insert(0, os.path.dirname(__file__))
from proto import AccountPersonalShow_pb2, FreeFire_pb2, main_pb2

# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("FFInfo")

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────
MAIN_KEY        = base64.b64decode("WWcmdGMlREV1aDYlWmNeOA==")
MAIN_IV         = base64.b64decode("Nm95WkRyMjJFM3ljaGpNJQ==")
RELEASE_VERSION = "OB54"
USER_AGENT      = "ART/2.2.0 (Linux; U; Android 14; SAMSUNG_S25 Build/UP1A.240905.001)"
CLIENT_SECRET   = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"
CLIENT_ID       = "100067"
OAUTH_URL       = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
LOGIN_URL       = "https://loginbp.ggblueshark.com/MajorLogin"
TOKEN_TTL       = 25200   # 7 hours
CACHE_TTL       = 300     # 5 minutes
API_VERSION     = "3.0.0"

SUPPORTED_REGIONS = {
    "IND", "BR", "US", "SAC", "NA", "SG", "RU",
    "ID", "TW", "VN", "TH", "ME", "PK", "CIS", "BD", "EUROPE",
}

def _env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# The supplied guest account is used by default and can be overridden with
# FF_GUEST_UID / FF_GUEST_PASSWORD in production. Never log these values.
GUEST_ACCOUNT_INFO: Dict[str, str] = {
    "com.garena.msdk.guest_password": _env_or_default(
        "FF_GUEST_PASSWORD",
        "6097634A64F942696E7C7D527A1C65D45FADDE735D6D793AF9B3C25ACAF3794E",
    ),
    "com.garena.msdk.guest_uid": _env_or_default("FF_GUEST_UID", "6966459454"),
}
GUEST_CREDENTIAL = (
    f"uid={GUEST_ACCOUNT_INFO['com.garena.msdk.guest_uid']}"
    f"&password={GUEST_ACCOUNT_INFO['com.garena.msdk.guest_password']}"
)

# Region-specific accounts are retained where available; BD and all fallback
# requests use the supplied guest account rather than the stale default.
REGION_CREDENTIALS: Dict[str, str] = {
    "BD":     GUEST_CREDENTIAL,
    "IND":    "uid=3197059560&password=3EC146CD4EEF7A640F2967B06D7F4413BD4FB37382E0ED260E214E8BACD96734",
    "BR":     "uid=3939493997&password=D08775EC0CCCEA77B2426EBC4CF04C097E0D58822804756C02738BF37578EE17",
    "US":     "uid=3939493997&password=D08775EC0CCCEA77B2426EBC4CF04C097E0D58822804756C02738BF37578EE17",
    "SAC":    "uid=3939493997&password=D08775EC0CCCEA77B2426EBC4CF04C097E0D58822804756C02738BF37578EE17",
    "NA":     "uid=3939493997&password=D08775EC0CCCEA77B2426EBC4CF04C097E0D58822804756C02738BF37578EE17",
}
DEFAULT_CREDENTIAL = GUEST_CREDENTIAL

RATE_LIMIT_MAX    = 100
RATE_LIMIT_WINDOW = 100
UPSTREAM_MAX_RETRIES = _int_env("UPSTREAM_MAX_RETRIES", 3, 0)
UPSTREAM_BACKOFF_BASE = _float_env("UPSTREAM_BACKOFF_BASE", 1.0, 0.1)
UPSTREAM_MAX_BACKOFF = _float_env("UPSTREAM_MAX_BACKOFF", 8.0, 1.0)
UPSTREAM_MAX_CONCURRENCY = _int_env("UPSTREAM_MAX_CONCURRENCY", 2, 1)
INIT_TOKEN_CONCURRENCY = _int_env("INIT_TOKEN_CONCURRENCY", 2, 1)

# ─────────────────────────────────────────────────────────────────────────────
#  Flask app
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.json.sort_keys = False
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

response_cache:     TTLCache          = TTLCache(maxsize=1000, ttl=CACHE_TTL)
cached_tokens:      Dict[str, Dict]   = {}
rate_limit_store:   Dict[str, List]   = defaultdict(list)
token_lock:         Lock              = Lock()
token_refresh_locks: Dict[str, Lock] = defaultdict(Lock)
upstream_slots = BoundedSemaphore(UPSTREAM_MAX_CONCURRENCY)

# ─────────────────────────────────────────────────────────────────────────────
#  Crypto helpers
# ─────────────────────────────────────────────────────────────────────────────
def _pad(data: bytes) -> bytes:
    n = AES.block_size - (len(data) % AES.block_size)
    return data + bytes([n] * n)

def aes_encrypt(data: bytes) -> bytes:
    return AES.new(MAIN_KEY, AES.MODE_CBC, MAIN_IV).encrypt(_pad(data))

def parse_proto(raw: bytes, proto_type):
    msg = proto_type()
    msg.ParseFromString(raw)
    return msg

async def serialize_proto(obj: dict, proto_msg) -> bytes:
    json_format.ParseDict(obj, proto_msg)
    return proto_msg.SerializeToString()


def _retry_after_seconds(response: httpx.Response, default: float) -> int:
    """Read Retry-After safely and cap it for serverless request latency."""
    raw = response.headers.get("Retry-After", "").strip()
    if raw:
        try:
            return max(1, min(int(float(raw)), int(UPSTREAM_MAX_BACKOFF)))
        except ValueError:
            try:
                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                seconds = int((target - datetime.now(timezone.utc)).total_seconds())
                return max(1, min(seconds, int(UPSTREAM_MAX_BACKOFF)))
            except (TypeError, ValueError, OverflowError):
                pass
    return max(1, min(int(default), int(UPSTREAM_MAX_BACKOFF)))


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    data: Any,
    headers: Dict[str, str],
) -> httpx.Response:
    """Serialize upstream traffic and retry transient 429/5xx responses."""
    last_error = None
    for attempt in range(UPSTREAM_MAX_RETRIES + 1):
        await asyncio.to_thread(upstream_slots.acquire)
        try:
            response = await client.post(url, data=data, headers=headers)
        except httpx.RequestError as exc:
            last_error = exc
            response = None
        finally:
            upstream_slots.release()

        if response is not None:
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= UPSTREAM_MAX_RETRIES:
                return response
            exponential = min(
                UPSTREAM_MAX_BACKOFF,
                UPSTREAM_BACKOFF_BASE * (2 ** attempt),
            )
            delay = _retry_after_seconds(response, exponential)
            delay = min(UPSTREAM_MAX_BACKOFF, max(exponential, delay))
            logger.warning(
                "[Upstream] status=%s attempt=%s/%s; retrying in %.1fs",
                response.status_code,
                attempt + 1,
                UPSTREAM_MAX_RETRIES + 1,
                delay,
            )
        else:
            if attempt >= UPSTREAM_MAX_RETRIES:
                raise last_error
            delay = min(
                UPSTREAM_MAX_BACKOFF,
                UPSTREAM_BACKOFF_BASE * (2 ** attempt),
            )
            logger.warning(
                "[Upstream] request error attempt=%s/%s; retrying in %.1fs",
                attempt + 1,
                UPSTREAM_MAX_RETRIES + 1,
                delay,
            )

        await asyncio.sleep(delay + random.uniform(0.0, 0.2))

    raise last_error or RuntimeError("Upstream request failed")


# ─────────────────────────────────────────────────────────────────────────────
#  Token management
# ─────────────────────────────────────────────────────────────────────────────
def _get_creds(region: str) -> str:
    return REGION_CREDENTIALS.get(region.upper(), DEFAULT_CREDENTIAL)

async def _fetch_oauth(creds: str) -> Tuple[str, str]:
    payload = (
        f"{creds}&response_type=token&client_type=2"
        f"&client_secret={CLIENT_SECRET}&client_id={CLIENT_ID}"
    )
    async with httpx.AsyncClient(timeout=15) as c:
        r = await _post_with_retry(c, OAUTH_URL, data=payload, headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "Keep-Alive", "Accept-Encoding": "gzip",
        })
        r.raise_for_status()
        d = r.json()
    return d["access_token"], d["open_id"]

async def _fetch_game_token(access_token: str, open_id: str) -> Tuple[str, str]:
    proto_bytes = await serialize_proto({
        "open_id": open_id, "open_id_type": "4",
        "login_token": access_token, "orign_platform_type": "4",
    }, FreeFire_pb2.LoginReq())
    enc = aes_encrypt(proto_bytes)

    async with httpx.AsyncClient(timeout=15) as c:
        r = await _post_with_retry(c, LOGIN_URL, data=enc, headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/octet-stream",
            "Connection": "Keep-Alive", "Accept-Encoding": "gzip",
            "Expect": "100-continue", "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1", "ReleaseVersion": RELEASE_VERSION,
        })
        r.raise_for_status()

    res = json.loads(json_format.MessageToJson(
        parse_proto(r.content, FreeFire_pb2.LoginRes)
    ))
    token      = res.get("token", "")
    server_url = res.get("serverUrl", "")
    if not token:
        raise RuntimeError(f"No token in login response: {res}")
    return f"Bearer {token}", server_url

async def _create_token(region: str) -> None:
    region = region.upper()
    creds  = _get_creds(region)
    access_token, open_id = await _fetch_oauth(creds)
    bearer, server_url    = await _fetch_game_token(access_token, open_id)
    with token_lock:
        cached_tokens[region] = {
            "token":      bearer,
            "server_url": server_url,
            "expires_at": time.time() + TOKEN_TTL,
            "refreshed":  datetime.now(timezone.utc).isoformat(),
        }
    logger.info(f"[Token] Refreshed → {region} | server={server_url}")

async def _get_token(region: str) -> Tuple[str, str]:
    region = region.upper()
    with token_lock:
        info = cached_tokens.get(region)
    if info and time.time() < info["expires_at"] - 300:
        return info["token"], info["server_url"]

    # Re-check after acquiring the per-region lock so concurrent requests do
    # not all refresh the same guest token and trigger an upstream 429.
    refresh_lock = token_refresh_locks[region]
    await asyncio.to_thread(refresh_lock.acquire)
    try:
        with token_lock:
            info = cached_tokens.get(region)
        if info and time.time() < info["expires_at"] - 300:
            return info["token"], info["server_url"]
        await _create_token(region)
    finally:
        refresh_lock.release()

    with token_lock:
        info = cached_tokens[region]
    return info["token"], info["server_url"]


async def init_tokens() -> None:
    limiter = asyncio.Semaphore(INIT_TOKEN_CONCURRENCY)

    async def refresh(region: str):
        async with limiter:
            return await _create_token(region)

    tasks = [refresh(r) for r in SUPPORTED_REGIONS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    logger.info(f"[Startup] Tokens: {ok}/{len(SUPPORTED_REGIONS)} OK")

# ─────────────────────────────────────────────────────────────────────────────
#  Player data fetch + full extraction
# ─────────────────────────────────────────────────────────────────────────────
async def _call_player_api(uid: int, region: str) -> Dict[str, Any]:
    bearer, server_url = await _get_token(region)
    payload = await serialize_proto({"a": uid, "b": 7}, main_pb2.GetPlayerPersonalShow())
    enc     = aes_encrypt(payload)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await _post_with_retry(
            c,
            server_url + "/GetPlayerPersonalShow",
            data=enc,
            headers={
                "User-Agent": USER_AGENT, "Content-Type": "application/octet-stream",
                "Connection": "Keep-Alive", "Accept-Encoding": "gzip",
                "Expect": "100-continue", "Authorization": bearer,
                "X-Unity-Version": "2018.4.11f1", "X-GA": "v1 1",
                "ReleaseVersion": RELEASE_VERSION,
            },
        )
        r.raise_for_status()
    return json.loads(json_format.MessageToJson(
        parse_proto(r.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)
    ))

def _fmt_ts(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return None

def _drop_none(value: Any) -> Any:
    """Remove absent values while preserving every value returned by the game."""
    if isinstance(value, dict):
        return {
            key: _drop_none(item)
            for key, item in value.items()
            if item is not None and not (
                isinstance(item, str) and item.strip().lower() in {"null", "n/a"}
            )
        }
    if isinstance(value, list):
        return [_drop_none(item) for item in value if item is not None]
    return value


def _clean_upstream_payload(value: Any) -> Any:
    """Return the complete upstream payload without null/N/A placeholders.

    This deliberately does not fill missing fields with made-up values. The
    game server only sends some sections for some accounts; removing absent
    values keeps the response JSON valid and makes sure clients never receive
    null or N/A placeholders while retaining all sections and fields that were
    actually returned.
    """
    return _drop_none(value)


def _account_details(
    account: Dict[str, Any],
    fallback_uid: Any = None,
    include_full: bool = True,
) -> Dict[str, Any]:
    """Return one canonical account profile without duplicate raw aliases."""
    account = account if isinstance(account, dict) else {}
    account_id = account.get("accountId") or fallback_uid
    details = {
        "available": bool(account),
        "uid": str(account_id) if account_id is not None else None,
        "accountId": account.get("accountId"),
        "nickname": account.get("nickname"),
        "region": account.get("region"),
        "level": account.get("level"),
        "exp": account.get("exp"),
        "accountType": account.get("accountType"),
        "externalId": account.get("externalId"),
        "externalType": account.get("externalType"),
        "externalName": account.get("externalName"),
        "externalUid": account.get("externalUid"),
        "releaseVersion": account.get("releaseVersion"),
        "createAt": _fmt_ts(account.get("createAt")),
        "lastLoginAt": _fmt_ts(account.get("lastLoginAt")),
        "returnAt": _fmt_ts(account.get("returnAt")),
    }
    if include_full:
        details.update({
            "externalIcon": account.get("externalIcon"),
            "role": account.get("role"),
            "rank": account.get("rank"),
            "rankingPoints": account.get("rankingPoints"),
            "csRank": account.get("csRank"),
            "csRankingPoints": account.get("csRankingPoints"),
            "hasElitePass": account.get("hasElitePass"),
            "badgeCount": account.get("badgeCnt"),
            "badgeId": account.get("badgeId"),
            "seasonId": account.get("seasonId"),
            "liked": account.get("liked"),
            "isDeleted": account.get("isDeleted"),
            "showRank": account.get("showRank"),
            "showBrRank": account.get("showBrRank"),
            "showCsRank": account.get("showCsRank"),
            "maxRank": account.get("maxRank"),
            "csMaxRank": account.get("csMaxRank"),
            "maxRankingPoints": account.get("maxRankingPoints"),
            "peakRankPos": account.get("peakRankPos"),
            "csPeakRankPos": account.get("csPeakRankPos"),
            "periodicRank": account.get("periodicRank"),
            "periodicRankingPoints": account.get("periodicRankingPoints"),
            "headPic": account.get("headPic"),
            "bannerId": account.get("bannerId"),
            "pinId": account.get("pinId"),
            "title": account.get("title"),
            "gameBagShow": account.get("gameBagShow"),
            "weaponSkinShows": account.get("weaponSkinShows", []),
            "selectedItemSlots": account.get("selectedItemSlots", []),
            "veteranExpireAt": _fmt_ts(account.get("veteranExpireTime")),
            "membershipState": account.get("membershipState"),
        })
    cleaned = _drop_none(details)
    if cleaned.get("accountId") is not None and str(cleaned.get("accountId")) == str(cleaned.get("uid")):
        cleaned.pop("accountId", None)
    return cleaned


def _extract_all(raw: Dict, uid: int, region: str) -> Dict[str, Any]:
    """Return a detailed profile with one canonical copy of each field."""
    raw = raw if isinstance(raw, dict) else {}
    b = raw.get("basicInfo") or {}
    s = raw.get("socialInfo") or {}
    p = raw.get("profileInfo") or {}
    cl = raw.get("clanBasicInfo") or {}
    cp = raw.get("captainBasicInfo") or {}
    pt = raw.get("petInfo") or {}
    cr = raw.get("creditScoreInfo") or {}
    dc = raw.get("diamondCostRes") or {}
    highlights = b.get("socialHighLightsWithBasicInfo") or {}
    sh = highlights.get("socialBasicInfo") or {}

    basic = _account_details(b, uid, include_full=False)
    basic.update({
        "uid": str(uid),
        "region": b.get("region", region),
    })

    appearance = {
        "headPic": b.get("headPic"),
        "bannerId": b.get("bannerId"),
        "badgeId": b.get("badgeId"),
        "badgeCount": b.get("badgeCnt"),
        "pinId": b.get("pinId"),
        "title": b.get("title"),
        "gameBagShow": b.get("gameBagShow"),
        "selectedItemSlots": b.get("selectedItemSlots", []),
        "weaponSkinShows": b.get("weaponSkinShows", []),
        "externalIcon": b.get("externalIcon"),
        "externalIconInfo": b.get("externalIconInfo") or {},
    }

    avatar = {
        "avatarId": p.get("avatarId"),
        "skinColor": p.get("skinColor"),
        "isSelected": p.get("isSelected"),
        "isSelectedAwaken": p.get("isSelectedAwaken"),
        "isMarkedStar": p.get("isMarkedStar"),
        "clothes": p.get("clothes", []),
        "equipedSkills": p.get("equipedSkills", []),
        "pvePrimaryWeapon": p.get("pvePrimaryWeapon"),
        "unlockType": p.get("unlockType"),
        "unlockTime": p.get("unlockTime"),
        "endTime": p.get("endTime"),
        "clothesTailorEffects": p.get("clothesTailorEffects", []),
    }

    rank = {
        "brRank": b.get("rank"),
        "brRankingPoints": b.get("rankingPoints"),
        "brMaxRank": b.get("maxRank"),
        "brMaxRankingPoints": b.get("maxRankingPoints"),
        "brPeakRankPos": b.get("peakRankPos"),
        "brPeriodicRank": b.get("periodicRank"),
        "brPeriodicPoints": b.get("periodicRankingPoints"),
        "csRank": b.get("csRank"),
        "csRankingPoints": b.get("csRankingPoints"),
        "csMaxRank": b.get("csMaxRank"),
        "csPeakRankPos": b.get("csPeakRankPos"),
        "isCsRankingBan": b.get("isCsRankingBan"),
        "isDeleted": b.get("isDeleted"),
        "showRank": b.get("showRank"),
        "showBrRank": b.get("showBrRank"),
        "showCsRank": b.get("showCsRank"),
        "rankLeaderboardPos": raw.get("rankingLeaderboardPos"),
    }

    social = {
        "liked": b.get("liked"),
        "seasonId": b.get("seasonId"),
        "role": b.get("role"),
        "bio": s.get("signature") or sh.get("signature"),
        "gender": s.get("gender"),
        "language": s.get("language"),
        "timeOnline": s.get("timeOnline"),
        "timeActive": s.get("timeActive"),
        "modePrefer": s.get("modePrefer"),
        "rankShow": s.get("rankShow"),
        "battleTags": s.get("battleTag", []),
        "battleTagCount": s.get("battleTagCount", []),
        "socialTags": s.get("socialTag", []),
        "signatureBanExpireAt": _fmt_ts(s.get("signatureBanExpireTime")),
        "socialHighlights": highlights.get("socialHighLights", []),
        "leaderboardTitles": sh.get("leaderboardTitles") or {},
    }

    captain_id = cl.get("captainId") or (cp.get("accountId") if cp else None)
    clan_id = cl.get("clanId") or b.get("clanId")
    clan_name = cl.get("clanName") or b.get("clanName")
    has_guild = any(
        value is not None and str(value) not in ("", "0")
        for value in (clan_id, clan_name, captain_id)
    )
    owner = _account_details(cp, captain_id) if cp else {
        "available": False,
        "message": "Guild owner/captain details were not returned by the game server.",
    }
    guild = {
        "available": has_guild,
        "clanId": clan_id,
        "clanName": clan_name,
        "clanLevel": cl.get("clanLevel"),
        "memberNum": cl.get("memberNum"),
        "capacity": cl.get("capacity"),
        "honorPoint": cl.get("honorPoint"),
        "clanBadgeId": b.get("clanBadgeId"),
        "clanFrameId": b.get("clanFrameId"),
        "customBadge": b.get("customClanBadge"),
        "useCustomBadge": b.get("useCustomClanBadge"),
        "owner": owner,
    }
    if not owner.get("available") and captain_id is not None:
        guild["ownerUid"] = str(captain_id)
    if not has_guild:
        guild["message"] = "This player is not in a guild, or guild details were not returned."

    if pt:
        pet = {
            "available": True,
            "id": pt.get("id"),
            "name": pt.get("name"),
            "level": pt.get("level"),
            "exp": pt.get("exp"),
            "isSelected": pt.get("isSelected"),
            "skinId": pt.get("skinId"),
            "actions": pt.get("actions", []),
            "selectedSkill": pt.get("selectedSkillId"),
            "skills": pt.get("skills", []),
            "isMarkedStar": pt.get("isMarkedStar"),
            "endTime": pt.get("endTime"),
        }
    else:
        pet = {"available": False, "message": "Pet details were not returned by the game server."}

    if cr:
        credit = {
            "available": True,
            "creditScore": cr.get("creditScore"),
            "isInit": cr.get("isInit"),
            "rewardState": cr.get("rewardState"),
            "weeklyMatches": cr.get("weeklyMatchCnt"),
            "periodLikes": cr.get("periodicSummaryLikeCnt"),
            "periodIllegal": cr.get("periodicSummaryIllegalCnt"),
            "periodStartAt": _fmt_ts(cr.get("periodicSummaryStartTime")),
            "periodEndAt": _fmt_ts(cr.get("periodicSummaryEndTime")),
        }
    else:
        credit = {"available": False, "message": "Credit score details were not returned by the game server."}

    ep_history = [
        _drop_none({
            "eventId": ep.get("epEventId"),
            "ownedPass": ep.get("ownedPass"),
            "badge": ep.get("epBadge"),
            "badgeCnt": ep.get("badgeCnt"),
            "icon": ep.get("bpIcon"),
            "maxLevel": ep.get("maxLevel"),
            "name": ep.get("eventName"),
        })
        for ep in raw.get("historyEpInfo", [])
    ]

    news = [
        _drop_none({
            "type": n.get("type"),
            "updateTime": _fmt_ts(n.get("updateTime")),
            "content": n.get("content") or {},
        })
        for n in raw.get("news", [])
    ]

    prefs_raw = b.get("accountPrefers") or {}
    preferences = {
        "hideLobby": prefs_raw.get("hideMyLobby"),
        "pregameShowChoices": prefs_raw.get("pregameShowChoices", []),
        "brPregameShowChoices": prefs_raw.get("brPregameShowChoices", []),
        "hidePersonalInfo": prefs_raw.get("hidePersonalInfo"),
        "disableFriendSpectate": prefs_raw.get("disableFriendSpectate"),
        "hideOccupation": prefs_raw.get("hideOccupation"),
    }

    occupations = [
        _drop_none({
            "seasonId": o.get("seasonId"),
            "gameMode": o.get("gameMode"),
            "info": o.get("info") or {},
        })
        for o in b.get("selectOccupations", [])
    ]

    data = {
        "basic": basic,
        "appearance": appearance,
        "avatar": avatar,
        "rank": rank,
        "social": social,
        "guild": guild,
        "pet": pet,
        "elitePass": ep_history,
        "creditScore": credit,
        "diamondCost": dc,
        "achievements": [
            _drop_none({"achId": a.get("achId"), "level": a.get("level")})
            for a in raw.get("equippedAch", [])
        ],
        "recentNews": news,
        "preferences": preferences,
        "occupations": occupations,
        "championship": {
            "teamName": b.get("championshipTeamName"),
            "teamId": b.get("championshipTeamId"),
            "teamMemberNum": b.get("championshipTeamMemberNum"),
        },
        "membership": {
            "hasElitePass": b.get("hasElitePass"),
            "membershipState": b.get("membershipState"),
            "veteranExpireAt": _fmt_ts(b.get("veteranExpireTime")),
            "veteranLeaveDaysTag": b.get("veteranLeaveDaysTag"),
            "preVeteranType": raw.get("preVeteranType"),
        },
        "meta": {
            "requestedUid": str(uid),
            "requestedRegion": region,
            "returnedSections": sorted(raw.keys()),
            "note": "Only fields returned by the game server are included; unavailable data is omitted or marked unavailable.",
        },
    }
    return _drop_none(data)

async def fetch_player_info(uid: str, region: str) -> Dict[str, Any]:
    region = region.upper()
    if region not in SUPPORTED_REGIONS:
        raise ValueError(f"Unsupported region: {region}")
    try:
        raw = await _call_player_api(int(uid), region)
    except httpx.HTTPStatusError as exc:
        # A token can expire or be revoked upstream before our local TTL.
        if exc.response.status_code not in (401, 403):
            raise
        with token_lock:
            cached_tokens.pop(region, None)
        raw = await _call_player_api(int(uid), region)
    return _extract_all(raw, int(uid), region)

# ─────────────────────────────────────────────────────────────────────────────
#  Rate limiting
# ─────────────────────────────────────────────────────────────────────────────
def rate_limit(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ip  = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        ip  = ip.split(",")[0].strip()
        now = time.time()
        hits = rate_limit_store[ip]
        rate_limit_store[ip] = [t for t in hits if now - t < RATE_LIMIT_WINDOW]
        if len(rate_limit_store[ip]) >= RATE_LIMIT_MAX:
            retry = int(RATE_LIMIT_WINDOW - (now - rate_limit_store[ip][0]))
            return _err("RATE_LIMIT_EXCEEDED",
                        f"Too many requests. Retry after {retry}s.", 429,
                        {"retry_after": retry})
        rate_limit_store[ip].append(now)
        return fn(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────────────────────────────────────
#  Response builders
# ─────────────────────────────────────────────────────────────────────────────
CREDITS = {
    "author":    "Infinity Codex",
    "developer": "https://t.me/zerox6t9",
}

DEVELOPER_CREDIT = "@MT_0G"


def _with_developer_credit(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the game payload directly, retaining only the Dev credit.

    No status/api_version/timestamp/data wrapper is added to player lookups.
    Dev is kept as the final key to match the public response shape.
    """
    payload = dict(data)
    payload.pop("credits", None)
    payload["Dev"] = payload.get("Dev") or DEVELOPER_CREDIT
    return payload

def _ok(data: Any, cached: bool = False):
    return jsonify({
        "status":      "success",
        "api_version": API_VERSION,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "cached":      cached,
        "credits":     CREDITS,
        "data":        data,
    }), 200

def _err(code: str, msg: str, status: int, extra: dict = None):
    body = {
        "status":      "error",
        "api_version": API_VERSION,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "error":       {"code": code, "message": msg},
    }
    if extra:
        body["error"].update(extra)
    return jsonify(body), status

def _fmt_ttl(s: int) -> str:
    if s <= 0: return "expired"
    h, r = divmod(s, 3600); m, sec = divmod(r, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if sec or not parts: parts.append(f"{sec}s")
    return " ".join(parts)

# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({
        "status": "success",
        "api_version": API_VERSION,
        "mode": "json_only",
        "message": "Free Fire player information API. Submit a UID and region to receive the complete profile payload returned by the game server.",
        "endpoints": {
            "player_info": {
                "methods": ["GET", "POST"],
                "path": "/player-info",
                "query_example": "/player-info?uid=2769409057&region=BD",
                "json_example": {"uid": "2769409057", "region": "BD"},
                "returns": "The complete upstream AccountPersonalShow JSON directly in the success response, including basicInfo, profileInfo, socialInfo, clanBasicInfo, captainBasicInfo, petInfo, creditScoreInfo, news, historyEpInfo, equippedAch and all other fields known to the response schema.",
            },
            "info": {
                "methods": ["GET", "POST"],
                "path": "/info",
                "query_example": "/info?uid=1704140050",
                "default_region": "BD",
                "returns": "The same complete upstream game payload as /player-info; region defaults to BD when omitted.",
            },
            "token_status": {"method": "GET", "path": "/token-status"},
            "regions": {"method": "GET", "path": "/regions"},
            "refresh": {"methods": ["GET", "POST"], "path": "/refresh?region=BD"},
            "health": {"method": "GET", "path": "/health"},
        },
        "notes": [
            "Responses are JSON only; no HTML lookup interface is served.",
            "The success response body is the complete upstream payload directly, without a status/data/api_version overlay.",
            "Only the Dev developer credit is retained as an additional response key.",
            "Null, null-like, and N/A placeholder values are omitted; zero, false, empty arrays, and every value actually returned by the game server are preserved.",
            "The API cannot invent fields that the game server does not provide for the requested UID.",
        ],
    })

def _player_input(name: str) -> str:
    """Read a value from query string, form data, or a JSON body."""
    values = [request.args.get(name), request.form.get(name)]
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        values.append(body.get(name))
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


@app.route("/player-info", methods=["GET", "POST"])
@app.route("/info", methods=["GET", "POST"])
@rate_limit
def get_player_info():
    uid    = _player_input("uid")
    # /info mirrors the public lookup URL and defaults to Bangladesh when the
    # region is omitted. /player-info remains explicit for backwards
    # compatibility and continues to require region.
    region_input = _player_input("region")
    region = (region_input or ("BD" if request.path.rstrip("/") == "/info" else "")).upper()

    if not uid:
        return _err("MISSING_UID", "Query param 'uid' is required.", 400)
    if not uid.isdigit():
        return _err("INVALID_UID", "UID must be numeric.", 400)
    if not (5 <= len(uid) <= 12):
        return _err("INVALID_UID", "UID must be 5–12 digits.", 400)
    if not region:
        return _err("MISSING_REGION", "Query param 'region' is required for /player-info.", 400)
    if region not in SUPPORTED_REGIONS:
        return _err("INVALID_REGION", f"Region '{region}' not supported.", 400,
                    {"supported_regions": sorted(SUPPORTED_REGIONS)})

    cache_key = hashlib.md5(f"{uid}:{region}".encode()).hexdigest()
    if cache_key in response_cache:
        logger.info(f"[Cache HIT] uid={uid} region={region}")
        response = jsonify(_with_developer_credit(response_cache[cache_key]))
        response.headers["X-Cache"] = "HIT"
        return response, 200

    try:
        logger.info(f"[Fetch] uid={uid} region={region}")
        # Keep the protobuf-to-JSON structure from the game server intact:
        # basicInfo, profileInfo, clanBasicInfo, captainBasicInfo, petInfo,
        # socialInfo, diamondCostRes, creditScoreInfo, news, historyEpInfo,
        # equippedAch, and any other fields in the current response schema.
        data = _clean_upstream_payload(
            asyncio.run(_call_player_api(int(uid), region))
        )

        if not data.get("basicInfo"):
            return _err("PLAYER_NOT_FOUND",
                        f"Player UID {uid} not found in {region}.", 404)

        response_cache[cache_key] = data
        response = jsonify(_with_developer_credit(data))
        response.headers["X-Cache"] = "MISS"
        return response, 200

    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 400)
    except httpx.TimeoutException:
        return _err("TIMEOUT", "The game service timed out. Please try again shortly.", 504)
    except httpx.HTTPStatusError as e:
        upstream_status = e.response.status_code
        if upstream_status == 429:
            retry_after = _retry_after_seconds(
                e.response,
                UPSTREAM_BACKOFF_BASE * (UPSTREAM_MAX_RETRIES + 1),
            )
            response, status = _err(
                "UPSTREAM_RATE_LIMITED",
                "The game service is temporarily busy. Please try again in a few seconds.",
                503,
                {"retry_after": retry_after},
            )
            response.headers["Retry-After"] = str(retry_after)
            return response, status
        return _err(
            "UPSTREAM_ERROR",
            "The game service returned a temporary error. Please retry shortly.",
            502,
            {"upstream_status": upstream_status},
        )
    except httpx.RequestError:
        return _err(
            "UPSTREAM_UNAVAILABLE",
            "The game service could not be reached. Please retry shortly.",
            503,
        )
    except Exception as e:
        logger.exception(f"[Error] uid={uid} region={region} → {e}")
        return _err("INTERNAL_ERROR", "Unexpected error. Try again.", 500)


@app.route("/token-status")
def token_status():
    now = time.time()
    out = {}
    for reg in sorted(SUPPORTED_REGIONS):
        info = cached_tokens.get(reg)
        if info:
            left = max(0, int(info["expires_at"] - now))
            out[reg] = {
                "status":       "active" if left > 0 else "expired",
                "expires_in":   left,
                "expires_human": _fmt_ttl(left),
                "last_refreshed": info.get("refreshed"),
                "server_url":   info.get("server_url"),
            }
        else:
            out[reg] = {"status": "not_initialized"}
    active = sum(1 for v in out.values() if v.get("status") == "active")
    return jsonify({"summary": {"total": len(SUPPORTED_REGIONS),
                                "active": active,
                                "inactive": len(SUPPORTED_REGIONS) - active},
                    "regions": out})


@app.route("/refresh", methods=["GET", "POST"])
def refresh_tokens():
    region = request.args.get("region", "").strip().upper()
    try:
        if region:
            if region not in SUPPORTED_REGIONS:
                return _err("INVALID_REGION", f"Region '{region}' not supported.", 400)
            asyncio.run(_create_token(region))
            return jsonify({"status": "success", "message": f"Refreshed {region}."})
        asyncio.run(init_tokens())
        active = sum(1 for r in SUPPORTED_REGIONS if cached_tokens.get(r))
        return jsonify({"status": "success", "tokens_active": active})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            retry_after = _retry_after_seconds(e.response, UPSTREAM_BACKOFF_BASE)
            response, status = _err(
                "UPSTREAM_RATE_LIMITED",
                "The game service is temporarily busy. Please try again in a few seconds.",
                503,
                {"retry_after": retry_after},
            )
            response.headers["Retry-After"] = str(retry_after)
            return response, status
        return _err("REFRESH_FAILED", "Token refresh failed upstream. Please retry shortly.", 502)
    except httpx.RequestError:
        return _err("UPSTREAM_UNAVAILABLE", "The game service could not be reached. Please retry shortly.", 503)
    except Exception as e:
        return _err("REFRESH_FAILED", "Token refresh failed. Please retry shortly.", 500)


@app.route("/regions")
def list_regions():
    return jsonify({"total": len(SUPPORTED_REGIONS),
                    "regions": sorted(SUPPORTED_REGIONS)})


@app.route("/health")
def health():
    active = sum(1 for r in SUPPORTED_REGIONS if cached_tokens.get(r))
    return jsonify({
        "status": "healthy", "version": API_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tokens": {"active": active, "total": len(SUPPORTED_REGIONS)},
        "cache":  {"size": len(response_cache), "maxsize": response_cache.maxsize, "ttl": CACHE_TTL},
        "rate_limit": {"max": RATE_LIMIT_MAX, "window_s": RATE_LIMIT_WINDOW},
    })


@app.errorhandler(404)
def not_found(_):
    return _err("NOT_FOUND", "Endpoint not found.", 404)

@app.errorhandler(405)
def method_not_allowed(_):
    return _err("METHOD_NOT_ALLOWED", "Method not allowed.", 405)

@app.errorhandler(500)
def server_error(_):
    return _err("INTERNAL_ERROR", "Internal server error.", 500)


# ─────────────────────────────────────────────────────────────────────────────
#  App factory + startup
# ─────────────────────────────────────────────────────────────────────────────
def create_app() -> Flask:
    app._start_time = datetime.now(timezone.utc).isoformat()
    return app


if __name__ == "__main__":
    asyncio.run(init_tokens())
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    logger.info(f"[Server] FF Info API v{API_VERSION} → port {port}")
    create_app().run(host="0.0.0.0", port=port, debug=debug)
