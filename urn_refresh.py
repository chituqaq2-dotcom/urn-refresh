#!/usr/bin/env python3
"""URnetwork residential pool refresher (shared by Hark task and GitHub Actions).

Keeps up to 5 healthy, clean US home-broadband URnetwork SOCKS exits and publishes the
rendered subscription into Cloudflare Workers KV; Worker 'urn-sub' (urn.08010613.xyz/<key>)
serves it from KV. Node state (client_id + auth tokens) lives only in KV key 'state'.
Never prints tokens.

Env:
  CF_ACCOUNT_ID, KV_NAMESPACE_ID     plain config
  CF_API_TOKEN                       optional; if unset, no auth header (Hark injects on the wire)
  URNETWORK_API_KEY                  optional; same
  MAX_NEW                            new candidates tested per run (default 15)
Flags: --seed-from-local  (one-time: import ./state.json + node_*.json into KV if KV empty)
       --dry-run          (test & report only; no auth-client/remove-client/KV writes)
"""
import json, os, subprocess, sys, base64, time, random, concurrent.futures as cf

A = "https://api.bringyour.com"; C = "https://api.cloudflare.com/client/v4"
AC = os.environ.get("CF_ACCOUNT_ID", "b82cafb09666351f559a8d6a079cabe4")
NS = os.environ.get("KV_NAMESPACE_ID", "01ab3fdf7b5047259e57fec9c298cbdd")
CFT = os.environ.get("CF_API_TOKEN", "").strip(); URT = os.environ.get("URNETWORK_API_KEY", "").strip()
TARGET = int(os.environ.get("TARGET", "1")); MIN_BPS = 60_000; MAX_NEW = int(os.environ.get("MAX_NEW", "8"))
DRY = "--dry-run" in sys.argv
KVB = f"{C}/accounts/{AC}/storage/kv/namespaces/{NS}/values/"
ABBR = {"AT&T": "ATT", "Charter": "Spectrum", "Spectrum": "Spectrum", "Comcast": "Comcast", "Verizon": "Verizon",
        "Cox": "Cox", "Frontier": "Frontier", "CenturyLink": "CenturyLink", "Lumen": "CenturyLink", "T-Mobile": "TMobile",
        "Optimum": "Optimum", "Altice": "Optimum", "Windstream": "Windstream", "Mediacom": "Mediacom", "Google Fiber": "GoogleFiber"}
FIELDS = ("provider", "client_id", "auth_token", "proxy_host", "socks_proxy_port", "ip", "bps", "city", "region", "isp", "as", "added")

def sh(args, timeout=60, inp=None):
    try: return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=inp).stdout
    except Exception: return ""

def api(path, body=None):
    a = ["curl", "-s", "-m", "30", A + path, "-H", "Content-Type: application/json"]
    if URT: a += ["-H", "Authorization: Bearer " + URT]
    if body is not None: a += ["-X", "POST", "-d", json.dumps(body)]
    try: return json.loads(sh(a) or "{}")
    except Exception: return {}

def cfh(): return ["-H", "Authorization: Bearer " + CFT] if CFT else []

def kv_get(key):
    out = sh(["curl", "-s", "-m", "30", "-w", "\n%{http_code}", KVB + key, *cfh()])
    body, _, code = out.rpartition("\n")
    return body if code == "200" else None

def kv_put(key, value):
    if DRY: return True
    out = sh(["curl", "-s", "-m", "30", "-X", "PUT", KVB + key, *cfh(), "-H", "Content-Type: text/plain", "--data-binary", "@-"], inp=value)
    try: return bool(json.loads(out).get("success"))
    except Exception: return False

def seed_from_local():
    st = []
    for n in json.load(open("state.json")):
        d = json.load(open(n["file"])); r = d["proxy_config_result"]
        st.append({**{k: n.get(k) for k in FIELDS}, "client_id": d["client_id"], "auth_token": r["auth_token"],
                   "proxy_host": r["proxy_host"], "socks_proxy_port": r["socks_proxy_port"], "added": int(time.time())})
    return st

