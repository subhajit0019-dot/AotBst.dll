# MeowGG Player Device ID Capture, Ban & Termination System

Ek comprehensive system jo har player (**Bypass Player** ho ya **Normal Player**) ka unique **Device ID (Hardware ID)** automatically capture karke backend me bhejta hai, aur **Admin Panel** se aap kisi bhi Device ID ko **Ban** ya **Terminate** kar sakte hain.

---

## 🚀 System Architecture Overview

```
 [ Bypass Player / Normal Player / Web Client ]
                     │
       1. Captures Hardware Device ID
       2. Sends Heartbeat to Backend
                     ▼
       POST /api/device/report
                     │
                     ▼
        [ MeowGG Backend Server ]
        - Checks Device Status in Database
        - If Status == 'active'     -> Returns 200 (Allowed)
        - If Status == 'banned'     -> Returns 403 (Permanent Lockout)
        - If Status == 'terminated' -> Returns 403 (Kill Signal to Client)
                     ▲
                     │
         [ Admin Panel (/admin) ]
         - Real-time Device ID Monitoring
         - 🚫 Ban Device ID Button
         - ⚡ Terminate Session Button
         - 🟢 Unban / Restore Button
```

---

## 🛠️ 1. Admin Panel Usage (`/admin`)

1. Login to the admin control panel:
   - URL: `http://localhost:5000/admin` (or your deployed Vercel domain `/admin`)
   - **Username:** `72`
   - **Password:** `12`
2. **Device Security & Ban Manager Tab:**
   - **Tracked Device IDs:** Shows Total Devices, Active, Banned, and Terminated.
   - **Client Breakdown:** Real-time ratio of **Bypass Players** vs **Normal Players**.
   - **Online Pulse:** Displays a green glowing dot next to any device active in the last 5 minutes.
   - **Actions:**
     - **🚫 Ban:** Permanently bans the hardware ID with a custom violation reason.
     - **⚡ Terminate:** Sends a kill signal to the player's client to force-close their game/session.
     - **🟢 Unban / Restore:** Reactivates a banned/terminated device back to active status.
     - **🗑️ Delete:** Deletes the device record from the database.
     - **➕ Add / Pre-Ban Device:** Pre-emptively add a device ID to ban before the player even connects.

---

## 💻 2. Player Client Integration (`client_agent.py`)

A complete client script [`client_agent.py`](file:///c:/Users/subha/Downloads/NOVA_X_UID_BYPASS_WHITLISTZss/client_agent.py) is included in the project root.

### Running for a Normal Player:
```bash
python client_agent.py --type normal
```

### Running for a Bypass Player:
```bash
python client_agent.py --type bypass 12345678
```

### How the Client Enforces Directives:
- **`allow`**: The player continues playing normally.
- **`terminate`**: The client immediately detects the admin termination directive, displays an alert, and closes the application process (`os._exit(1)`).
- **`ban`**: The client displays a permanent hardware ban warning, revokes all access, and exits (`os._exit(2)`).

---

## 📱 3. Integration Code Snippets

### A. Android / Java Integration (For Android APKs / Injectors)
```java
// 1. Capture Android Device ID
String deviceId = Settings.Secure.getString(
    context.getContentResolver(), 
    Settings.Secure.ANDROID_ID
);

// 2. Send Heartbeat to Backend
JSONObject payload = new JSONObject();
payload.put("device_id", deviceId);
payload.put("player_uid", inGameUid);
payload.put("player_type", isBypass ? "bypass" : "normal");

// 3. Handle Admin Directives
// If response returns: action == "terminate" -> finishAffinity() / System.exit(0);
// If response returns: action == "ban"       -> showBanDialog(); System.exit(0);
```

### B. C++ / Native Game Client Snippet
```cpp
// Send HTTP POST to /api/device/report
// Request payload:
// {"device_id": hwid, "player_type": "bypass", "player_uid": "5182947291"}

if (jsonResponse["action"] == "terminate") {
    MessageBoxA(NULL, "Session terminated by Administrator.", "MeowGG Security", MB_ICONWARNING);
    ExitProcess(0); // Force-close game
} else if (jsonResponse["action"] == "ban") {
    MessageBoxA(NULL, "This Device ID is permanently banned.", "MeowGG Security", MB_ICONERROR);
    ExitProcess(0);
}
```

### C. cURL / Shell Test
```bash
# Report player device:
curl -X POST http://localhost:5000/api/device/report \
  -H "Content-Type: application/json" \
  -d '{"device_id":"HWID-9921-FF88-1234ABCD","player_type":"bypass","player_uid":"5182947291"}'
```

---

## 🌐 4. Web Portal Integration
The web whitelist portal (`/`) also automatically fingerprints every browser visitor. If an administrator bans their Device ID from the Admin Panel, they are immediately blocked from whitelisting any UID on the website.
