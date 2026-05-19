#!/usr/bin/env python3
"""
⛏️  MINER HERO v3.0 - تعدين بيتكوين حقيقي عبر بروتوكول Stratum
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✔ إصلاح رئيسي: target لا يُعاد ضبطه من nbits عند كل job جديد
✔ إصلاح: prev_hash byte-swap صحيح (كل 4 بايت مستقل)
✔ إصلاح: merkle_root بدون قلب غير ضروري
✔ إصلاح: nonce_batch يغطي كامل نطاق 32-bit
✔ تطوير: ntime rolling (تحديث تلقائي كل ثانية)
✔ تطوير: extra_nonce2 لكل خيط بدون تداخل
✔ تطوير: إحصاء shares مفصّل
"""

import socket
import json
import hashlib
import struct
import time
import threading
import logging
import queue
from datetime import datetime

# ─────────────────────────────────────────────────────────────
#  إعدادات المعدّن  ← عدّل هنا فقط
# ─────────────────────────────────────────────────────────────
POOL_HOST       = "public-pool.io"
POOL_PORT       = 21496
BTC_ADDRESS     = "bc1q0uaa30cennmll6xs9f22zu9qy7npz3r384wqxp"
WORKER_NAME     = BTC_ADDRESS          # اسم العامل = عنوان البيتكوين
WORKER_PASS     = "x"

NUM_THREADS     = 4                    # عدد خيوط التعدين
RECONNECT_DELAY = 5                    # ثوانٍ قبل إعادة الاتصال
STATS_INTERVAL  = 15                   # ثوانٍ بين تقارير الأداء

# ─────────────────────────────────────────────────────────────
#  نظام السجلات
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("MinerHero")


# ═════════════════════════════════════════════════════════════
#  دوال التجزئة والتحويل
# ═════════════════════════════════════════════════════════════

def sha256d(data: bytes) -> bytes:
    """Double SHA-256 — تجزئة مزدوجة كما تستخدمها شبكة البيتكوين"""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def pack_uint32_le(n: int) -> bytes:
    """عدد صحيح → 4 بايت little-endian"""
    return struct.pack("<I", n & 0xFFFFFFFF)


def difficulty_to_target(difficulty: float) -> int:
    """تحويل صعوبة pool إلى هدف رقمي قابل للمقارنة"""
    diff1 = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
    if difficulty <= 0:
        return diff1
    return int(diff1 / difficulty)


def swap_prev_hash(h: str) -> bytes:
    """
    ✔ الإصلاح الصحيح لـ prev_hash
    Stratum يرسل كل 4 بايتات (8 أحرف) بترتيب little-endian مستقل
    نقلب كل مجموعة على حدة
    """
    result = b""
    for i in range(0, 64, 8):
        result += bytes.fromhex(h[i:i+8])[::-1]
    return result


# ═════════════════════════════════════════════════════════════
#  حساب Merkle Root
# ═════════════════════════════════════════════════════════════

def build_coinbase(coinb1: str, en1: str, en2: str, coinb2: str) -> bytes:
    return bytes.fromhex(coinb1 + en1 + en2 + coinb2)


def build_merkle_root(coinbase: bytes, branches: list) -> bytes:
    """
    ✔ الحساب الصحيح: SHA256d(coinbase) ثم SHA256d(current + branch)
    النتيجة بـ natural byte order (لا قلب)
    """
    current = sha256d(coinbase)
    for branch in branches:
        current = sha256d(current + bytes.fromhex(branch))
    return current


# ═════════════════════════════════════════════════════════════
#  بناء رأس البلوك (80 بايت)
# ═════════════════════════════════════════════════════════════

def build_header(version: str, prev_hash: str, merkle_root: bytes,
                 ntime: str, nbits: str, nonce: int) -> bytes:
    """
    تركيب رأس البلوك بالترتيب الصحيح:
    version(4) + prev_hash(32) + merkle_root(32) + ntime(4) + nbits(4) + nonce(4)

    ملاحظات endianness:
    - version  : يصل big-endian → نقلب لـ LE
    - prev_hash: نطبّق swap_prev_hash (كل 4 بايت مستقل)
    - merkle   : natural order بدون قلب
    - ntime    : يصل big-endian → نقلب لـ LE
    - nbits    : يصل big-endian → نقلب لـ LE
    - nonce    : 4 بايت LE
    """
    header = (
        bytes.fromhex(version)[::-1]   +   # version LE
        swap_prev_hash(prev_hash)       +   # prev_hash swapped
        merkle_root                     +   # merkle natural
        bytes.fromhex(ntime)[::-1]      +   # ntime LE
        bytes.fromhex(nbits)[::-1]      +   # nbits LE
        pack_uint32_le(nonce)               # nonce LE
    )
    assert len(header) == 80
    return header


