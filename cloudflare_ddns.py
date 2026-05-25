import sys
import re
import json
import requests
import random
import datetime
import os
import argparse
import fcntl
import logging
import logging.handlers
'''
cloudflareDDNS
Updates Cloudflare A records when the host's public IP changes.
Requires Python 3.7 or later.
Brent Russell  —  www.brentrussell.com

Usage:
    python3 cloudflare_ddns.py /abs/path/to/zones.json
    python3 cloudflare_ddns.py /abs/path/to/zones.json --force-update
    python3 cloudflare_ddns.py /abs/path/to/zones.json --dry-run

Exit codes:
    0 — success (DNS in sync, or no update needed)
    1 — failure (couldn't get IP, couldn't update one or more records, lock held, etc.)
'''

if sys.version_info < (3, 7):
    sys.exit("Please upgrade to Python 3.7 or later")


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "cloudflare_ddns.log")
IP_JSON_PATH = os.path.join(SCRIPT_DIR, "ip.json")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".cloudflare_ddns.lock")
IP_HISTORY_MAX = 50


def _setupLogging():
    log = logging.getLogger("cloudflare_ddns")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S'
    ))
    log.addHandler(handler)
    # Also echo to stdout when run interactively (cron will redirect to /dev/null).
    if sys.stdout.isatty():
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
        log.addHandler(stream)
    return log


log = _setupLogging()


# ----- IP provider lookup -----

IP_PROVIDERS = {
    'https://api.ipify.org?format=json': 'ip',
    'https://ipapi.co/json/': 'ip',
    'https://api.bigdatacloud.net/data/client-ip': 'ipString',
    'https://checkip.amazonaws.com/': '',
    'https://ifconfig.me/ip': ''
}


def validIP(ipString):
    return bool(re.search(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ipString))


def _tryProvider(provider, key):
    headers = {'User-Agent': 'Mozilla/5.0 (cloudflare_ddns)'}
    try:
        r = requests.get(provider, headers=headers, timeout=10)
    except requests.exceptions.RequestException as e:
        log.warning("remoteIP: connection error from %s: %s", provider, e)
        return None
    if not r.content:
        log.warning("remoteIP: no content from %s", provider)
        return None
    try:
        if key:
            data = json.loads(r.content)
            val = data.get(key)
            if val and validIP(val):
                return val.strip()
            log.warning("remoteIP: invalid/missing IP from %s", provider)
            return None
        text = r.text.strip()
        if validIP(text):
            return text
        log.warning("remoteIP: invalid IP text from %s", provider)
        return None
    except json.decoder.JSONDecodeError:
        log.warning("remoteIP: bad json from %s", provider)
        return None


def remoteIP():
    providers = list(IP_PROVIDERS.items())
    random.shuffle(providers)
    for provider, key in providers:
        ip = _tryProvider(provider, key)
        if ip:
            log.info("Public IP %s (from %s)", ip, provider)
            return ip
    log.error("All IP providers failed")
    return None


# ----- ip.json persistence -----

def _migrateLegacy(data):
    if 'history' in data:
        return data
    legacy_date = data.get('date', '')
    history = []
    for key in ('lastip1', 'lastip2', 'lastip3', 'lastip4'):
        ip = data.get(key)
        if ip and (not history or history[-1]['ip'] != ip):
            history.append({"ip": ip, "changed_at": legacy_date})
    return {
        "currentip": data.get('currentip', '0.0.0.0'),
        "current_since": legacy_date,
        "history": history
    }


def readIpJson():
    try:
        with open(IP_JSON_PATH, 'r') as f:
            return _migrateLegacy(json.load(f))
    except FileNotFoundError:
        log.warning("ip.json not found; initializing")
        return {"currentip": "0.0.0.0", "current_since": "", "history": []}
    except (PermissionError, json.decoder.JSONDecodeError) as e:
        log.error("Cannot read ip.json: %s", e)
        return None


def _writeIpJson(data):
    try:
        with open(IP_JSON_PATH, "w") as f:
            f.write(json.dumps(data, sort_keys=True, default=str, indent=4))
        return True
    except (PermissionError, OSError) as e:
        log.error("Cannot write ip.json: %s", e)
        return False


def persistIp(remote_ip):
    """Only call after a successful Cloudflare update."""
    data = readIpJson()
    if data is None:
        return False
    currentip = data.get('currentip', '0.0.0.0')
    if currentip == remote_ip:
        return True
    now = datetime.datetime.now().isoformat(timespec='seconds')
    history = data.get('history', [])
    if not history or history[0].get('ip') != currentip:
        history.insert(0, {"ip": currentip, "changed_at": data.get('current_since', '')})
    history = history[:IP_HISTORY_MAX]
    return _writeIpJson({
        "currentip": remote_ip,
        "current_since": now,
        "history": history
    })


# ----- Cloudflare API -----

def _cfRequest(method, url, headers, payload=None):
    try:
        if method == 'put':
            r = requests.put(url, headers=headers, data=payload, timeout=15)
        else:
            r = requests.get(url, headers=headers, timeout=15)
        return r
    except requests.exceptions.RequestException as e:
        log.error("Cloudflare %s %s failed: %s", method.upper(), url, e)
        return None


