#!/usr/bin/env python3
"""First-run setup wizard for meeting-memory.

Handles: venv creation, Composio API key validation, Gmail address,
LLM backend selection + validation, Gmail OAuth, and .env writing.

Usage:
    python setup.py           # interactive wizard
    python setup.py --force   # re-run even if .env already configured
    python setup.py --reinstall  # recreate .venv before running
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VENV_PYTHON = VENV / "bin" / "python"
ENV_FILE = HERE / ".env"
ENV_EXAMPLE = HERE / ".env.example"
REQUIREMENTS = HERE / "requirements.txt"


# ── helpers ──────────────────────────────────────────────────────────────────


def _rule(label: str) -> None:
    width = 60
    pad = max(0, width - len(label) - 4)
    print(f"\n── {label} {'─' * pad}")


def _ok(msg: str) -> None:
    print(f"  ✓  {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠  {msg}", file=sys.stderr)


def _err(msg: str) -> None:
    print(f"  ✗  {msg}", file=sys.stderr)


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {msg}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val or default


def read_env(path: Path) -> dict[str, str]:
    """Parse key=value pairs from an env file, ignoring comments."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def write_env(path: Path, new_values: dict[str, str]) -> None:
    """Merge new_values into path, preserving structure from .env.example."""
    existing = read_env(path)
    merged = {**existing, **new_values}

    lines: list[str] = []
    written: set[str] = set()

    if ENV_EXAMPLE.exists():
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in merged:
                    lines.append(f"{k}={merged[k]}")
                    written.add(k)
                else:
                    lines.append(line)
            else:
                lines.append(line)

    # Append any keys not present in .env.example
    extras = {k: v for k, v in merged.items() if k not in written}
    if extras:
        lines.append("")
        lines.append("# ── Additional keys ─────────────────────────────────")
        for k, v in extras.items():
            lines.append(f"{k}={v}")

    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── Step 1: Prerequisites ─────────────────────────────────────────────────────


def step_prerequisites(force_reinstall: bool) -> None:
    _rule("Step 1 — Prerequisites")

    if sys.version_info < (3, 9):
        _err(f"Python 3.9+ required. You have {sys.version.split()[0]}.")
        sys.exit(1)
    _ok(f"Python {sys.version.split()[0]}")

    if VENV.exists() and not force_reinstall:
        _ok(".venv exists — skipping dependency install")
        return

    print("  Creating virtual environment …")
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)

    pip = VENV / "bin" / "pip"
    print("  Installing dependencies (this takes ~30 seconds) …")
    subprocess.run(
        [str(pip), "install", "-q", "-r", str(REQUIREMENTS)],
        check=True,
    )
    _ok("Dependencies installed")


# ── Step 2: Composio API key ──────────────────────────────────────────────────


def step_composio_key(existing: dict[str, str]) -> str:
    _rule("Step 2 — Composio API Key")

    import httpx  # available after step_prerequisites

    current = existing.get("COMPOSIO_API_KEY", "")
    if current:
        print(f"  Found existing key: {current[:8]}…")
        ans = _prompt("Use this key? [Y/n]", default="Y").upper()
        if ans != "N":
            _ok("Using existing key")
            return current

    print("  Get a free key at: https://app.composio.dev → Settings → API Keys")

    for attempt in range(1, 4):
        key = _prompt(f"Composio API key (attempt {attempt}/3)")
        if not key:
            continue
        try:
            r = httpx.get(
                "https://backend.composio.dev/api/v3.1/connected_accounts",
                headers={"x-api-key": key},
                timeout=10,
            )
            if r.status_code < 400:
                _ok("API key valid")
                return key
            _warn(f"Key rejected (HTTP {r.status_code})")
        except httpx.RequestError as exc:
            _warn(f"Network error: {exc}")

    _err("Could not validate Composio key after 3 attempts.")
    _err("Check your key at https://app.composio.dev and re-run setup.")
    sys.exit(1)


# ── Step 3: Gmail address ─────────────────────────────────────────────────────