def header_hash_int(header: bytes) -> int:
    """تجزئة الرأس وتحويل الناتج لعدد صحيح للمقارنة مع الهدف"""
    h = sha256d(header)
    return int.from_bytes(h[::-1], "big")


# ═════════════════════════════════════════════════════════════
#  اتصال Stratum
# ═════════════════════════════════════════════════════════════

class StratumClient:

    def __init__(self, host: str, port: int):
        self.host    = host
        self.port    = port
        self.sock    = None
        self._buf    = b""
        self._lock   = threading.Lock()
        self._msg_id = 0

    def connect(self) -> bool:
        try:
            self.sock = socket.create_connection(
                (self.host, self.port), timeout=30
            )
            self.sock.settimeout(5)
            log.info(f"متصل: {self.host}:{self.port}")
            return True
        except Exception as e:
            log.error(f"فشل الاتصال: {e}")
            return False

    def close(self):
        if self.sock:
            try: self.sock.close()
            except: pass
            self.sock = None

    def send(self, method: str, params: list) -> int:
        with self._lock:
            self._msg_id += 1
            msg = {"id": self._msg_id, "method": method, "params": params}
            try:
                self.sock.sendall((json.dumps(msg) + "\n").encode())
            except Exception as e:
                log.error(f"خطأ إرسال: {e}")
            return self._msg_id

    def recv_messages(self) -> list:
        messages = []
        try:
            chunk = self.sock.recv(8192)
            if chunk:
                self._buf += chunk
        except socket.timeout:
            pass
        except Exception as e:
            log.warning(f"خطأ استقبال: {e}")
            raise

        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except:
                    pass
        return messages


# ═════════════════════════════════════════════════════════════
#  حالة المهمة
# ═════════════════════════════════════════════════════════════

class JobState:

    def __init__(self):
        self._lock            = threading.Lock()
        self.job              = None
        self.extra_nonce1     = ""
        self.en2_size         = 4
        # ✔ الإصلاح: target يأتي فقط من set_difficulty
        self.target           = difficulty_to_target(1.0)  # افتراضي
        self.difficulty       = 1.0
        self._en2_counter     = 0

    def update_job(self, job: dict, en1: str, en2_size: int):
        """
        ✔ الإصلاح الرئيسي: لا نلمس self.target هنا أبداً
        target يبقى من آخر set_difficulty
        """
        with self._lock:
            self.job          = job
            self.extra_nonce1 = en1
            self.en2_size     = en2_size

    def update_difficulty(self, diff: float):
        with self._lock:
            self.difficulty = diff
            self.target     = difficulty_to_target(diff)
        log.info(f"🎯 صعوبة جديدة: {diff} | "
                 f"target: {hex(self.target)[:18]}...")

    def next_en2(self, thread_id: int) -> str:
        """
        ✔ extra_nonce2 بدون تداخل بين الخيوط
        thread_id=0 → 0, N, 2N...
        thread_id=1 → 1, N+1, 2N+1...
        """
        with self._lock:
            val = self._en2_counter * NUM_THREADS + thread_id
            self._en2_counter += 1
        return val.to_bytes(self.en2_size, "little").hex()

    def snapshot(self):
        with self._lock:
            if self.job is None:
                return None
            return {
                "job":    dict(self.job),
                "en1":    self.extra_nonce1,
                "en2sz":  self.en2_size,
                "target": self.target,
            }

    @property
    def has_job(self) -> bool:
        with self._lock:
            return self.job is not None


# ═════════════════════════════════════════════════════════════
#  خيط التعدين
# ═════════════════════════════════════════════════════════════

