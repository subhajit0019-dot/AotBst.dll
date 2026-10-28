"""
MeowGG Universal Player Client Agent
====================================
Cross-platform device identifier capture and real-time security enforcement agent.
Supports both Bypass and Normal players.
Automatically reports device ID, player UID, and client type to the backend.
Listens for Admin Ban and Session Termination directives in real-time.
"""

import os
import sys
import time
import json
import uuid
import platform
import hashlib
import subprocess
import urllib.request
import urllib.error

# ==================== CONFIGURATION ====================
DEFAULT_BACKEND_URL = os.getenv("MEOW_BACKEND_URL", "http://localhost:5000")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "15"))  # seconds

def get_hardware_device_id() -> str:
    """
    Generate a deterministic, unique hardware Device ID for this machine.
    Uses OS machine-guid, motherboard/disk UUID, or network MAC address.
    """
    raw_components = []

    # 1. Platform & OS
    raw_components.append(platform.system())
    raw_components.append(platform.machine())

    # 2. Windows MachineGuid
    if platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                if guid:
                    raw_components.append(str(guid).strip())
        except Exception:
            pass

    # 3. Linux/Android machine-id
    elif platform.system() == "Linux":
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id", "/sys/class/android_usb/android0/iSerial"):
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        content = f.read().strip()
                        if content:
                            raw_components.append(content)
                            break
                except Exception:
                    pass

    # 4. Fallback: Network Node MAC Address
    try:
        mac = uuid.getnode()
        if mac and mac != uuid.getnode():  # check if random
            raw_components.append(str(mac))
        else:
            raw_components.append(str(mac))
    except Exception:
        pass

    # Hash components to produce a clean 32-character hex identifier
    combined = ":".join(raw_components)
    dev_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:24].upper()
    return f"HWID-{dev_hash[:4]}-{dev_hash[4:8]}-{dev_hash[8:16]}-{dev_hash[16:24]}"

def send_heartbeat(backend_url: str, device_id: str, player_uid: str, player_name: str, player_type: str) -> dict:
    """Send heartbeat payload to backend endpoint."""
    endpoint = f"{backend_url.rstrip('/')}/api/device/report"
    payload = {
        "device_id": device_id,
        "player_uid": player_uid,
        "player_name": player_name,
        "player_type": player_type,
        "app_version": "2.4.0",
        "os_info": f"{platform.system()} {platform.release()}"
    }

    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"MeowGG-Agent/2.4 ({platform.system()}; {player_type})",
            "X-Device-ID": device_id
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"allowed": False, "status": "error", "message": f"HTTP {e.code}"}
    except Exception as e:
        return {"allowed": False, "status": "offline", "message": str(e)}

def execute_terminate_directive(reason: str):
    """Forcefully terminate client session upon admin signal."""
    print("\n" + "=" * 65)
    print(" [!] EMERGENCY DIRECTIVE: SESSION TERMINATED BY ADMINISTRATOR")
    print(f" [!] Reason: {reason}")
    print(" [!] Closing client process immediately...")
    print("=" * 65 + "\n")
    time.sleep(1)
    os._exit(1)

def execute_ban_directive(reason: str):
    """Enforce permanent ban on this hardware device."""
    print("\n" + "=" * 65)
    print(" [CRITICAL] ACCESS DENIED: THIS HARDWARE DEVICE HAS BEEN BANNED")
    print(f" [CRITICAL] Enforcement Reason: {reason}")
    print(" [CRITICAL] All whitelist claims and game access are permanently revoked.")
    print("=" * 65 + "\n")
    time.sleep(2)
    os._exit(2)

def main():
    print("=" * 65)
    print("       MeowGG Security & Device Identification Agent")
    print("=" * 65)

    backend_url = os.getenv("MEOW_BACKEND_URL", DEFAULT_BACKEND_URL)
    device_id = get_hardware_device_id()

    print(f"[*] Detected Hardware Device ID: {device_id}")
    print(f"[*] Backend Server: {backend_url}")

    # Interactive or automated player input
    default_type = "bypass" if "bypass" in sys.argv else "normal"
    player_type = os.getenv("PLAYER_TYPE", default_type)
    player_uid = os.getenv("PLAYER_UID", "")
    player_name = os.getenv("PLAYER_NAME", "")

    if not player_uid and len(sys.argv) > 1 and sys.argv[1].isdigit():
        player_uid = sys.argv[1]

    print(f"[*] Player Classification: {player_type.upper()}")
    if player_uid:
        print(f"[*] Associated UID: {player_uid}")

    print("[*] Initiating handshake with backend...")
    
    # Initial verification handshake
    resp = send_heartbeat(backend_url, device_id, player_uid, player_name, player_type)

    if resp.get("status") == "banned":
        execute_ban_directive(resp.get("reason") or resp.get("message"))

    if resp.get("status") == "terminated":
        execute_terminate_directive(resp.get("reason") or resp.get("message"))

    if resp.get("allowed") or resp.get("success"):
        print(f"[+] Device ID Registered & Verified! Status: ACTIVE")
    else:
        print(f"[-] Server note: {resp.get('message', 'Unconfirmed')}")

    print(f"[*] Listening for Admin Directives (Heartbeat: every {HEARTBEAT_INTERVAL}s)...\n")

    while True:
        try:
            time.sleep(HEARTBEAT_INTERVAL)
            hb = send_heartbeat(backend_url, device_id, player_uid, player_name, player_type)

            action = hb.get("action", "")
            status = hb.get("status", "")

            if action == "ban" or status == "banned":
                execute_ban_directive(hb.get("reason") or hb.get("message"))

            if action == "terminate" or status == "terminated":
                execute_terminate_directive(hb.get("reason") or hb.get("message"))

            if hb.get("allowed"):
                print(f"[{time.strftime('%H:%M:%S')}] Heartbeat OK | Device: {device_id} | Status: ACTIVE")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Server response: {hb.get('message')}")

        except KeyboardInterrupt:
            print("\n[*] Agent stopped by user.")
            break
        except Exception as e:
            print(f"[!] Heartbeat warning: {e}")

if __name__ == "__main__":
    main()
