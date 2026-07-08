#!/usr/bin/env python3
"""
carousel — unified multi-agent auth carousel.

Installs + configures Hermes & OpenCode with:
- caveman skill (both)
- 28+ API keys from ai/.env (auto-mirror)
- OAuth flows for openai-codex / qwen / nous (browser auto-open)
- nightly refresh cron
- status command

Usage:
  carousel install        # install both CLIs + skills + mirror keys
  carousel oauth          # interactive OAuth (browser pops)
  carousel refresh        # re-signup exhausted providers
  carousel status         # pooled creds + validity summary
  carousel nightly        # full rotation cycle
"""
import os
import sys
import json
import shutil
import subprocess
import argparse
import http.server
import socketserver
import threading
import urllib.parse
import webbrowser
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────
HOME = Path.home()
AI_DIR = Path("/media/gio/7d36e554-5f5a-4fe2-9fcc-8b3c53614168/ai")
AI_ENV = AI_DIR / ".env"
HERMES_HOME = HOME / ".hermes"
OPENCODE_HOME = HOME / ".config" / "opencode"
OPENCODE_AUTH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
ZSHRC = HOME / ".zshrc"

OAUTH_PROVIDERS = ["openai-codex", "qwen-oauth", "nous"]

# Mirror map: ai/.env PREFIX → (hermes provider name, opencode provider name)
MIRROR = {
    "ANTHROPIC":   ("anthropic",  "anthropic"),
    "OPENAI":      ("openai",     "openai"),
    "GEMINI":      ("gemini",     "google"),
    "VERTEX":      ("vertex",     "vertex"),
    "MISTRAL":     ("mistral",    "mistral"),
    "DEEPSEEK":    ("deepseek",   "deepseek"),
    "GROQ":        ("groq",       "groq"),
    "XAI":         ("xai",        "xai"),
    "OPENROUTER":  ("openrouter", "openrouter"),
    "TOGETHER":    ("together",   "together"),
    "FIREWORKS":   ("fireworks",  None),
    "COHERE":      ("cohere",     "cohere"),
    "CEREBRAS":    ("cerebras",   "cerebras"),
    "HUGGINGFACE": ("huggingface", None),
    "NVIDIA":      ("nvidia",     "nvidia"),
    "REPLICATE":   ("replicate",  None),
    "BAZAARLINK":  (None,         None),   # custom — handled via providers dict
    "KOLM":        (None,         None),
    "CLOUDFLARE":  ("cloudflare", "cloudflare"),
    "SAMBANOVA":   ("sambanova",  None),
    "AIMLAPI":     ("aimlapi",    None),
    "AI21":        ("ai21",       "ai21"),
    "MOONDREAM":   ("moondream",  None),
    "ZHIPU":       ("zhipu",      "zhipu"),
    "DEEPGRAM":    ("deepgram",   None),
    "KLUSTER":     ("kluster",    None),
}


# ── env loader ────────────────────────────────────────────────────────────────
def load_ai_env() -> dict[str, str]:
    """Parse ai/.env into a dict, handling multi-line KEYS lists."""
    if not AI_ENV.exists():
        return {}
    env = {}
    raw = AI_ENV.read_text()
    # Normalize: collapse lines that don't start with KEY= into previous line
    lines = raw.splitlines()
    buf = ""
    for line in lines:
        if "=" in line and not line.startswith((" ", "\t", "#")):
            if buf:
                k, _, v = buf.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
            buf = line
        else:
            buf += line
    if buf:
        k, _, v = buf.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def split_keys(value: str) -> list[str]:
    """Split comma- or space-separated key list."""
    if not value:
        return []
    if "," in value:
        return [k.strip() for k in value.split(",") if k.strip()]
    return [value]


# ── install ───────────────────────────────────────────────────────────────────
def install_hermes():
    """Install Hermes if not present + load caveman skill."""
    if shutil.which("hermes"):
        print("✓ hermes already installed:", subprocess.run(["hermes", "--version"], capture_output=True, text=True).stdout.strip())
    else:
        print("→ installing hermes...")
        subprocess.run(["bash", "-c",
                       "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"],
                       check=True)
    # caveman skill
    subprocess.run(["hermes", "skills", "install", "caveman"], check=False)
    # zshrc aliases (idempotent)
    ensure_zshrc_aliases()


def install_opencode():
    """Ensure opencode binary present + caveman plugin."""
    oc = HOME / ".opencode" / "bin" / "opencode"
    if oc.exists():
        print(f"✓ opencode at {oc}")
    else:
        print("→ installing opencode...")
        subprocess.run(["bash", "-c",
                       "curl -fsSL https://opencode.ai/install | bash"],
                       check=True)
    # caveman plugin already at ~/.config/opencode/plugins/caveman (verified)