def step_gmail(existing: dict[str, str]) -> str:
    _rule("Step 3 — Gmail Address")

    current = existing.get("GMAIL_USER", "")
    placeholder = "you@example.com"
    if current and current != placeholder:
        print(f"  Found existing Gmail: {current}")
        ans = _prompt("Use this address? [Y/n]", default="Y").upper()
        if ans != "N":
            _ok(f"Gmail: {current}")
            return current

    for _ in range(3):
        email = _prompt("Gmail address to connect").lower()
        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            _ok(f"Gmail: {email}")
            return email
        _warn("Invalid email format — try again")

    _err("Could not get a valid Gmail address.")
    sys.exit(1)


# ── Step 4: LLM backend ───────────────────────────────────────────────────────


def step_llm(existing: dict[str, str]) -> dict[str, str]:
    _rule("Step 4 — LLM Backend")
    print("  [A] Anthropic Claude  — cloud, highest quality")
    print("  [O] Ollama            — fully local, private")
    choice = _prompt("Choice [A/O]", default="A").upper()
    if choice == "O":
        return _llm_ollama(existing)
    return _llm_anthropic(existing)


def _llm_anthropic(existing: dict[str, str]) -> dict[str, str]:
    import httpx

    current = existing.get("ANTHROPIC_API_KEY", "")
    if current:
        print(f"  Found existing Anthropic key: {current[:8]}…")
        ans = _prompt("Use this key? [Y/n]", default="Y").upper()
        if ans != "N":
            _ok("Using existing Anthropic key")
            return {
                "LLM_BACKEND": "anthropic",
                "ANTHROPIC_API_KEY": current,
                "ANTHROPIC_MODEL": existing.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            }

    print("  Get a key at: https://console.anthropic.com")

    for attempt in range(1, 4):
        key = _prompt(f"Anthropic API key (attempt {attempt}/3)")
        if not key:
            continue
        try:
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=15,
            )
            if r.status_code < 400:
                _ok("Anthropic key valid")
                return {
                    "LLM_BACKEND": "anthropic",
                    "ANTHROPIC_API_KEY": key,
                    "ANTHROPIC_MODEL": existing.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                }
            _warn(f"Key rejected (HTTP {r.status_code}): {r.text[:120]}")
        except httpx.RequestError as exc:
            _warn(f"Network error: {exc}")

    _err("Could not validate Anthropic key after 3 attempts.")
    sys.exit(1)


def _llm_ollama(existing: dict[str, str]) -> dict[str, str]:
    import httpx

    model = existing.get("OLLAMA_MODEL") or "qwen2.5:7b"
    host = existing.get("OLLAMA_HOST") or "http://localhost:11434"

    local_models: list[str] = []

    for attempt in range(1, 4):
        try:
            r = httpx.get(f"{host}/api/tags", timeout=5)
            if r.status_code == 200:
                local_models = [m["name"] for m in r.json().get("models", [])]
                _ok(f"Ollama running at {host}")
                break
        except httpx.RequestError:
            pass
        _warn(f"Ollama not reachable at {host} (attempt {attempt}/3)")
        if attempt < 3:
            input(f"  Start Ollama with: ollama serve\n  Then press Enter to retry… ")
    else:
        _err("Could not reach Ollama after 3 attempts.")
        _err("Start it with: ollama serve")
        sys.exit(1)

    if model not in local_models:
        print(f"  Model '{model}' not found locally. Available: {local_models or ['(none)']}")
        ans = _prompt(f"Pull '{model}' now? (~4.7 GB) [Y/n]", default="Y").upper()
        if ans != "N":
            print(f"  Pulling {model} — this may take a few minutes …")
            subprocess.run(["ollama", "pull", model], check=True)
            _ok(f"{model} ready")
        else:
            _warn(f"Skipped pull. Run 'ollama pull {model}' before using the pipeline.")
    else:
        _ok(f"{model} already available")

    return {"LLM_BACKEND": "ollama", "OLLAMA_MODEL": model, "OLLAMA_HOST": host}


# ── Step 5: Gmail OAuth ───────────────────────────────────────────────────────