def zoneData(headers, zone):
    r = _cfRequest('get', 'https://api.cloudflare.com/client/v4/zones?name=' + zone, headers)
    if r is None:
        return None
    try:
        data = json.loads(r.content)
        results = data.get('result') or []
        if results and results[0].get('id'):
            return results[0]['id']
        log.error("Zone %s not found", zone)
        return None
    except json.decoder.JSONDecodeError:
        log.error("Invalid JSON from Cloudflare zones endpoint for %s", zone)
        return None


def recordData(headers, zone_id, record):
    r = _cfRequest('get', 'https://api.cloudflare.com/client/v4/zones/' + zone_id + '/dns_records?name=' + record, headers)
    if r is None:
        return None
    try:
        data = json.loads(r.content)
        results = data.get('result') or []
        if results and results[0].get('id'):
            return results[0]['id']
        log.error("Record %s not found", record)
        return None
    except json.decoder.JSONDecodeError:
        log.error("Invalid JSON from Cloudflare records endpoint for %s", record)
        return None


def updateRecord(headers, zone_id, record, record_id, remote_ip, proxied_state):
    payload = json.dumps(dict(type="A", name=record, content=remote_ip, ttl=1, proxied=proxied_state))
    r = _cfRequest('put', 'https://api.cloudflare.com/client/v4/zones/' + zone_id + '/dns_records/' + record_id, headers, payload)
    if r is None:
        return 'request failed'
    try:
        data = json.loads(r.content)
    except json.decoder.JSONDecodeError:
        return 'invalid json response'
    if data.get('success'):
        return 'success'
    errors = data.get('errors') or []
    if errors and isinstance(errors[0], dict) and 'message' in errors[0]:
        return errors[0]['message']
    return 'unknown error from Cloudflare api'


# ----- Config / CLI -----

CONFIG_DEFAULTS = ['', '', '', '', False, True]
CONFIG_KEYS = {
    'zone': 0,
    'record': 1,
    'global_api_key': 2,
    'cloudflare_email': 3,
    'proxied_state': 4,
    'enabled': 5,
}


def loadConfig(path):
    if not os.path.isfile(path):
        log.error("Config file does not exist: %s", path)
        return None
    try:
        with open(path, 'r') as f:
            zoneFilejson = json.load(f)
    except (PermissionError, FileNotFoundError, json.decoder.JSONDecodeError) as e:
        log.error("Cannot read config %s: %s", path, e)
        return None
    out = []
    for item in zoneFilejson:
        row = list(CONFIG_DEFAULTS)
        for key, idx in CONFIG_KEYS.items():
            if key in item:
                row[idx] = item[key]
        if not row[0] or not row[2] or not row[3]:
            log.error("Skipping malformed config entry: %s", item)
            continue
        out.append(row)
    return out


def parseArgs():
    parser = argparse.ArgumentParser(description='Cloudflare DDNS updater')
    parser.add_argument('configFile', type=str, help='Path to the zones JSON config (absolute path recommended for cron)')
    parser.add_argument('--force-update', action='store_true', help='Force update even if IP has not changed')
    parser.add_argument('--dry-run', action='store_true', help='Check IP and records but do not PUT to Cloudflare')
    return parser.parse_args()


# ----- Main -----

def _acquireLock():
    """Returns the open lock file handle, or None if another run holds it."""
    f = open(LOCK_PATH, 'w')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def run(args):
    config = loadConfig(args.configFile)
    if not config:
        log.error("No usable config entries; aborting")
        return 1

    remote_ip = remoteIP()
    if not remote_ip:
        return 1

    data = readIpJson()
    if data is None:
        return 1
    currentip = data.get('currentip', '0.0.0.0')
    needs_update = args.force_update or (currentip != remote_ip)

    if not needs_update:
        log.info("No update needed. Public IP %s unchanged.", remote_ip)
        return 0

    if args.dry_run:
        log.info("DRY RUN — would update %d record(s) to %s", len(config), remote_ip)
        return 0

    total_updates = 0
    total_errors = 0
    for zone, record, api_key, email, proxied, enabled in config:
        if not enabled:
            log.info("%s: not enabled, skipping", record)
            continue
        if record in ('', '@', '.'):
            record = zone
        headers = {
            'Content-Type': 'application/json',
            'X-Auth-Key': api_key,
            'X-Auth-Email': email,
        }
        zone_id = zoneData(headers, zone)
        if not zone_id:
            total_errors += 1
            continue
        record_id = recordData(headers, zone_id, record)
        if not record_id:
            total_errors += 1
            continue
        result = updateRecord(headers, zone_id, record, record_id, remote_ip, proxied)
        if result == 'success':
            log.info("Updated %s -> %s", record, remote_ip)
            total_updates += 1
        elif result and re.search('already exists', result, flags=re.IGNORECASE):
            log.info("%s already at %s", record, remote_ip)
            total_updates += 1
        else:
            log.error("Failed to update %s -> %s: %s", record, remote_ip, result)
            total_errors += 1

    log.info("Results: %d updated, %d error(s)", total_updates, total_errors)

    if total_errors == 0 and total_updates > 0:
        if not persistIp(remote_ip):
            return 1
        return 0
    # Any errors → leave ip.json alone so next run retries, and signal failure.
    return 1


def main():
    args = parseArgs()
    lock = _acquireLock()
    if lock is None:
        log.warning("Another cloudflare_ddns run is in progress; exiting")
        sys.exit(1)
    try:
        rc = run(args)
    except Exception:
        log.exception("Unhandled exception")
        rc = 1
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    sys.exit(rc)


if __name__ == '__main__':
    main()
