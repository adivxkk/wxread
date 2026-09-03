# main.py 主逻辑：包括字段拼接、模拟请求
import json
import time
import random
import logging
import hashlib
import re
import requests
import urllib.parse
from push import push
from log_utils import setup_logging
from config import data, headers, cookies, READ_NUM, PUSH_METHOD, book, chapter

try:
    from curl_cffi import requests as curl_cffi_requests
except ImportError:
    curl_cffi_requests = None


# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [{"rq": "%2Fweb%2Fbook%2Fread", "ql": False},{"rq": "%2Fweb%2Fbook%2Fread", "ql": True},{"rq": "%2Fweb%2Fbook%2Fread"},]

# 模拟真实阅读的节奏参数（秒），仍以约 30 秒/次为准
PAGE_WAIT_MIN = 25.0
PAGE_WAIT_MAX = 35.0
PAGE_WAIT_DRIFT = 2.0
LONG_PAUSE_RATE = 0.04
LONG_PAUSE_MIN = 45.0
LONG_PAUSE_MAX = 75.0

# 请求稳定性参数，仅针对网络抖动重试，不重放成功请求
REQUEST_TIMEOUT = 10
REQUEST_RETRIES = 3
RETRY_BACKOFF_FACTOR = 0.8


def _browser_client_hints(user_agent):
    """根据 User-Agent 生成一致的浏览器客户端提示，避免请求头相互矛盾。"""
    chrome_version = re.search(r"Chrome/(\d+)", user_agent)
    chrome_version = chrome_version.group(1) if chrome_version else "131"
    edge_version = re.search(r"Edg/(\d+)", user_agent)

    if edge_version:
        return {
            "sec-ch-ua": (
                f'"Not_A Brand";v="24", "Chromium";v="{chrome_version}", '
                f'"Microsoft Edge";v="{edge_version.group(1)}"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    return {
        "sec-ch-ua": (
            f'"Not_A Brand";v="24", "Chromium";v="{chrome_version}", '
            f'"Google Chrome";v="{chrome_version}"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def build_request_headers():
    """合并浏览器请求头，配置中的头信息优先。"""
    request_headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": "https://weread.qq.com",
        "pragma": "no-cache",
        "referer": "https://weread.qq.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest",
    }
    request_headers.update(_browser_client_hints(headers.get("user-agent", "")))
    request_headers.update(headers)

    if CURL_SESSION:
        # curl_cffi 会按浏览器指纹生成自己的 UA 和客户端提示，避免两边不一致。
        for key in list(request_headers):
            if key.lower() in {
                "user-agent",
                "sec-ch-ua",
                "sec-ch-ua-mobile",
                "sec-ch-ua-platform",
            }:
                request_headers.pop(key, None)

    return request_headers


def create_http_session():
    """优先使用 curl_cffi 模拟浏览器 TLS，缺失时回退 requests。"""
    if curl_cffi_requests is not None:
        try:
            return curl_cffi_requests.Session(impersonate="chrome"), True
        except Exception as exc:
            logging.debug("curl_cffi 初始化失败，回退到 requests：%s", exc)

    return requests.Session(), False


def post_json(url, payload, timeout=REQUEST_TIMEOUT):
    """发送 JSON 请求，仅在连接失败时带指数退避重试。"""
    request_headers = build_request_headers()
    last_error = None

    for attempt in range(REQUEST_RETRIES):
        try:
            return HTTP_SESSION.post(
                url,
                headers=request_headers,
                cookies=cookies,
                data=json.dumps(payload, separators=(",", ":")),
                timeout=timeout,
            )
        except Exception as exc:
            last_error = exc
            if attempt < REQUEST_RETRIES - 1:
                delay = RETRY_BACKOFF_FACTOR * (2 ** attempt) + random.uniform(0, 0.5)
                logging.debug("请求 %s 失败（%s），%.2f 秒后重试。", url, exc, delay)
                time.sleep(delay)

    raise last_error


def encode_data(data):
    """数据编码"""
    return '&'.join(f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys()))


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7fffffff & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7fffffff & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()


def next_page_wait(current_wait):
    """返回（下一轮节奏，本次等待时长），模拟翻页间隔的缓慢漂移和偶尔停留。"""
    if random.random() < LONG_PAUSE_RATE:
        return current_wait, random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
    next_wait = current_wait + random.uniform(-PAGE_WAIT_DRIFT, PAGE_WAIT_DRIFT)
    next_wait = max(PAGE_WAIT_MIN, min(PAGE_WAIT_MAX, next_wait))
    return next_wait, next_wait

def get_wr_skey():
    """刷新cookie密钥"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = post_json(RENEW_URL, cookie_data)
            
            if 'wr_skey' in response.cookies:
                return response.cookies['wr_skey'][:8]
            else:
                continue
        except Exception as exc:
            logging.warning(f"refresh_cookie 请求失败，payload={cookie_data}，原因：{exc}")
            continue
        
        
    return None

def fix_no_synckey():
    post_json(FIX_SYNCKEY_URL, {"bookIds": ["3300060341"]})

refresh_print = setup_logging()
HTTP_SESSION, CURL_SESSION = create_http_session()

def refresh_cookie():
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies['wr_skey'] = new_skey
        logging.info(f"密钥刷新成功，新密钥：{new_skey[:2]}***")
        logging.info("重新本次阅读。")
    else:
        ERROR_CODE = "无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。"
        logging.error(ERROR_CODE)
        push(ERROR_CODE, PUSH_METHOD, is_success=False)
        raise Exception(ERROR_CODE)

refresh_cookie()
index = 1
page_wait = random.uniform(PAGE_WAIT_MIN, PAGE_WAIT_MAX)
lastTime = int(time.time()) - int(page_wait)
logging.info(f"一共需要阅读 {READ_NUM} 次。")

while index <= READ_NUM:
    data.pop('s')
    data['b'] = random.choice(book)
    data['c'] = random.choice(chapter)
    thisTime = int(time.time())
    data['ct'] = thisTime
    data['rt'] = thisTime - lastTime
    data['ts'] = int(thisTime * 1000) + random.randint(0, 1000)
    data['rn'] = random.randint(0, 1000)
    data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
    data['s'] = cal_hash(encode_data(data))

    refresh_print(f"阅读进度: 第 {index}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
    logging.debug("data: %s", data)
    response = post_json(READ_URL, data)
    resData = response.json()
    logging.debug("response: %s", resData)

    if 'succ' in resData:
        if 'synckey' in resData:
            lastTime = thisTime
            index += 1
            if index <= READ_NUM:
                page_wait, wait_seconds = next_page_wait(page_wait)
                time.sleep(wait_seconds)
            refresh_print(f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        else:
            logging.warning("无 synckey，尝试修复...")
            fix_no_synckey()
    else:
        logging.warning("cookie 已过期，尝试刷新...")
        refresh_cookie()

logging.info("阅读脚本已完成。")

if PUSH_METHOD not in (None, ''):
    logging.info("开始推送...")
    push(f"微信读书自动阅读完成。\n阅读时长：{(index - 1) * 0.5} 分钟。", PUSH_METHOD, is_success=True)
else:
    logging.info("未配置推送渠道，跳过推送。")
