"""
Script สำหรับอัปโหลดขึ้น Hugging Face Spaces เพื่อให้ออนไลน์ 24 ชั่วโมงฟรีแม้ปิดคอมพิวเตอร์
วิธีใช้:
1. สมัครสมาชิกฟรีที่ https://huggingface.co/join (ไม่ใช้บัตรเครดิต)
2. สร้าง Access Token ที่ https://huggingface.co/settings/tokens (เลือก Role: Write)
3. รัน: python upload_to_hf.py <YOUR_HF_USERNAME> <YOUR_HF_TOKEN>
"""

import sys
import os
from pathlib import Path

def main():
    if len(sys.argv) < 3:
        print("วิธีใช้งาน: python upload_to_hf.py <USERNAME> <HF_WRITE_TOKEN> [SPACE_NAME]")
        print("ตัวอย่าง: python upload_to_hf.py myusername hf_xxxx btc-options-pinning")
        return

    username = sys.argv[1]
    token = sys.argv[2]
    space_name = sys.argv[3] if len(sys.argv) > 3 else "btc-options-pinning"
    repo_id = f"{username}/{space_name}"

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("กำลังติดตั้ง huggingface_hub...")
        os.system(f"{sys.executable} -m pip install huggingface_hub")
        from huggingface_hub import HfApi

    api = HfApi(token=token)

    print(f"กำลังสร้าง Space: {repo_id} (Docker SDK)...")
    try:
        api.create_repo(
            repo_id=repo_id,
            repo_type="space",
            space_sdk="docker",
            exist_ok=True,
            private=False,
        )
        print("สร้าง/พบ Space เรียบร้อยแล้ว")
    except Exception as e:
        print(f"Error creating space: {e}")

    # Files to upload
    base_dir = Path(__file__).resolve().parent
    print(f"กำลังอัปโหลดไฟล์จาก {base_dir} ไปยัง Hugging Face Space...")

    # Upload entire folder excluding .venv, __pycache__, cloudflared.exe
    ignore_patterns = [".venv/**", "**/__pycache__/**", "cloudflared.exe", "*.pyc"]

    api.upload_folder(
        folder_path=str(base_dir),
        repo_id=repo_id,
        repo_type="space",
        ignore_patterns=ignore_patterns,
    )

    space_url = f"https://huggingface.co/spaces/{repo_id}"
    app_url = f"https://{username}-{space_name}.hf.space"
    print("\n" + "="*60)
    print("🎉 อัปโหลดสำเร็จเรียบร้อยแล้ว!")
    print(f"📌 หน้าจัดการ Space: {space_url}")
    print(f"🌐 ลิงก์ Dashboard ออนไลน์ 24 ชม.: {app_url}")
    print("="*60)
    print("Hugging Face กำลัง Build Docker Container (~1-2 นาที) แล้วจะเปิดทำงาน 24 ชม. ตลอดเวลา แม้คุณจะปิดคอมพิวเตอร์ครับ!")

if __name__ == "__main__":
    main()
