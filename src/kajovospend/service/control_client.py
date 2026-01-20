from __future__ import annotations

import json
import socket
from typing import Any, Dict


def send_cmd(host: str, port: int, cmd: str, timeout: float = 2.0) -> Dict[str, Any]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall((json.dumps({"cmd": cmd}) + "\n").encode("utf-8"))
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        line = data.split(b"\n", 1)[0].decode("utf-8", errors="ignore")
        try:
            return json.loads(line)
        except Exception:
            return {"ok": False, "error": "invalid_response", "raw": line}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            s.close()
        except Exception:
            pass