def test(n):
    X = ["--socks5-hostname", f'{n["proxy_host"]}:{n["socks_proxy_port"]}', "--proxy-user", n["auth_token"] + ":"]
    ips = []; info = None
    for _ in range(3):
        try: j = json.loads(sh(["curl", "-s", "-m", "15", *X, "http://ip-api.com/json/?fields=status,query,city,regionName,countryCode,isp,org,as,proxy,hosting,mobile"], 20) or "{}")
        except Exception: j = {}
        if j.get("status") == "success": ips.append(j["query"]); info = j
        time.sleep(2)
    g = sh(["curl", "-s", "-m", "10", *X, "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/generate_204"], 15)
    try: bps = float(sh(["curl", "-s", "-m", "20", *X, "-o", "/dev/null", "-w", "%{speed_download}", "https://speed.cloudflare.com/__down?bytes=3000000"], 25) or 0)
    except Exception: bps = 0
    ok = (len(ips) >= 2 and len(set(ips)) == 1  # 2026-11-26: 2 of 3 lookups enough (home links jitter)
          and info and info.get("countryCode") == "US" and info.get("proxy") is False
          and info.get("hosting") is False and info.get("mobile") is False and g == "204" and bps >= MIN_BPS)
    n.update({"ok": bool(ok), "ip": ips[-1] if ips else n.get("ip"), "bps": int(bps), "g204": g})
    if info: n.update({"city": info.get("city"), "region": info.get("regionName"), "isp": info.get("isp"), "as": info.get("as")})
    return n

def remove(n):
    if not DRY and n.get("client_id"): api("/network/remove-client", {"client_id": n["client_id"]})

def candidates(exclude):
    loc = api("/network/find-provider-locations", {"query": "United States"})
    small = [l["location_id"] for l in loc.get("locations", []) if l.get("country_code") == "us" and l.get("location_type") == "city" and l.get("provider_count", 99) <= 5]
    random.shuffle(small)
    pr = api("/network/find-providers2", {"specs": [{"location_id": i} for i in small[:120]], "count": 120}).get("providers", [])
    pr = [p for p in pr if p["client_id"] not in exclude and p.get("location", {}).get("country_code") == "us" and p.get("tier", 99) <= 3]
    pr.sort(key=lambda p: -p.get("estimated_bytes_per_second", 0))
    return pr

def name(n):
    isp = n.get("isp") or ""; tag = next((v for k, v in ABBR.items() if k.lower() in isp.lower()), "".join(ch for ch in isp.split()[0] if ch.isalnum()) if isp else "ISP")
    city = "".join(w.capitalize() for w in (n.get("city") or "US").replace("-", " ").split())
    return f"URnetwork-US-{city}-{tag}-Clean"

def render(nodes):
    plain = []; b64 = []; clash = []; names = []; seen = set()
    for n in nodes:
        t, h, p = n["auth_token"], n["proxy_host"], n["socks_proxy_port"]
        nm = name(n); k = 2
        while nm in seen: nm = f"{name(n)}{k}"; k += 1
        seen.add(nm); names.append(nm)
        plain.append(f"socks://{t}:@{h}:{p}#{nm}")
        b64.append(f"socks://{base64.urlsafe_b64encode((t+':').encode()).decode().rstrip('=')}@{h}:{p}#{nm}")
        clash.append(f'  - {{name: "{nm}", type: socks5, server: {h}, port: {p}, username: "{t}", password: "", udp: false}}')
    ns = ", ".join(f'"{x}"' for x in names)
    yaml = ("mixed-port: 7890\nallow-lan: false\nmode: rule\nlog-level: info\nproxies:\n" + "\n".join(clash) +
            f"\nproxy-groups:\n  - {{name: PROXY, type: select, proxies: [{ns}, DIRECT]}}\nrules:\n  - GEOIP,LAN,DIRECT\n  - GEOIP,CN,DIRECT\n  - MATCH,PROXY\n")
    return {"plain": "\n".join(plain) + "\n", "sr": base64.b64encode("\n".join(b64).encode()).decode(), "clash": yaml}, names

