#!/usr/bin/env python3
"""
⛏️  MINER HERO - تعدين بيتكوين حقيقي عبر بروتوكول Stratum
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✔ إصلاح: حساب جذر Merkle الصحيح (خطأ كان في النسخة السابقة)
✔ إصلاح: بناء رأس البلوك بالترتيب الصحيح (little-endian)
✔ تطوير: دعم difficulty من التجمع (mining.set_difficulty)
✔ تطوير: إعادة الاتصال التلقائي عند الانقطاع
✔ تطوير: نظام logging مناسب
✔ تطوير: دعم multi-threading للتعدين الأسرع
✔ تطوير: حساب hashrate دقيق
✔ تطوير: التحقق من الحل قبل الإرسال
"""

import socket
import json
import hashlib
import struct
import time
import os
import threading
import logging
import queue
from binascii import hexlify, unhexlify
from datetime import datetime

# ─────────────────────────────────────────
#  إعدادات المعدّن
# ─────────────────────────────────────────
POOL_HOST    = "public-pool.io"
POOL_PORT    = 21496
WORKER_NAME  = "bc1q0uaa30cennmll6xs9f22zu9qy7npz3r384wqxp"
BTC_ADDRESS  = "bc1q0uaa30cennmll6xs9f22zu9qy7npz3r384wqxp"
WORKER_PASS  = "x"

# عدد خيوط التعدين (زد هنا لرفع السرعة على CPUs متعددة)
NUM_THREADS  = 4

# عدد nonces لكل خيط قبل الانتقال لـ extra_nonce2 جديد
NONCE_BATCH  = 100_000

# إعادة الاتصال بعد كم ثانية عند الانقطاع
RECONNECT_DELAY = 5

# ─────────────────────────────────────────
#  إعداد نظام السجلات
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("MinerHero")


# ═══════════════════════════════════════════════════════
#  دوال التجزئة والتحويل
# ═══════════════════════════════════════════════════════

def sha256d(data: bytes) -> bytes:
    """SHA-256 مزدوجة (Double SHA-256) كما تستخدمها شبكة البيتكوين"""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def sha256d_hex(data: bytes) -> str:
    """Double SHA-256 وتعيد الناتج بصيغة hex"""
    return sha256d(data).hex()


def flip_endian(hex_str: str) -> str:
    """
    عكس ترتيب البايتات (byte-swap) في سلسلة hex
    مثال: "aabbcc" → "ccbbaa"
    """
    if len(hex_str) % 2 != 0:
        hex_str = "0" + hex_str
    b = bytes.fromhex(hex_str)
    return b[::-1].hex()


def pack_uint32_le(n: int) -> bytes:
    """تحويل عدد صحيح إلى 4 بايت little-endian"""
    return struct.pack("<I", n)


def hex_uint32_le(n: int) -> str:
    """عدد صحيح ← hex بـ little-endian (8 أحرف)"""
    return pack_uint32_le(n).hex()


def nbits_to_target(nbits_hex: str) -> int:
    """
    تحويل nbits المضغوطة إلى الهدف الكامل (256-bit integer)
    الصيغة: أول بايت = الأس، باقي 3 بايتات = المعامل
    """
    nbits   = int(nbits_hex, 16)
    exp     = nbits >> 24
    mantissa = nbits & 0x007FFFFF
    target  = mantissa * (2 ** (8 * (exp - 3)))
    return target


def difficulty_to_target(difficulty: float) -> int:
    """تحويل الصعوبة (difficulty) إلى هدف رقمي"""
    diff1_target = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
    if difficulty <= 0:
        return diff1_target
    return int(diff1_target / difficulty)


# ═══════════════════════════════════════════════════════
#  حساب جذر Merkle (الصحيح)
# ═══════════════════════════════════════════════════════

def build_coinbase_tx(coinb1: str, extra_nonce1: str,
                      extra_nonce2: str, coinb2: str) -> bytes:
    """
    بناء معاملة Coinbase الكاملة:
    coinbase = coinb1 + extra_nonce1 + extra_nonce2 + coinb2
    """
    return bytes.fromhex(coinb1 + extra_nonce1 + extra_nonce2 + coinb2)


