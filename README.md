---
title: BTC Options Pinning Dashboard
emoji: ₿
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# BTC Options Price Pinning Dashboard (Deribit)

ระบบเก็บสถิติ BTC Options จาก Deribit Public API และเว็บไซต์ Dashboard วิเคราะห์ **Price Pinning** เมื่อสัญญาหมดอายุ (15:00 น. เวลาไทย / 08:00 UTC)

* 🌐 **Live Dashboard:** [https://btc-options-pinning.onrender.com](https://btc-options-pinning.onrender.com)
* 🐙 **GitHub Repository:** [https://github.com/ninayasha/btc-options-pinning](https://github.com/ninayasha/btc-options-pinning)
* 📁 **Google Drive Backup:** `BTC_Options_Pinning_Data` (industry9110@gmail.com)

---

## 📌 คุณสมบัติเด่น (Features)
1. **ข้อมูลจริง 100% จาก Deribit Public API**: ไม่มี Mock ไม่มีการแต่งตัวเลข มี Retry & Exponential backoff
2. **ภาษาไทย & เวลาไทย (Asia/Bangkok, UTC+7)** ทั่วทั้งระบบ
3. **การคำนวณ Options Math ครบถ้วน**:
   - **Max Pain Strike** (จุดที่ผู้ถือ option ขาดทุนรวมมากที่สุด)
   - **Top Strikes**: Call #1 (C1), Call #2 (C2), Put #1 (P1), Put #2 (P2), Top OI Strike รวม
   - **Dealer GEX (Gamma Exposure)**: Long Call (+) / Short Put (-) หน่วย USD/1% spot move
   - **Gamma Flip Level**: Re-pricing ด้วย Black-Scholes บน grid spot ±15% (step 0.05%) หาจุดตัดศูนย์ที่ใกล้ spot ที่สุด
   - **Volatility Metrics**: DVOL, ATM IV (ค่าเฉลี่ย Call/Put ATM), 25-delta Risk Reversal ($IV_{25\Delta C} - IV_{25\Delta P}$)
   - **OI Change**: เปรียบเทียบระหว่างรอบ 06:00 vs 21:00 vs 15:30 (Strike ที่เพิ่ม/ลดสูงสุด 3 อันดับ, การขยับของ Max Pain / C1 / P1)
   - **ปฏิทินเศรษฐกิจ & อีเวนต์**: เชื่อมต่อ ForexFactory (USD High/Medium) อัตโนมัติ + บันทึกอีเวนต์คริปโตเองได้
4. **Dashboard ครบทั้ง 7 หน้า**:
   - `หน้าล่าสุด (Latest)`: การ์ดสรุป + กราฟ 2 แผง (OI per Strike & GEX per Strike) + ตาราง C1/C2/P1/P2 + Wall summary
   - `หน้าการเปลี่ยนแปลง OI (OI Change)`: เปรียบเทียบ Snapshot รอบต่างๆ ใน Expiry เดียวกัน
   - `หน้า GEX รวม (Aggregate GEX)`: GEX รวมทุก Expiry รายสัปดาห์ / สิ้นเดือน / ราย Expiry
   - `หน้าผล Settle (Results)`: ประวัติราคา Settle, ระยะห่าง (%) จาก Max Pain, Top OI, C1, P1, Flip, สถานะอยู่ในกรอบ P1–C1
   - `หน้าสถิติสะสม (Stats)`: ตาราง Hit-rate (±0.5%, ±1%, n ตัวอย่าง, ในกรอบ P1–C1) แยกตามประเภท Expiry และวันที่มี Event + กราฟ Distribution
   - `หน้า Expiry ขนาดใหญ่ (Big Expiries)`: Weekly, Monthly, Quarterly ถัดไป
   - `หน้าข่าว/อีเวนต์ & Logs (Events)`: ตาราง ForexFactory + ฟอร์มเพิ่ม/ลบอีเวนต์คริปโต + บันทึกสถานะการรัน
   - ทุกหน้ามีปุ่ม **Export CSV**

---

## 🚀 วิธีเปิดใช้งานระบบ

### 1. รัน Web Dashboard
```powershell
cd C:\Users\Noname\.gemini\antigravity\scratch\btc-pinning-dashboard
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe main.py
```
เปิดบราวเซอร์ไปที่: **http://localhost:8000**

### 2. รัน Snapshot หรือ Settlement ผ่าน CLI
```powershell
$env:PYTHONPATH = "."

# รัน snapshot รอบปัจจุบัน
.\.venv\Scripts\python.exe run_collector.py manual

# รัน snapshot ตามรอบเวลา
.\.venv\Scripts\python.exe run_collector.py 0600
.\.venv\Scripts\python.exe run_collector.py 1530
.\.venv\Scripts\python.exe run_collector.py 2100

# รัน settlement สำหรับวันนี้
.\.venv\Scripts\python.exe run_collector.py settle
```

### 3. รัน Unit Tests
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\pytest.exe tests -v
```

---

## ⏰ ตารางเวลารันอัตโนมัติ (Scheduler Asia/Bangkok)
ระบบมี **APScheduler** ฝังอยู่ใน Web App ทำงานอัตโนมัติตามเวลาไทย:
- **06:00** → Snapshot Expiry ของวันนี้ (หมดอายุ 15:00)
- **15:30** → (1) Settle Expiry ของวันนี้ (ดึงราคา Delivery เทียบกับรอบ 06:00 หรือ fallback รอบ 21:00 เมื่อวาน) + (2) Snapshot Expiry ของพรุ่งนี้
- **21:00** → Snapshot Expiry ของพรุ่งนี้อีกรอบ
- หรือกดปุ่ม **"⚡ รัน Snapshot ตอนนี้"** และ **"🎯 รัน Settlement"** ได้ทันทีบนหน้าเว็บ
