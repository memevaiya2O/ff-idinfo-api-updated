# 🎮 Free Fire Player Info API

**Version:** 3.0.0 | **Game Version:** OB54

A full-featured REST API to fetch detailed Free Fire player profiles using the game's own protocol (Protobuf + AES). No third-party scraping — direct game server communication.

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET/POST` | `/player-info?uid=&region=` | Complete upstream player JSON profile |
| `GET/POST` | `/info?uid=&region=` | Same complete profile; region defaults to `BD` |
| `GET` | `/token-status` | Token health for all regions |
| `GET` | `/regions` | List all supported regions |
| `GET/POST` | `/refresh?region=` | Force token refresh |
| `GET` | `/health` | API health check |

---

## 🔍 Usage Example

The endpoint accepts either query parameters or a JSON/form body. `/info` defaults to the BD region when no region is supplied. The service validates the UID and region before contacting the game servers.

```http
GET /info?uid=1704140050
```

```http
POST /player-info
Content-Type: application/json

{"uid":"1704140050","region":"BD"}
```

If the upstream game service is temporarily busy, the API retries transient requests with exponential backoff and returns a friendly `503` response with `Retry-After` instead of exposing the raw upstream `429` message. Repeated refresh requests should be avoided because the upstream service may still enforce its own limits.

---

## 🌍 Supported Regions

`IND` `BR` `US` `SAC` `NA` `SG` `RU` `ID` `TW` `VN` `TH` `ME` `PK` `CIS` `BD` `EUROPE`

---

## 📦 Player Info Output Fields

The API returns JSON only. A successful `/player-info` response is the complete upstream player payload itself, without a `data` wrapper, normalization, renaming, omission, or duplication. This includes sections such as `basicInfo`, `profileInfo`, `clanBasicInfo`, `captainBasicInfo`, `petInfo`, `socialInfo`, `diamondCostRes`, `creditScoreInfo`, `news`, `historyEpInfo`, `equippedAch`, and any additional fields known by the response schema.

For a player who belongs to a guild, the game server may return the guild identity in `clanBasicInfo` and the owner/captain profile in `captainBasicInfo`. If a field is not returned by the upstream service for a UID, this API does not fabricate it.

### Complete Upstream Payload

The response keeps the original upstream field names and structure. For example, the player ID remains under `basicInfo.accountId`, while the guild owner remains under `captainBasicInfo`. Null, `null`, and `N/A` placeholders are omitted, while `0`, `false`, empty arrays, and all fields actually returned by the game server are preserved. The API never fabricates a value for data the game server did not send.
| Field | Description |
|-------|-------------|
| `uid` | Player UID |
| `accountId` | Internal account ID |
| `nickname` | Player nickname/name |
| `region` | Server region (e.g. BD, IND) |
| `level` | Account level |
| `exp` | Total EXP |
| `accountType` | Account type ID |
| `externalId` | Linked platform ID (Facebook/Google etc.) |
| `externalType` | Platform type (1=FB, 2=Google, 4=Guest, etc.) |
| `externalName` | Linked platform display name |
| `externalUid` | Linked platform UID |
| `releaseVersion` | Game version at last login (e.g. OB54) |
| `createAt` | Account creation date |
| `lastLoginAt` | Last login timestamp |
| `returnAt` | Return/reactivation date |

---

### `appearance` — Profile Cosmetics
| Field | Description |
|-------|-------------|
| `headPic` | Profile picture/avatar ID |
| `bannerId` | Profile banner ID |
| `badgeId` | Equipped badge ID |
| `badgeCount` | Total badge count |
| `pinId` | Pinned item ID |
| `title` | Equipped title ID |
| `gameBagShow` | Bag skin shown |
| `selectedItemSlots` | List of equipped item slot IDs |
| `weaponSkinShows` | List of shown weapon skin IDs |
| `externalIcon` | External icon URL/ID |
| `externalIconInfo` | Object: `status`, `showType` |

---

### `avatar` — Character / Hero
| Field | Description |
|-------|-------------|
| `avatarId` | Selected character/hero ID |
| `skinColor` | Character skin color ID |
| `clothes` | List of equipped clothing item IDs |
| `equipedSkills` | List of equipped skill IDs |
| `pve_primary_weapon` | PvE primary weapon ID |
| `isSelectedAwaken` | Whether awakened form is selected |
| `unlockType` | Character unlock type |
| `clothesTailorEffects` | Tailor/custom effects on clothes |

---

### `rank` — Competitive Ranking
| Field | Description |
|-------|-------------|
| `brRank` | Battle Royale rank tier ID |
| `brRankingPoints` | BR ranking points |
| `brMaxRank` | Highest BR rank achieved |
| `brMaxRankingPoints` | Highest BR points |
| `brPeakRankPos` | BR peak leaderboard position |
| `brPeriodicRank` | BR periodic/season rank |
| `brPeriodicPoints` | BR periodic ranking points |
| `csRank` | Clash Squad rank tier ID |
| `csRankingPoints` | CS ranking points |
| `csMaxRank` | Highest CS rank achieved |
| `csPeakRankPos` | CS peak leaderboard position |
| `isCsRankingBan` | Whether CS rank is banned |
| `showBrRank` | Whether BR rank is public |
| `showCsRank` | Whether CS rank is public |
| `rankLeaderboardPos` | Overall leaderboard position |

---

### `social` — Social Profile
| Field | Description |
|-------|-------------|
| `bio` | Player bio / signature |
| `gender` | Gender setting |
| `language` | Preferred language |
| `liked` | Total likes received |
| `hasElitePass` | Whether player has Elite Pass |
| `seasonId` | Current season ID |
| `role` | Player role ID |
| `timeOnline` | Online time preference |
| `timeActive` | Active time preference |
| `modePrefer` | Preferred game mode |
| `rankShow` | Which rank is shown publicly |
| `battleTags` | List of battle tag IDs |
| `socialTags` | List of social tag IDs |
| `socialHighlights` | List of highlight achievements |
| `leaderboardTitles` | Leaderboard title info |

---

### `guild` — Guild / Clan (nullable)
| Field | Description |
|-------|-------------|
| `clanId` | Guild ID |
| `clanName` | Guild name |
| `clanLevel` | Guild level |
| `captainId` | Guild captain's UID |
| `memberNum` | Current member count |
| `capacity` | Max member capacity |
| `honorPoint` | Guild honor points |
| `clanBadgeId` | Guild badge ID |
| `clanFrameId` | Guild frame ID |
| `customBadge` | Custom badge URL/ID |
| `captain` | Object: `accountId`, `nickname`, `level`, `headPic` |

---

### `pet` — Pet Info (nullable)
| Field | Description |
|-------|-------------|
| `id` | Pet ID |
| `name` | Pet name |
| `level` | Pet level |
| `exp` | Pet EXP |
| `skinId` | Equipped pet skin ID |
| `selectedSkill` | Active skill ID |
| `skills` | List: `{skillId, skillLevel}` |
| `isMarkedStar` | Whether pet is starred |

---

### `elitePass` — Elite Pass History
List of objects:
| Field | Description |
|-------|-------------|
| `eventId` | EP event ID |
| `name` | Event name |
| `ownedPass` | Whether pass was purchased |
| `badge` | EP badge ID |
| `badgeCnt` | Badge count |
| `icon` | EP icon URL |
| `maxLevel` | Max level reached |

---

### `creditScore` — Credit Score (nullable)
| Field | Description |
|-------|-------------|
| `creditScore` | Credit score value |
| `rewardState` | Reward claim state |
| `weeklyMatches` | Weekly match count |
| `periodLikes` | Period like count |
| `periodIllegal` | Period illegal action count |

---

### `membership` — Subscription Status
| Field | Description |
|-------|-------------|
| `hasElitePass` | Has active Elite Pass |
| `membershipState` | Membership state flag |
| `veteranExpireAt` | Veteran status expiry date |
| `preVeteranType` | Pre-veteran action type |

---

### `achievements` — Equipped Achievements
List of `{achId, level}` objects.

---

### `recentNews` — Recent Activity
List of recent in-game activities:
| Field | Description |
|-------|-------------|
| `type` | News type (rank, lottery, purchase, etc.) |
| `updateTime` | When the activity happened |
| `content` | Activity content details |

---

### `preferences` — Privacy Settings
| Field | Description |
|-------|-------------|
| `hideLobby` | Lobby hidden from others |
| `hidePersonalInfo` | Personal info hidden |
| `disableFriendSpectate` | Friend spectate disabled |
| `hideOccupation` | Occupation hidden |

---

### `occupations` — Season Occupation Info
List of `{seasonId, gameMode, info}` objects showing occupation roles per season.

---

### `championship` — Championship Team
| Field | Description |
|-------|-------------|
| `teamName` | Championship team name |
| `teamId` | Championship team ID |
| `teamMemberNum` | Number of team members |

---

## 📋 Sample Response Structure

```json
{
  "status": "success",
  "api_version": "3.0.0",
  "timestamp": "2025-01-01T00:00:00+00:00",
  "cached": false,
  "credits": {
    "author": "Infinity Codex",
    "developer": "https://t.me/zerox6t9"
  },
  "data": {
    "basic": { "uid": "2769409057", "nickname": "PlayerName", "level": 75, ... },
    "appearance": { ... },
    "avatar": { ... },
    "rank": { "brRank": 7, "brRankingPoints": 3200, ... },
    "social": { "bio": "Hello World", "liked": 152, ... },
    "guild": { "clanName": "MyGuild", "memberNum": 46, ... },
    "pet": { "name": "Dreki", "level": 7, ... },
    "elitePass": [ ... ],
    "creditScore": { "creditScore": 100, ... },
    "membership": { "hasElitePass": true, ... },
    "achievements": [ ... ],
    "recentNews": [ ... ],
    "preferences": { ... },
    "occupations": [ ... ],
    "championship": { ... }
  }
}
```

---

## JSON-only Responses

The root endpoint `/` also returns JSON documentation. There is no HTML lookup interface. Use either of the following forms:

```http
GET /player-info?uid=1704140050&region=BD
```

```http
POST /player-info
Content-Type: application/json