def save_state(nodes):
    return kv_put("state", json.dumps([{k: n.get(k) for k in FIELDS} for n in nodes]))

def publish(nodes):
    out, names = render(nodes)
    ok = all(kv_put(k, v) for k, v in out.items())
    kv_put("updated", json.dumps({"ts": int(time.time()), "by": os.environ.get("RUNNER", "unknown"), "count": len(nodes)}))
    return ok, names

def main():
    raw = kv_get("state")
    if raw is None:
        if "--seed-from-local" in sys.argv: state = seed_from_local()
        else: print(json.dumps({"error": "KV state missing or KV unreachable; refusing to run (would orphan clients)"})); sys.exit(2)
    else: state = json.loads(raw)
    lock = kv_get("lock")
    if lock and time.time() - float(lock) < 1200 and "--force" not in sys.argv:
        print(json.dumps({"skipped": "another run holds the lock (<20 min old)"})); return
    kv_put("lock", str(time.time()))
    try:
        with cf.ThreadPoolExecutor(8) as ex: state = list(ex.map(test, state))
        keep = [n for n in state if n["ok"]]; dropped = [n for n in state if not n["ok"]]
        for n in dropped: remove(n)
        if dropped: save_state(keep); time.sleep(10)
        new_tested = []; tried = 0; exclude = {n["provider"] for n in state}
        if len(keep) < TARGET and not DRY:
            pool = candidates(exclude)
            while len(keep) < TARGET and tried < MAX_NEW and pool:
                batch = []
                while pool and len(batch) < min(TARGET - len(keep), MAX_NEW - tried, 5):
                    p = pool.pop(0)
                    r = api("/network/auth-client", {"description": "hark-urn", "device_spec": "hark",
                            "proxy_config": {"initial_device_state": {"location": {"connect_location_id": {"client_id": p["client_id"]}}}}})
                    tried += 1; pc = r.get("proxy_config_result") or {}
                    if r.get("error") or not pc.get("socks_proxy_port"):
                        msg = (r.get("error") or {}).get("message", "no socks")
                        new_tested.append({"provider": p["client_id"], "ok": False, "err": msg})
                        if "a minute" in msg: pool.insert(0, p); time.sleep(65); continue   # free plan: 5 new proxies/min
                        if "limit of" in msg: pool = []; break                              # free plan active-proxy cap reached
                        continue
                    batch.append({"provider": p["client_id"], "client_id": r.get("client_id") or pc.get("client_id"), "auth_token": pc["auth_token"],
                                  "proxy_host": pc["proxy_host"], "socks_proxy_port": pc["socks_proxy_port"], "added": int(time.time())})
                save_state(keep + batch)  # crash-safe: pending clients are recorded so the next run retests/removes them
                time.sleep(5)
                with cf.ThreadPoolExecutor(8) as ex: batch = list(ex.map(test, batch))
                for n in batch:
                    new_tested.append(n)
                    if n["ok"]: keep.append(n)
                    else: remove(n)
        keep = sorted(keep, key=lambda n: -n["bps"])
        for n in keep[TARGET:]: remove(n)
        keep = keep[:TARGET]
        save_state(keep)
        changed = bool(dropped) or any(n.get("ok") for n in new_tested) or "--publish" in sys.argv or kv_get("plain") is None
        pub = publish(keep) if (changed and keep) else None
    finally:
        if not DRY: sh(["curl", "-s", "-m", "30", "-X", "DELETE", KVB + "lock", *cfh()])
    clean = lambda n: {k: n.get(k) for k in ("ok", "ip", "city", "isp", "bps", "g204", "err")}
    print(json.dumps({"kept": [clean(n) for n in keep], "dropped": [clean(n) for n in dropped], "new_tested": [clean(n) for n in new_tested],
                      "published": pub, "count": len(keep), "dry_run": DRY}, ensure_ascii=False, indent=1))
    if not keep: sys.exit(1)

if __name__ == "__main__": main()