def build_merkle_root(coinbase_tx: bytes, branches: list[str]) -> bytes:
    """
    ✔ الحساب الصحيح لجذر Merkle

    1. نجزّئ معاملة coinbase بـ Double-SHA256 ← هاش المعاملة
    2. نمشي على الفروع بالترتيب:
       merkle = SHA256d(current_hash + branch)
    """
    current = sha256d(coinbase_tx)  # هاش coinbase كـ bytes

    for branch_hex in branches:
        branch = bytes.fromhex(branch_hex)
        # الترتيب الصحيح: current || branch
        current = sha256d(current + branch)

    return current  # جذر Merkle كـ bytes


# ═══════════════════════════════════════════════════════
#  بناء رأس البلوك (Block Header)
# ═══════════════════════════════════════════════════════

def build_block_header(version_hex: str,
                       prev_hash_hex: str,
                       merkle_root: bytes,
                       ntime_hex: str,
                       nbits_hex: str,
                       nonce: int) -> bytes:
    """
    بناء رأس البلوك (80 بايت) بالترتيب الصحيح:

    [version 4B LE] [prev_hash 32B LE] [merkle_root 32B LE]
    [ntime 4B LE]   [nbits 4B LE]      [nonce 4B LE]

    ملاحظة: التجمع يُرسل prev_hash بـ little-endian جاهزاً
             لكن version/ntime/nbits بـ big-endian → نقلبها
    """
    # الإصدار: يصل big-endian، نحوّله لـ LE
    version_bytes    = bytes.fromhex(version_hex)[::-1]

    # الهاش السابق: يصل من التجمع مقلوباً بالفعل (Stratum convention)
    prev_hash_bytes  = bytes.fromhex(prev_hash_hex)

    # جذر Merkle: نقلبه لـ little-endian
    merkle_root_le   = merkle_root[::-1]

    # الوقت: يصل big-endian → LE
    ntime_bytes      = bytes.fromhex(ntime_hex)[::-1]

    # الهدف المضغوط: يصل big-endian → LE
    nbits_bytes      = bytes.fromhex(nbits_hex)[::-1]

    # nonce: 4 بايت LE
    nonce_bytes      = pack_uint32_le(nonce)

    header = (version_bytes + prev_hash_bytes + merkle_root_le +
              ntime_bytes   + nbits_bytes      + nonce_bytes)

    assert len(header) == 80, f"حجم الرأس خاطئ: {len(header)}"
    return header


def hash_block_header(header: bytes) -> int:
    """
    تجزئة رأس البلوك وإعادة الناتج كعدد صحيح (big-endian)
    للمقارنة مع الهدف
    """
    h = sha256d(header)
    # الناتج little-endian → نقلبه للمقارنة الصحيحة
    return int.from_bytes(h[::-1], "big")


# ═══════════════════════════════════════════════════════
#  اتصال Stratum
# ═══════════════════════════════════════════════════════

class StratumClient:
    """يتولى الاتصال بالتجمع وإرسال/استقبال رسائل Stratum"""

    def __init__(self, host: str, port: int):
        self.host   = host
        self.port   = port
        self.sock   = None
        self._buf   = b""
        self._lock  = threading.Lock()
        self._msg_id = 0

    # ─── الاتصال ───────────────────────────────────────

    def connect(self) -> bool:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=30)
            self.sock.settimeout(10)
            log.info(f"متصل بالتجمع: {self.host}:{self.port}")
            return True
        except Exception as e:
            log.error(f"فشل الاتصال: {e}")
            return False

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    # ─── إرسال ─────────────────────────────────────────

    def send(self, method: str, params: list) -> int:
        with self._lock:
            self._msg_id += 1
            msg = {"id": self._msg_id, "method": method, "params": params}
            try:
                self.sock.sendall((json.dumps(msg) + "\n").encode())
            except Exception as e:
                log.error(f"خطأ في الإرسال: {e}")
            return self._msg_id

    # ─── استقبال ───────────────────────────────────────

    def recv_messages(self) -> list[dict]:
        """استقبال كل الرسائل المتاحة في البفر"""
        messages = []
        try:
            chunk = self.sock.recv(8192)
            if chunk:
                self._buf += chunk
        except socket.timeout:
            pass
        except Exception as e:
            log.warning(f"خطأ في الاستقبال: {e}")
            return messages

        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return messages


# ═══════════════════════════════════════════════════════
#  حالة المهمة الحالية (Job State)
# ═══════════════════════════════════════════════════════