{"uid":"1704140050","region":"BD"}
```

## ⚙️ Deploy on Vercel

```bash
# Install Vercel CLI
npm i -g vercel

# Deploy
vercel --prod
```

The `vercel.json` is already configured for Python 3.11.

## 🖥️ Run Locally

```bash
pip install -r requirements.txt
python app.py
# Server starts on port 5000
```

Or with gunicorn:
```bash
gunicorn wsgi:app --workers 4 --bind 0.0.0.0:5000
```

---

## 📁 Project Structure

```
ff-idinfo-api/
├── app.py              # Main Flask API (all routes + logic)
├── index.py            # Vercel WSGI entry point
├── wsgi.py             # Gunicorn WSGI entry point
├── vercel.json         # Vercel deployment config
├── requirements.txt    # Python dependencies
└── proto/
    ├── __init__.py
    ├── FreeFire_pb2.py          # Login request/response protobuf
    ├── main_pb2.py              # Player personal show protobuf
    └── AccountPersonalShow_pb2.py  # Full account info protobuf
```

---

## ⚠️ Rate Limits and Upstream Protection

- **100 requests** per IP per **100 seconds** for the public `/player-info` endpoint
- Cache TTL: **5 minutes** per UID+region combination
- Token TTL: **7 hours** with single-flight refresh per region
- Upstream calls are concurrency-limited and retry transient `429`/`5xx` responses with bounded exponential backoff
- The guest account can be overridden at deployment time with `FF_GUEST_UID` and `FF_GUEST_PASSWORD`; these values must be kept private and must never be logged

---

*Free Fire is a registered trademark of Garena. This project is for educational purposes only.*
