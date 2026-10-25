import os
import sys
from pathlib import Path
from huggingface_hub import HfApi, login

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")

def deploy(hf_token: str, space_name: str = "btc-options-pinning"):
    clean_token = hf_token.strip()
    if not clean_token:
        print("[ERROR] Token cannot be empty.")
        return None, None

    print("[1/3] Logging into Hugging Face...")
    login(token=clean_token, add_to_git_credential=False)
    api = HfApi(token=clean_token)

    try:
        who = api.whoami()
        user = who["name"]
        print(f"[OK] Authenticated as user: {user}")
    except Exception as e:
        print(f"[ERROR] Authentication failed: {e}")
        return None, None

    repo_id = f"{user}/{space_name}"
    print(f"[2/3] Setting up Docker Space: {repo_id}...")

    try:
        api.create_repo(
            repo_id=repo_id,
            repo_type="space",
            space_sdk="docker",
            exist_ok=True,
            private=False
        )
        print("[OK] Space repository ready.")
    except Exception as e:
        print(f"[INFO] Create repo info: {e}")

    print("[3/3] Uploading project files, templates, and database...")
    folder_path = Path(__file__).resolve().parent

    try:
        api.upload_folder(
            folder_path=str(folder_path),
            repo_id=repo_id,
            repo_type="space",
            ignore_patterns=[
                ".venv/**",
                "__pycache__/**",
                "*/__pycache__/**",
                "*.pyc",
                ".git/**",
                "cloudflared.exe",
                "*.log"
            ]
        )
        print("[OK] Upload completed successfully!")
    except Exception as e:
        print(f"[ERROR] Upload failed: {e}")
        return None, None

    space_page = f"https://huggingface.co/spaces/{repo_id}"
    app_url = f"https://{user.lower().replace('_', '-')}-{space_name.lower().replace('_', '-')}.hf.space"

    print("\n" + "="*60)
    print("DEPLOYMENT COMPLETE & RUNNING 24/7 ONLINE!")
    print(f"Hugging Face Space: {space_page}")
    print(f"Direct App URL:     {app_url}")
    print("="*60 + "\n")

    return space_page, app_url

if __name__ == "__main__":
    token = sys.argv[1] if len(sys.argv) > 1 else input("Enter your Hugging Face Token: ").strip()
    deploy(token)