def ensure_zshrc_aliases():
    """Idempotent zshrc append."""
    block_start = "# >>> carousel-managed aliases >>>"
    block_end   = "# <<< carousel-managed aliases <<<"
    if ZSHRC.exists() and block_start in ZSHRC.read_text():
        print("✓ zshrc aliases already present")
        return
    aliases = f"""
{block_start}
alias hm='hermes'
alias hmq='hermes chat -q'
alias hmm='hermes model'
alias hmd='hermes doctor'
alias hma='hermes auth'
alias hmal='hermes auth list'
alias hml='hermes login'
alias hmreset='hermes auth reset'
alias oc='~/.opencode/bin/opencode'
alias ocr='~/.opencode/bin/opencode run'
alias ocs='~/.opencode/bin/opencode serve'
alias ocacp='~/.opencode/bin/opencode acp --port 18792 --hostname 127.0.0.1'
alias ocprov='~/.opencode/bin/opencode providers'
alias ocv='~/.opencode/bin/opencode --version'
alias carousel='python3 ~/.local/bin/carousel'
alias carstatus='carousel status'
alias carroauth='carousel oauth'
alias carreg='carousel refresh'
{block_end}
"""
    with open(ZSHRC, "a") as f:
        f.write(aliases)
    print(f"✓ aliases appended to {ZSHRC}")


# ── mirror keys ───────────────────────────────────────────────────────────────
def mirror_to_hermes(env: dict, skip_existing: bool = True):
    """Pool all 28 keys into Hermes via `hermes auth add`.

    skip_existing: if True, check existing pool via `hermes auth list` and skip
    providers already present (avoids re-adding every run).
    """
    print("\n── mirroring keys → Hermes ──")
    existing_provs = set()
    if skip_existing:
        try:
            r = subprocess.run(["hermes", "auth", "list"], capture_output=True, text=True, timeout=20)
            # Parse "(N credentials)" lines; provider is the line above
            for line in r.stdout.splitlines():
                line = line.strip()
                if "credentials" in line and line.startswith("("):
                    prov = line.split("credentials")[0].strip().rstrip("(").strip()
                    if prov:
                        existing_provs.add(prov)
        except subprocess.TimeoutExpired:
            pass
        # Also strip "(N credential)" singular
        existing_provs = {p for p in existing_provs if p}
        # Merge in known mapped labels that would show as custom:<name>
        existing_provs.update({
            f"custom:{p}" for p in
            ("groq","mistral","vertex","deepseek","gemini","huggingface","xai",
             "cohere","cerebras","together","replicate","sambanova","aimlapi",
             "ai21","moondream","zhipu","deepgram","kluster","cloudflare","nvidia")
        })

    ok = fail = 0
    for prefix, (hprov, _) in MIRROR.items():
        if not hprov or hprov in existing_provs:
            if hprov in existing_provs:
                print(f"  · {hprov} already pooled (skip)")
            continue
        keys = split_keys(env.get(f"{prefix}_KEY", "")) or split_keys(env.get(f"{prefix}_KEYS", ""))
        for i, key in enumerate(keys, 1):
            if not key or key.startswith("#"):
                continue
            label = f"{prefix.lower()}-{i}"
            try:
                r = subprocess.run(
                    ["hermes", "auth", "add", hprov, "--type", "api-key",
                     "--api-key", key, "--label", label],
                    capture_output=True, text=True, timeout=20,
                )
                if r.returncode == 0:
                    ok += 1
                    print(f"  ✓ {hprov} #{i}")
                else:
                    fail += 1
                    print(f"  ✗ {hprov} #{i}: {r.stderr.strip()[:80]}")
            except subprocess.TimeoutExpired:
                fail += 1
                print(f"  ⚠ {hprov} #{i}: timed out (skipped)")
    print(f"\nHermes: {ok} added, {fail} failed")


def mirror_to_opencode(env: dict):
    """Write all 28 keys into ~/.local/share/opencode/auth.json."""
    print("\n── mirroring keys → OpenCode ──")
    OPENCODE_AUTH.parent.mkdir(parents=True, exist_ok=True)

    # Load existing (preserve structure opencode uses)
    existing = {}
    if OPENCODE_AUTH.exists():
        try:
            existing = json.loads(OPENCODE_AUTH.read_text())
        except json.JSONDecodeError:
            existing = {}

    # opencode auth.json format: {"provider-id": {"type": "api", "key": "..."}}
    # We use camelCase provider IDs matching `opencode providers list` output
    opencode_id_map = {
        "ANTHROPIC": "anthropic", "OPENAI": "openai", "GEMINI": "google",
        "VERTEX": "vertex", "MISTRAL": "mistral", "DEEPSEEK": "deepseek",
        "GROQ": "groq", "XAI": "xai", "OPENROUTER": "openrouter",
        "COHERE": "cohere", "CEREBRAS": "cerebras", "HUGGINGFACE": "huggingface",
        "NVIDIA": "nvidia", "CLOUDFLARE": "cloudflare", "AI21": "ai21",
        "ZHIPU": "zhipu",
    }

    ok = fail = 0
    for prefix, oc_id in opencode_id_map.items():
        keys = split_keys(env.get(f"{prefix}_KEY", "")) or split_keys(env.get(f"{prefix}_KEYS", ""))
        for key in keys:
            if not key or key.startswith("#"):
                continue
            # OpenCode stores one key per provider; last one wins (no native pool yet)
            existing[oc_id] = {"type": "api", "key": key}
            ok += 1

    OPENCODE_AUTH.write_text(json.dumps(existing, indent=2))
    os.chmod(OPENCODE_AUTH, 0o600)
    print(f"✓ {ok} keys written to {OPENCODE_AUTH}")