class JobState:
    """تخزن حالة مهمة التعدين الحالية وتدعم الوصول المتزامن"""

    def __init__(self):
        self._lock      = threading.Lock()
        self.job        = None          # القاموس الكامل للمهمة
        self.extra_nonce1      = ""
        self.extra_nonce2_size = 4
        self.target     = 0             # الهدف كعدد صحيح
        self.difficulty = 1.0
        self._extra_nonce2_counter = 0

    def update_job(self, job: dict, en1: str, en2_size: int, target: int):
        with self._lock:
            self.job               = job
            self.extra_nonce1      = en1
            self.extra_nonce2_size = en2_size
            self.target            = target

    def update_difficulty(self, diff: float):
        with self._lock:
            self.difficulty = diff
            self.target     = difficulty_to_target(diff)
            log.info(f"صعوبة جديدة: {diff:.6f}")

    def get_next_extra_nonce2(self) -> str:
        """يولّد extra_nonce2 فريداً لكل خيط"""
        with self._lock:
            val = self._extra_nonce2_counter
            self._extra_nonce2_counter += 1
        # تحويل إلى hex بالحجم المطلوب (little-endian)
        return val.to_bytes(self.extra_nonce2_size, "little").hex()

    @property
    def has_job(self) -> bool:
        with self._lock:
            return self.job is not None

    def snapshot(self):
        """نسخة ثابتة من الحالة للاستخدام في خيط التعدين"""
        with self._lock:
            if self.job is None:
                return None
            return {
                "job":          dict(self.job),
                "extra_nonce1": self.extra_nonce1,
                "en2_size":     self.extra_nonce2_size,
                "target":       self.target,
            }


# ═══════════════════════════════════════════════════════
#  خيط التعدين (Mining Thread)
# ═══════════════════════════════════════════════════════

class MiningThread(threading.Thread):
    """
    خيط مستقل يُجرّب nonces باستمرار.
    عند إيجاد حل يضعه في result_queue.
    """

    def __init__(self, thread_id: int, state: JobState,
                 result_queue: queue.Queue, stats: dict):
        super().__init__(daemon=True, name=f"Miner-{thread_id}")
        self.thread_id    = thread_id
        self.state        = state
        self.result_queue = result_queue
        self.stats        = stats   # قاموس مشترك لإحصاء الهاشات
        self._stop_event  = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            snap = self.state.snapshot()
            if snap is None:
                time.sleep(0.2)
                continue

            self._mine(snap)

    def _mine(self, snap: dict):
        job    = snap["job"]
        en1    = snap["extra_nonce1"]
        target = snap["target"]

        # احصل على extra_nonce2 فريد
        en2_hex = self.state.get_next_extra_nonce2()

        # بناء coinbase وجذر Merkle
        coinbase = build_coinbase_tx(
            job["coinb1"], en1, en2_hex, job["coinb2"]
        )
        merkle_root = build_merkle_root(coinbase, job["merkle_branches"])

        # حفظ نسخة للمقارنة السريعة
        current_job_id = job["job_id"]

        for nonce in range(NONCE_BATCH):
            # تحقق إن تغيرت المهمة
            if self.state.job is not None and \
               self.state.job.get("job_id") != current_job_id:
                return  # مهمة قديمة، ابدأ من جديد

            if self._stop_event.is_set():
                return

            # بناء رأس البلوك وتجزئته
            header = build_block_header(
                job["version"],
                job["prev_hash"],
                merkle_root,
                job["ntime"],
                job["nbits"],
                nonce
            )
            h_int = hash_block_header(header)

            # إحصاء الهاشات
            self.stats["hashes"] = self.stats.get("hashes", 0) + 1

            # هل وجدنا حلاً؟
            if h_int < target:
                result = {
                    "job_id":    job["job_id"],
                    "en2_hex":   en2_hex,
                    "ntime_hex": job["ntime"],
                    "nonce":     nonce,
                    "hash_hex":  sha256d(header).hex(),
                    "hash_int":  h_int,
                }
                self.result_queue.put(result)
                log.info(f"[🎉 خيط {self.thread_id}] وجد حلاً! Nonce={nonce}")
                return


# ═══════════════════════════════════════════════════════
#  المعدّن الرئيسي
# ═══════════════════════════════════════════════════════

