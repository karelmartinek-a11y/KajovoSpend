from __future__ import annotations

import os
from pathlib import Path
from typing import Dict
import sys
import re

try:
    import winreg
except Exception:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore

def load_dotenv(path: Path) -> Dict[str, str]:
    """
    Jednoduché načtení .env souboru do os.environ.
    Vrací dict načtených klíčů/hodnot.
    """
    loaded: Dict[str, str] = {}
    if not path.exists():
        return loaded
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            os.environ[k] = v
            loaded[k] = v
    except Exception:
        pass
    return loaded


def load_user_env_var(name: str) -> str | None:
    """
    Načte hodnotu z uživatelských proměnných Windows (HKCU\\Environment) a vrátí ji.
    Nezávisí na aktuální procesové env, takže funguje i bez nového přihlášení.
    """
    if sys.platform.startswith("win") and winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, name)
                if isinstance(val, str):
                    return val
        except FileNotFoundError:
            return None
        except Exception:
            return None
    # fallback: zkus procesové env
    return os.getenv(name)


def sanitize_openai_api_key(raw: str | None) -> str:
    """
    Vrati 'nejpravdepodobnejsi' OpenAI API key z libovolneho textu.

    Resi i pripady, kdy uzivatel omylem vlozi do pole/registru vic radku (napr. prompt z PowerShellu).
    """
    if raw is None:
        return ""
    s = str(raw)
    # nejdriv hrube osekat a odstranit bezne prefixy
    s = s.strip().strip("\"'").strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()

    # hledej token "sk-..." (vcetne sk-proj-...), delka min 20 znaku za sk-
    m = re.search(r"(sk-[A-Za-z0-9_-]{20,})", s)
    if m:
        return m.group(1)
    # fallback: vrat cely jeden radek bez whitespace, pokud vypada rozumne
    one = s.splitlines()[0].strip() if s else ""
    if one.startswith("sk-") and len(one) >= 24:
        return one
    return ""


def set_user_env_var(name: str, value: str) -> bool:
    """
    Uloží proměnnou do uživatelského prostředí Windows (HKCU\\Environment).
    Nevyžaduje restart aplikace pro aktuální proces (ten si nastavuje sám).
    Vrací True/False podle úspěchu.
    """
    if name == "KAJOVOSPEND_OPENAI_API_KEY":
        value = sanitize_openai_api_key(value) or ""

    ok = False
    if sys.platform.startswith("win") and winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                ok = True
        except FileNotFoundError:
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                    ok = True
            except Exception:
                ok = False
        except Exception:
            ok = False
    return ok