class MiningThread(threading.Thread):

    def __init__(self, tid: int, state: JobState,
                 result_q: queue.Queue, stats: dict):
        super().__init__(daemon=True, name=f"Miner-{tid}")
        self.tid      = tid
        self.state    = state
        self.result_q = result_q
        self.stats    = stats
        self._stop    = threading.Event()

    def stop(self): self._stop.set()

    def run(self):
        while not self._stop.is_set():
            snap = self.state.snapshot()
            if snap is None:
                time.sleep(0.1)
                continue
            self._mine_job(snap)

    def _mine_job(self, snap: dict):
        job    = snap["job"]
        target = snap["target"]
        en1    = snap["en1"]
        en2    = self.state.next_en2(self.tid)

        coinbase    = build_coinbase(job["coinb1"], en1, en2, job["coinb2"])
        merkle_root = build_merkle_root(coinbase, job["merkle_branches"])
        job_id      = job["job_id"]

        # ntime rolling: نبدأ بـ ntime المهمة ونزيده كل ~1M hash
        base_ntime = int(job["ntime"], 16)
        ntime_inc  = 0
        BATCH      = 1_000_000  # نطاق nonce الكامل تقريباً

        for nonce in range(0x100000000):  # كامل 32-bit
            # تحقق من تغيير المهمة كل 100K
            if nonce % 100_000 == 0:
                if self._stop.is_set():
                    return
                cur = self.state.snapshot()
                if cur and cur["job"]["job_id"] != job_id:
                    return

            # ntime rolling كل مليون
            if nonce > 0 and nonce % BATCH == 0:
                ntime_inc += 1

            ntime_hex = struct.pack(">I", base_ntime + ntime_inc).hex()

            header = build_header(
                job["version"],
                job["prev_hash"],
                merkle_root,
                ntime_hex,
                job["nbits"],
                nonce
            )
            h_int = header_hash_int(header)

            # إحصاء
            self.stats["hashes"] = self.stats.get("hashes", 0) + 1

            if h_int < target:
                self.result_q.put({
                    "job_id":  job_id,
                    "en2":     en2,
                    "ntime":   ntime_hex,
                    "nonce":   nonce,
                    "hash":    sha256d(header)[::-1].hex(),
                })
                log.info(f"[🎉 T{self.tid}] حل وجد! nonce={nonce:#010x}")
                # بعد الحل، غيّر en2 للخيط
                en2     = self.state.next_en2(self.tid)
                coinbase = build_coinbase(job["coinb1"], en1, en2, job["coinb2"])
                merkle_root = build_merkle_root(coinbase, job["merkle_branches"])


# ═════════════════════════════════════════════════════════════
#  المعدّن الرئيسي
# ═════════════════════════════════════════════════════════════