class MinerHero:
    """
    يربط كل المكونات معاً:
    - يتصل بالتجمع عبر StratumClient
    - يُحدّث JobState عند وصول مهام جديدة
    - يُشغّل خيوط التعدين
    - يُرسل الحلول للتجمع
    - يُعيد الاتصال عند الانقطاع
    """

    def __init__(self):
        self.client       = StratumClient(POOL_HOST, POOL_PORT)
        self.state        = JobState()
        self.result_queue = queue.Queue()
        self.stats        = {"hashes": 0, "solutions": 0, "rejected": 0}
        self.threads      : list[MiningThread] = []
        self.start_time   = None
        self._running     = True
        self._last_ping   = 0

    # ─── اشتراك وتفويض ─────────────────────────────────

    def _handshake(self) -> bool:
        """إرسال subscribe + authorize والتحقق من النتيجة"""
        # Subscribe
        sub_id = self.client.send("mining.subscribe",
                                  [f"MinerHero/2.0/{BTC_ADDRESS}"])
        time.sleep(1)
        msgs = self.client.recv_messages()

        sub_ok = False
        for m in msgs:
            if m.get("id") == sub_id and isinstance(m.get("result"), list):
                res = m["result"]
                # res[0] = [[sub_type, id], ...], res[1]=en1, res[2]=en2_size
                self.state.extra_nonce1      = res[1]
                self.state.extra_nonce2_size = res[2]
                log.info(f"Subscribed | extra_nonce1={res[1]} | en2_size={res[2]}")
                sub_ok = True

        if not sub_ok:
            log.error("فشل الاشتراك (subscribe)")
            return False

        # Authorize
        auth_id = self.client.send("mining.authorize",
                                   [WORKER_NAME, WORKER_PASS])
        time.sleep(0.5)
        msgs = self.client.recv_messages()

        for m in msgs:
            if m.get("id") == auth_id:
                if m.get("result") is True:
                    log.info(f"العامل مُفوَّض: {WORKER_NAME}")
                    return True
                else:
                    log.error(f"رُفض التفويض: {m.get('error')}")
                    return False

        # قد يصل الرد لاحقاً - نعتبر النجاح مبدئياً
        log.info("في انتظار تأكيد التفويض...")
        return True

    # ─── تشغيل خيوط التعدين ────────────────────────────

    def _start_threads(self):
        for t in self.threads:
            t.stop()
        self.threads.clear()

        for i in range(NUM_THREADS):
            t = MiningThread(i, self.state, self.result_queue, self.stats)
            t.start()
            self.threads.append(t)
        log.info(f"تم تشغيل {NUM_THREADS} خيط تعدين")

    def _stop_threads(self):
        for t in self.threads:
            t.stop()
        self.threads.clear()

    # ─── معالجة الرسائل الواردة ────────────────────────

    def _handle_message(self, msg: dict):
        method = msg.get("method", "")
        params = msg.get("params", [])

        # ── مهمة تعدين جديدة ──
        if method == "mining.notify":
            if len(params) < 9:
                return
            job = {
                "job_id":         params[0],
                "prev_hash":      params[1],
                "coinb1":         params[2],
                "coinb2":         params[3],
                "merkle_branches": params[4],
                "version":        params[5],
                "nbits":          params[6],
                "ntime":          params[7],
                "clean_jobs":     params[8],
            }
            target = nbits_to_target(job["nbits"])
            self.state.update_job(
                job,
                self.state.extra_nonce1,
                self.state.extra_nonce2_size,
                target
            )
            clean = "🔄 تنظيف" if job["clean_jobs"] else "➕ إضافة"
            log.info(f"مهمة جديدة [{clean}]: {job['job_id'][:16]}... | "
                     f"nbits={job['nbits']}")

        # ── تحديث الصعوبة ──
        elif method == "mining.set_difficulty":
            if params:
                self.state.update_difficulty(float(params[0]))

        # ── رد على إرسال حل ──
        elif "result" in msg and "id" in msg:
            if msg["result"] is True:
                log.info("✅ الحل قُبل من التجمع!")
                self.stats["solutions"] = self.stats.get("solutions", 0) + 1
            elif msg.get("error"):
                log.warning(f"❌ الحل رُفض: {msg['error']}")
                self.stats["rejected"] = self.stats.get("rejected", 0) + 1

    # ─── إرسال الحلول ──────────────────────────────────

    def _submit_solution(self, result: dict):
        nonce_hex = hex_uint32_le(result["nonce"])
        log.info(f"📤 إرسال حل | job={result['job_id'][:16]} | "
                 f"nonce={result['nonce']} | hash={result['hash_hex'][:20]}...")
        self.client.send("mining.submit", [
            WORKER_NAME,
            result["job_id"],
            result["en2_hex"],
            result["ntime_hex"],
            nonce_hex,
        ])

    # ─── تقرير الأداء ──────────────────────────────────

    def _print_stats(self):
        elapsed  = time.time() - self.start_time
        hashes   = self.stats.get("hashes", 0)
        hashrate = hashes / elapsed if elapsed > 0 else 0

        if hashrate >= 1_000_000:
            rate_str = f"{hashrate/1_000_000:.2f} MH/s"
        elif hashrate >= 1_000:
            rate_str = f"{hashrate/1_000:.2f} KH/s"
        else:
            rate_str = f"{hashrate:.0f} H/s"

        log.info(
            f"📊 {rate_str} | "
            f"هاشات: {hashes:,} | "
            f"مقبولة: {self.stats.get('solutions',0)} | "
            f"مرفوضة: {self.stats.get('rejected',0)} | "
            f"وقت: {elapsed:.0f}s"
        )

    # ─── الحلقة الرئيسية ───────────────────────────────

    def run(self):
        self._print_banner()
        self.start_time  = time.time()
        last_stats_time  = time.time()
        stats_interval   = 10  # ثانية

        while self._running:
            # ─ اتصال ─
            if not self.client.connect():
                log.warning(f"إعادة المحاولة خلال {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)
                continue

            if not self._handshake():
                self.client.close()
                time.sleep(RECONNECT_DELAY)
                continue

            self._start_threads()

            try:
                while self._running:
                    # استقبال رسائل
                    for msg in self.client.recv_messages():
                        self._handle_message(msg)

                    # إرسال الحلول
                    while not self.result_queue.empty():
                        result = self.result_queue.get_nowait()
                        self._submit_solution(result)

                    # تقرير دوري
                    now = time.time()
                    if now - last_stats_time >= stats_interval:
                        self._print_stats()
                        last_stats_time = now

                    # نبض دوري
                    if now - self._last_ping >= 60:
                        self.client.send("mining.ping", [])
                        self._last_ping = now

                    time.sleep(0.05)

            except KeyboardInterrupt:
                self._running = False
            except Exception as e:
                log.error(f"خطأ في الحلقة الرئيسية: {e}")
                log.info(f"إعادة الاتصال خلال {RECONNECT_DELAY}s...")

            finally:
                self._stop_threads()
                self.client.close()
                if self._running:
                    time.sleep(RECONNECT_DELAY)

        self._print_final_stats()

    # ─── الشاشات التجميلية ─────────────────────────────

    def _print_banner(self):
        print("""
╔══════════════════════════════════════════════════════════╗
║        ⛏️   MINER HERO v2.0 - تعدين بيتكوين حقيقي       ║
║         بروتوكول Stratum | شبكة البيتكوين الرئيسية       ║
╚══════════════════════════════════════════════════════════╝""")
        print(f"  عنوان BTC : {BTC_ADDRESS}")
        print(f"  التجمع    : {POOL_HOST}:{POOL_PORT}")
        print(f"  العامل    : {WORKER_NAME}")
        print(f"  الخيوط    : {NUM_THREADS}")
        print(f"  التاريخ   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("─" * 60)

    def _print_final_stats(self):
        elapsed  = time.time() - self.start_time if self.start_time else 1
        hashes   = self.stats.get("hashes", 0)
        hashrate = hashes / elapsed if elapsed > 0 else 0
        print("\n" + "═" * 60)
        print("🏁 توقف المعدّن")
        print(f"   إجمالي الهاشات : {hashes:,}")
        print(f"   متوسط السرعة   : {hashrate:,.0f} H/s")
        print(f"   الحلول المقبولة : {self.stats.get('solutions', 0)}")
        print(f"   الحلول المرفوضة : {self.stats.get('rejected', 0)}")
        print(f"   وقت التشغيل    : {elapsed:.0f} ثانية")
        print("═" * 60)
        print("ℹ️  احتمالية إيجاد بلوك بـ CPU: 1 من ~6×10¹⁸ (للتعلم فقط)")


# ═══════════════════════════════════════════════════════
#  نقطة الدخول
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    miner = MinerHero()
    try:
        miner.run()
    except KeyboardInterrupt:
        pass