def step_gmail_oauth(api_key: str, existing: dict[str, str]) -> str:
    """Initiate OAuth, open browser, poll until ACTIVE. Returns entity_id."""
    _rule("Step 5 — Gmail OAuth")

    import httpx

    # Check if already connected
    try:
        r = httpx.get(
            "https://backend.composio.dev/api/v3.1/connected_accounts",
            headers={"x-api-key": api_key},
            params={"toolkit_slugs": "gmail", "statuses": "ACTIVE"},
            timeout=10,
        )
        items = r.json().get("items") or []
        if items:
            _ok(f"Gmail already connected (id: {items[0].get('id','?')})")
            return existing.get("COMPOSIO_ENTITY_ID", "default")
    except httpx.RequestError:
        pass

    # Bootstrap lib path so we can use ComposioGmail
    sys.path.insert(0, str(HERE))
    os.environ["COMPOSIO_API_KEY"] = api_key
    from lib.composio_gmail import ComposioGmail  # noqa: E402

    gmail = ComposioGmail(api_key=api_key)

    try:
        result = gmail.initiate_gmail_oauth()
    except Exception as exc:
        _err(f"Could not start OAuth flow: {exc}")
        sys.exit(1)

    # Composio returns camelCase or snake_case depending on version
    redirect_url = (
        result.get("redirectUrl")
        or result.get("redirect_url")
        or result.get("url")
        or ""
    )
    connected_account_id = (
        result.get("connectedAccountId")
        or result.get("connected_account_id")
        or ""
    )

    if not redirect_url:
        _err(f"Unexpected OAuth response (no redirect URL): {result}")
        sys.exit(1)

    print(f"\n  Opening browser for Gmail authorization …")
    print(f"  If the browser doesn't open, visit:\n  {redirect_url}\n")
    webbrowser.open(redirect_url)

    print("  Waiting for authorization", end="", flush=True)
    deadline = time.time() + 180  # 3 minutes

    while time.time() < deadline:
        time.sleep(3)
        print(".", end="", flush=True)
        try:
            if connected_account_id:
                status_data = gmail.get_connection_status(connected_account_id)
                status = (
                    status_data.get("status")
                    or status_data.get("connectionStatus")
                    or ""
                ).upper()
            else:
                # Fall back to listing active connections
                r2 = httpx.get(
                    "https://backend.composio.dev/api/v3.1/connected_accounts",
                    headers={"x-api-key": api_key},
                    params={"toolkit_slugs": "gmail", "statuses": "ACTIVE"},
                    timeout=10,
                )
                status = "ACTIVE" if (r2.json().get("items") or []) else "PENDING"

            if status == "ACTIVE":
                print()
                _ok("Gmail connected ✓")
                return existing.get("COMPOSIO_ENTITY_ID", "default")
        except Exception:
            pass  # transient; keep polling

    print()
    _err("Timed out waiting for Gmail authorization (3 minutes).")
    print(f"\n  Authorize at:\n  {redirect_url}")
    print("\n  Then re-run: python setup.py --force")
    sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="First-run setup wizard for meeting-memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-run wizard even if .env already exists",
    )
    ap.add_argument(
        "--reinstall",
        action="store_true",
        help="Recreate .venv before running (slow)",
    )
    args = ap.parse_args()

    print("\n  meeting-memory setup wizard")
    print("  ─" * 30)

    existing = read_env(ENV_FILE)

    # Short-circuit if already configured
    if existing.get("COMPOSIO_API_KEY") and not args.force:
        print("\n  Looks like setup was already run.")
        ans = _prompt("Re-run setup? [y/N]", default="N").upper()
        if ans != "Y":
            print("  Nothing changed. Use --force to override.")
            return 0

    step_prerequisites(force_reinstall=args.reinstall)

    composio_key = step_composio_key(existing)
    gmail_user = step_gmail(existing)
    llm_config = step_llm(existing)
    entity_id = step_gmail_oauth(composio_key, existing)

    env_values: dict[str, str] = {
        "COMPOSIO_API_KEY": composio_key,
        "COMPOSIO_ENTITY_ID": entity_id,
        "COMPOSIO_BASE_URL": "https://backend.composio.dev",
        "GMAIL_USER": gmail_user,
        **llm_config,
    }

    _rule("Step 6 — Writing .env")
    write_env(ENV_FILE, env_values)
    _ok(f".env written to {ENV_FILE}")

    print("\n" + "─" * 62)
    print("  Setup complete! Next steps:\n")
    print("    make backfill      # catch up on last 12 months of meetings")
    print("    make run           # one-off run (last hour)")
    print("    make start         # install background daemon (macOS)")
    print()
    print("  Open vault/ in Obsidian and enable the Dataview plugin.")
    print("  Start at: vault/_Dashboards/00 - Home.md")
    print("─" * 62 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
