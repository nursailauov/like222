from flask import Flask, request, jsonify, render_template
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import aiohttp
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import time
from collections import defaultdict
from datetime import datetime
import random
import os
import urllib.parse

app = Flask(__name__)

KEY_LIMIT = 100          # change to e.g. 500 if you want more likes per IP per day
JWT_CACHE_TTL = 6 * 60 * 60
ACCOUNT_CACHE_TTL = 60
VALID_SERVERS = ["CIS", "BR", "US", "SAC", "NA", "BD", "RU"]
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=24, connect=8, sock_read=16)
LIKE_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4, sock_read=5)
tracker = defaultdict(lambda: [0, time.time()])
liked_cache = defaultdict(set)
jwt_cache = {}
account_cache = {}

COMMON_HEADERS = {
    'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
    'Content-Type': "application/x-www-form-urlencoded",
    'X-GA': "v1 1",
    'ReleaseVersion': "OB53"
}


def get_today_midnight_timestamp():
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return midnight.timestamp()


def get_region_filename(server_name):
    """Return filename based on server region"""
    if server_name == "CIS":
        return "account_cis.txt"
    if server_name in {"BR", "US", "SAC", "NA"}:
        return "account_br.txt"
    return "account_bd.txt"


def read_accounts_file(filename):
    accounts = []
    with open(filename, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' in line:
                uid, pwd = line.split(':', 1)
                uid = uid.strip()
                pwd = pwd.strip()
                if uid and pwd:
                    accounts.append({"uid": uid, "password": pwd})
    return accounts


def load_accounts(server_name):
    filename = get_region_filename(server_name)
    if not os.path.exists(filename):
        print(f"WARNING: {filename} not found, creating empty.")
        open(filename, 'w').close()

    now = time.time()
    mtime = os.path.getmtime(filename)
    cached = account_cache.get(filename)
    if cached:
        cached_mtime, expires_at, accounts = cached
        if cached_mtime == mtime and expires_at > now:
            return accounts

    accounts = read_accounts_file(filename)
    account_cache[filename] = (mtime, now + ACCOUNT_CACHE_TTL, accounts)
    return accounts


def clear_account_cache(server_name=None):
    if server_name:
        account_cache.pop(get_region_filename(server_name), None)
    else:
        account_cache.clear()


def save_account_to_file(uid, password, server_name):
    """Append uid:password to the correct region file"""
    filename = get_region_filename(server_name)
    with open(filename, "a") as f:
        f.write(f"{uid}:{password}\n")
    jwt_cache.pop((uid, password), None)
    account_cache.pop(filename, None)
    return filename


def get_cached_jwt(uid, password):
    cached = jwt_cache.get((uid, password))
    if not cached:
        return None

    token, expires_at = cached
    if expires_at <= time.time():
        jwt_cache.pop((uid, password), None)
        return None
    return token


async def generate_jwt_token(uid, password, session):
    cached_token = get_cached_jwt(uid, password)
    if cached_token:
        return cached_token

    try:
        encoded_password = urllib.parse.quote(password)
        url = f"https://jwt-henna.vercel.app/guest?uid={uid}&password={encoded_password}"
        async with session.get(url, timeout=HTTP_TIMEOUT) as response:
            if response.status == 200:
                data = await response.json(content_type=None)
                token = data.get('jwt_token') or data.get('token') if isinstance(data, dict) else None
                if token:
                    jwt_cache[(uid, password)] = (token, time.time() + JWT_CACHE_TTL)
                    return token
        return None
    except Exception:
        return None


def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_message = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded_message)).decode('utf-8')


def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()


async def send_like(encrypted_uid, token, url, session):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {**COMMON_HEADERS, 'Authorization': f"Bearer {token}"}
        async with session.post(url, data=edata, headers=headers, timeout=LIKE_TIMEOUT) as response:
            return response.status
    except Exception:
        return 500


async def process_account(target_uid, encrypted_uid, account, url, semaphore, session):
    async with semaphore:
        token = await generate_jwt_token(account['uid'], account['password'], session)
        if not token:
            return 500, account['uid']
        status = await send_like(encrypted_uid, token, url, session)
        if status == 200:
            liked_cache[target_uid].add(account['uid'])
        return status, account['uid']


async def send_all_likes(target_uid, server_name, url, session):
    protobuf_message = create_protobuf_message(target_uid, server_name)
    encrypted_uid = encrypt_message(protobuf_message)
    accounts = load_accounts(server_name)
    if not accounts:
        return {'success': 0, 'failed': 0, 'total': 0, 'already_liked': 0}

    already_liked = liked_cache.get(target_uid, set())
    fresh_accounts = [acc for acc in accounts if acc['uid'] not in already_liked]

    if not fresh_accounts:
        return {'success': 0, 'failed': 0, 'total': len(accounts), 'already_liked': len(already_liked), 'fresh_used': 0}

    random.shuffle(fresh_accounts)
    semaphore = asyncio.Semaphore(30)
    tasks = [process_account(target_uid, encrypted_uid, acc, url, semaphore, session) for acc in fresh_accounts]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    successful = sum(1 for r in results if isinstance(r, tuple) and r[0] == 200)
    failed = len(results) - successful
    return {
        'success': successful,
        'failed': failed,
        'total': len(accounts),
        'already_liked': len(already_liked),
        'fresh_used': len(fresh_accounts)
    }