# ── OAuth (browser auto-open) ────────────────────────────────────────────────
def oauth_flow():
    """Drive Hermes OAuth for all providers, opening browser automatically.

    Hermes CLI opens browser itself — we just feed it provider args.
    """
    print("\n── OAuth carousel ──")
    print("Browser will open for each provider. Stay near the screen.\n")
    for prov in OAUTH_PROVIDERS:
        print(f"→ {prov}")
        try:
            r = subprocess.run(
                ["hermes", "login", "--provider", prov],
                timeout=180,
            )
            if r.returncode == 0:
                print(f"  ✓ {prov} done\n")
            else:
                print(f"  ✗ {prov} failed (code {r.returncode})\n")
        except subprocess.TimeoutExpired:
            print(f"  ⚠ {prov} timed out — skipping\n")
        except KeyboardInterrupt:
            print("\n  interrupted")
            return


# ── refresh (re-signup exhausted) ────────────────────────────────────────────
def refresh():
    """Re-signup all broken/exhausted providers via ai/provider_signer."""
    print("\n── refresh ──")
    if not AI_DIR.exists():
        print(f"✗ {AI_DIR} not mounted")
        return
    subprocess.run(["python3", "-m", "provider_signer", "signup", "all"],
                   cwd=str(AI_DIR))
    subprocess.run(["python3", "scanner.py"], cwd=str(AI_DIR))
    # Re-mirror after refresh
    env = load_ai_env()
    mirror_to_hermes(env)
    mirror_to_opencode(env)


# ── status ────────────────────────────────────────────────────────────────────
def status():
    """Show pooled creds + ai/.env key count + opencode provider count."""
    print("\n── Hermes pool ──")
    subprocess.run(["hermes", "auth", "list"], timeout=30)
    print("\n── OpenCode providers ──")
    subprocess.run([str(HOME / ".opencode/bin/opencode"), "providers", "list"], timeout=20)
    if AI_ENV.exists():
        env = load_ai_env()
        keys = sum(1 for k in env if k.endswith("_KEY") or k.endswith("_KEYS"))
        print(f"\n── ai/.env ──\n  {keys} providers pooled at {AI_ENV}")
    else:
        print(f"\n── ai/.env ──\n  not found at {AI_ENV}")


# ── nightly ───────────────────────────────────────────────────────────────────
def install_nightly_cron():
    """Add a cron entry that runs refresh + status at 03:30 daily."""
    cron_line = f"30 3 * * * /usr/bin/python3 {Path(__file__).resolve()} nightly"
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if "carousel nightly" in out:
            print("✓ nightly cron already installed")
            return
        new = out + cron_line + "\n"
        subprocess.run(["crontab", "-"], input=new, text=True, check=True)
        print(f"✓ nightly cron installed: {cron_line}")
    except FileNotFoundError:
        print("✗ crontab not available")


def nightly():
    """Full rotation: signup → scan → mirror → status."""
    refresh()
    status()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Carousel: unified multi-agent auth rotation")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("install", help="install hermes+opencode, load skills, mirror keys")
    sub.add_parser("oauth",   help="OAuth flows for codex/qwen/nous")
    sub.add_parser("refresh", help="re-signup exhausted providers")
    sub.add_parser("status",  help="show all pools")
    sub.add_parser("nightly", help="full rotation cycle (cron-callable)")

    args = p.parse_args()
    if args.cmd == "install":
        install_hermes()
        install_opencode()
        ensure_zshrc_aliases()
        env = load_ai_env()
        if env:
            mirror_to_hermes(env)
            mirror_to_opencode(env)
        install_nightly_cron()
        status()
    elif args.cmd == "oauth":
        oauth_flow()
    elif args.cmd == "refresh":
        refresh()
    elif args.cmd == "status":
        status()
    elif args.cmd == "nightly":
        nightly()
    else:
        p.print_help()


if __name__ == "__main__":
    main()