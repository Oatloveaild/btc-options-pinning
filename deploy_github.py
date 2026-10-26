import base64
import os
import sys
from pathlib import Path
import httpx

if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "replace")
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "replace")

def upload_to_github(github_token: str, repo_name: str = "btc-options-pinning"):
    headers = {
        "Authorization": f"token {github_token.strip()}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "BTC-Pinning-Deployer"
    }

    # 1. Get authenticated user
    r = httpx.get("https://api.github.com/user", headers=headers)
    if r.status_code != 200:
        print(f"❌ Authentication failed: {r.status_code} {r.text}")
        return False
    username = r.json()["login"]
    print(f"👤 Authenticated as GitHub user: {username}")

    # 2. Create repo if not exists
    create_payload = {
        "name": repo_name,
        "description": "BTC Options Pinning Dashboard & Auto Data Collector (Deribit)",
        "private": False,
        "auto_init": False
    }
    r_create = httpx.post("https://api.github.com/user/repos", json=create_payload, headers=headers)
    if r_create.status_code in (201, 422):
        print(f"📦 Repository {username}/{repo_name} is ready.")
    else:
        print(f"❌ Error creating repo: {r_create.status_code} {r_create.text}")

    # 3. Upload project files
    base_dir = Path(__file__).resolve().parent
    ignore_dirs = {".venv", "__pycache__", ".git", ".idea", ".vscode", "raw", "logs"}
    ignore_files = {"cloudflared.exe"}

    files_to_upload = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for f in files:
            if f in ignore_files or f.endswith(".pyc") or f.endswith(".log") or f.endswith(".db"):
                continue
            full_path = Path(root) / f
            rel_path = full_path.relative_to(base_dir).as_posix()
            files_to_upload.append((rel_path, full_path))

    print(f"🚀 Uploading {len(files_to_upload)} files to GitHub...")
    for rel_path, full_path in files_to_upload:
        try:
            with open(full_path, "rb") as fp:
                content_b64 = base64.b64encode(fp.read()).decode("utf-8")

            # Check if file exists to get SHA
            sha = None
            r_check = httpx.get(f"https://api.github.com/repos/{username}/{repo_name}/contents/{rel_path}", headers=headers)
            if r_check.status_code == 200:
                sha = r_check.json().get("sha")

            put_payload = {
                "message": f"Add/update {rel_path}",
                "content": content_b64
            }
            if sha:
                put_payload["sha"] = sha

            r_put = httpx.put(f"https://api.github.com/repos/{username}/{repo_name}/contents/{rel_path}", json=put_payload, headers=headers)
            if r_put.status_code in (200, 201):
                print(f"  ✓ {rel_path}")
            else:
                print(f"  ✗ {rel_path} ({r_put.status_code})")
        except Exception as e:
            print(f"  ✗ {rel_path}: {e}")

    repo_url = f"https://github.com/{username}/{repo_name}"
    render_deploy_url = f"https://render.com/deploy?repo={repo_url}"
    print("\n" + "="*60)
    print("🎉 GITHUB REPOSITORY READY!")
    print(f"🔗 Repo URL: {repo_url}")
    print(f"🚀 1-Click Deploy to Render: {render_deploy_url}")
    print("="*60 + "\n")

    # Auto trigger Render Deploy Hook if configured
    deploy_hook = os.getenv("RENDER_DEPLOY_HOOK", "https://api.render.com/deploy/srv-dberj8dg1s2s73dirqo0?key=ku_MLgq0LcA")
    if deploy_hook:
        try:
            print("🚀 Triggering Render auto-deploy via Deploy Hook...")
            r_dep = httpx.post(deploy_hook, timeout=15)
            if r_dep.status_code == 200:
                print(f"  ✓ Render deploy triggered successfully: {r_dep.text}")
            else:
                print(f"  ✗ Render deploy hook returned: {r_dep.status_code} {r_dep.text}")
        except Exception as e:
            print(f"  ✗ Failed to call Render deploy hook: {e}")

    return repo_url