class MinerHero:

    def __init__(self):
        self.client   = StratumClient(POOL_HOST, POOL_PORT)
        self.state    = JobState()
        self.result_q = queue.Queue()
        self.stats    = {"hashes": 0, "accepted": 0, "rejected": 0}
        self.threads  = []
        self.t0       = None
        self._running = True
        self._ping_t  = 0

    # ─── Handshake ──────────────────────────────────────────

    def _handshake(self) -> bool:
        # Subscribe
        sid = self.client.send("mining.subscribe",
                               [f"MinerHero/3.0/{BTC_ADDRESS}"])
        time.sleep(1.5)
        for m in self.client.recv_messages():
            if m.get("id") == sid and isinstance(m.get("result"), list):
                r = m["result"]
                self.state.extra_nonce1 = r[1]
                self.state.en2_size     = r[2]
                log.info(f"Subscribe OK | en1={r[1]} | en2_size={r[2]}")
                break
        else:
            log.error("Subscribe فشل")
            return False

        # Authorize
        aid = self.client.send("mining.authorize",
                               [WORKER_NAME, WORKER_PASS])
        time.sleep(0.8)
        for m in self.client.recv_messages():
            if m.get("id") == aid:
                if m.get("result") is True:
                    log.info(f"✅ مُفوَّض: {WORKER_NAME}")
                    return True
                log.error(f"❌ رُفض: {m.get('error')}")
                return False

        log.info("انتظار تأكيد التفويض...")
        return True

    # ─── Threads ────────────────────────────────────────────

    def _start_threads(self):
        for t in self.threads: t.stop()
        self.threads.clear()
        for i in range(NUM_THREADS):
            t = MiningThread(i, self.state, self.result_q, self.stats)
            t.start()
            self.threads.append(t)
        log.info(f"▶ {NUM_THREADS} خيوط تعدين تعمل")

    def _stop_threads(self):
        for t in self.threads: t.stop()
        self.threads.clear()

    # ─── Message Handler ────────────────────────────────────

    def _handle(self, msg: dict):
        method = msg.get("method", "")
        params = msg.get("params", [])

        if method == "mining.notify":
            if len(params) < 9: return
            job = {
                "job_id":          params[0],
                "prev_hash":       params[1],
                "coinb1":          params[2],
                "coinb2":          params[3],
                "merkle_branches": params[4],
                "version":         params[5],
                "nbits":           params[6],
                "ntime":           params[7],
                "clean":           params[8],
            }
            # ✔ لا نمرر target — يبقى من set_difficulty
            self.state.update_job(
                job,
                self.state.extra_nonce1,
                self.state.en2_size
            )
            tag = "🔄 تنظيف" if job["clean"] else "➕ إضافة"
            log.info(f"مهمة [{tag}] {job['job_id'][:12]} | "
                     f"diff={self.state.difficulty}")

        elif method == "mining.set_difficulty":
            if params:
                self.state.update_difficulty(float(params[0]))

        elif msg.get("id") and "result" in msg:
            if msg["result"] is True:
                self.stats["accepted"] += 1
                log.info(f"✅ Share مقبول! إجمالي: {self.stats['accepted']}")
            elif msg.get("error"):
                self.stats["rejected"] += 1
                log.warning(f"❌ Share مرفوض: {msg['error']}")

    # ─── Submit ─────────────────────────────────────────────

    def _submit(self, r: dict):
        nonce_hex = pack_uint32_le(r["nonce"]).hex()
        log.info(f"📤 إرسال | job={r['job_id'][:12]} "
                 f"nonce={r['nonce']:#010x} hash={r['hash'][:16]}...")
        self.client.send("mining.submit", [
            WORKER_NAME,
            r["job_id"],
            r["en2"],
            r["ntime"],
            nonce_hex,
        ])

    # ─── Stats ──────────────────────────────────────────────

    def _print_stats(self):
        elapsed  = time.time() - self.t0
        h        = self.stats["hashes"]
        rate     = h / elapsed if elapsed > 0 else 0
        if rate >= 1e6:   rs = f"{rate/1e6:.2f} MH/s"
        elif rate >= 1e3: rs = f"{rate/1e3:.2f} KH/s"
        else:             rs = f"{rate:.0f} H/s"
        log.info(
            f"📊 {rs} | هاشات: {h:,} | "
            f"✅ {self.stats['accepted']} | "
            f"❌ {self.stats['rejected']} | "
            f"⏱ {elapsed:.0f}s"
        )

    # ─── Main Loop ──────────────────────────────────────────

    def run(self):
        _banner(BTC_ADDRESS, POOL_HOST, POOL_PORT, NUM_THREADS)
        self.t0 = time.time()
        last_stats = time.time()

        while self._running:
            if not self.client.connect():
                time.sleep(RECONNECT_DELAY); continue
            if not self._handshake():
                self.client.close()
                time.sleep(RECONNECT_DELAY); continue

            self._start_threads()

            try:
                while self._running:
                    # استقبال
                    for m in self.client.recv_messages():
                        self._handle(m)

                    # إرسال حلول
                    while not self.result_q.empty():
                        self._submit(self.result_q.get_nowait())

                    # إحصاءات
                    now = time.time()
                    if now - last_stats >= STATS_INTERVAL:
                        self._print_stats()
                        last_stats = now

                    # ping
                    if now - self._ping_t >= 55:
                        self.client.send("mining.ping", [])
                        self._ping_t = now

                    time.sleep(0.02)

            except KeyboardInterrupt:
                self._running = False
            except Exception as e:
                log.error(f"خطأ: {e}")
                log.info(f"إعادة اتصال خلال {RECONNECT_DELAY}s...")
            finally:
                self._stop_threads()
                self.client.close()
                if self._running:
                    time.sleep(RECONNECT_DELAY)

        _final_stats(self.stats, self.t0)


# ═════════════════════════════════════════════════════════════
#  شاشات العرض
# ═════════════════════════════════════════════════════════════

def _banner(addr, host, port, threads):
    print("""
╔══════════════════════════════════════════════════════════╗
║       ⛏️   MINER HERO v3.0 - تعدين بيتكوين حقيقي        ║
║        بروتوكول Stratum | الإصلاح الكامل للـ Target      ║
╚══════════════════════════════════════════════════════════╝""")
    print(f"  BTC     : {addr}")
    print(f"  Pool    : {host}:{port}")
    print(f"  Threads : {threads}")
    print(f"  Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("─" * 60)


def _final_stats(stats, t0):
    elapsed = time.time() - t0 if t0 else 1
    h       = stats.get("hashes", 0)
    rate    = h / elapsed if elapsed > 0 else 0
    print("\n" + "═" * 60)
    print("🏁 توقف المعدّن")
    print(f"   هاشات    : {h:,}")
    print(f"   سرعة     : {rate:,.0f} H/s")
    print(f"   مقبولة   : {stats.get('accepted', 0)}")
    print(f"   مرفوضة   : {stats.get('rejected', 0)}")
    print(f"   وقت      : {elapsed:.0f}s")
    print("═" * 60)


# ═════════════════════════════════════════════════════════════
#  نقطة الدخول
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    miner = MinerHero()
    try:
        miner.run()
    except KeyboardInterrupt:
        pass