def enc(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return encrypt_message(message.SerializeToString())


def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except Exception:
        return None


def get_player_info_url(server_name):
    if server_name in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    return "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"


def get_like_url(server_name):
    if server_name in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/LikeProfile"
    return "https://clientbp.ggpolarbear.com/LikeProfile"


async def get_player_info_async(encrypted_uid, server_name, token, session):
    edata = bytes.fromhex(encrypted_uid)
    headers = {**COMMON_HEADERS, 'Authorization': f"Bearer {token}"}
    try:
        async with session.post(get_player_info_url(server_name), data=edata, headers=headers, timeout=HTTP_TIMEOUT) as response:
            return decode_protobuf(await response.read())
    except Exception:
        return None


async def generate_check_token(accounts, session):
    tasks = [generate_jwt_token(acc['uid'], acc['password'], session) for acc in accounts[:5]]
    for task in asyncio.as_completed(tasks):
        token = await task
        if token:
            return token
    return None


async def handle_like_request(uid, server_name):
    accounts = load_accounts(server_name)
    if not accounts:
        accounts = load_accounts("CIS")

    connector = aiohttp.TCPConnector(limit=80, limit_per_host=40, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        check_token = await generate_check_token(accounts, session)
        if not check_token:
            return jsonify({"error": "Token generation failed"}), 500

        encrypted_uid = enc(uid)
        before = await get_player_info_async(encrypted_uid, server_name, check_token, session)
        if before is None:
            return jsonify({"error": "Invalid UID or server"}), 200

        try:
            before_data = json.loads(MessageToJson(before))
            before_like = int(before_data['AccountInfo'].get('Likes', 0))
        except Exception:
            return jsonify({"error": "Data parsing failed"}), 200

        result = await send_all_likes(uid, server_name, get_like_url(server_name), session)

        after = await get_player_info_async(encrypted_uid, server_name, check_token, session)
        if after is None:
            return jsonify({"error": "Could not verify after likes"}), 200

    try:
        after_data = json.loads(MessageToJson(after))
        after_like = int(after_data['AccountInfo']['Likes'])
        player_name = str(after_data['AccountInfo']['PlayerNickname'])
        player_id = int(after_data['AccountInfo']['UID'])
        like_given = after_like - before_like
        return {
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": player_name,
            "UID": player_id,
            "status": 1 if like_given > 0 else 2,
            "accounts_used": result['success']
        }
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/like', methods=['GET'])
def handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()
    key = request.args.get("key", "nur")
    client_ip = request.remote_addr

    if key != "nur":
        return jsonify({"error": "Invalid or missing API key"}), 403
    if not uid or not server_name:
        return jsonify({"error": "uid and server_name required"}), 400

    if server_name not in VALID_SERVERS:
        return jsonify({"error": f"Invalid server. Use: {VALID_SERVERS}"}), 400

    today_midnight = get_today_midnight_timestamp()
    count, last_reset = tracker[client_ip]
    if last_reset < today_midnight:
        tracker[client_ip] = [0, time.time()]
        count = 0
    if count >= KEY_LIMIT:
        return jsonify({"error": "Daily limit reached", "remains": f"(0/{KEY_LIMIT})"}), 429

    result = asyncio.run(handle_like_request(uid, server_name))
    if isinstance(result, tuple):
        return result

    if result["LikesGivenByAPI"] > 0:
        tracker[client_ip][0] += 1

    remains = KEY_LIMIT - tracker[client_ip][0]
    result["remains"] = f"({remains}/{KEY_LIMIT})"
    return jsonify(result)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/token_info', methods=['GET'])
def token_info():
    data = {}
    for srv in VALID_SERVERS:
        count = len(load_accounts(srv))
        data[srv] = {"regular_tokens": count, "visit_tokens": 0}
    return jsonify(data)


@app.route('/add_account', methods=['GET', 'POST'])
def add_account():
    """Add a new uid:password to the correct region file.
    Usage: /add_account?uid=123456&pass=xyz&region=RU&key=nur
    """
    key = request.args.get("key") or (request.json.get("key") if request.is_json else None)
    if key != "nur":
        return jsonify({"error": "Invalid key"}), 403

    if request.method == "GET":
        uid = request.args.get("uid")
        password = request.args.get("pass")
        region = request.args.get("region", "").upper()
    else:
        data = request.get_json()
        uid = data.get("uid") if data else None
        password = data.get("pass") if data else None
        region = data.get("region", "").upper() if data else None

    if not uid or not password or not region:
        return jsonify({"error": "Missing uid, pass, or region"}), 400

    if region not in VALID_SERVERS:
        return jsonify({"error": f"Invalid region. Use: {VALID_SERVERS}"}), 400

    filename = save_account_to_file(uid, password, region)
    return jsonify({
        "status": "success",
        "message": f"Account {uid}:{password} added to {filename}",
        "region": region
    })


@app.route('/reset-cache', methods=['GET'])
def reset_cache():
    key = request.args.get("key")
    if key != "nur":
        return jsonify({"error": "Invalid key"}), 403
    liked_cache.clear()
    jwt_cache.clear()
    clear_account_cache()
    return jsonify({"message": "Cache cleared", "credit": "@NUR_SAILAUOV"})


if __name__ == '__main__':
    print("Smart Like API with Account Manager")
    print("Endpoints:")
    print("   GET  /like?uid=UID&server_name=REGION&key=nur")
    print("   GET  /add_account?uid=...&pass=...&region=...&key=nur")
    print("   GET  /reset-cache?key=nur")
    print("Account files: account_cis.txt, account_br.txt, account_bd.txt")
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)
