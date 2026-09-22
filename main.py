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
from config import data, headers, cookies, READ_NUM, PUSH_METHOD, book, chapter, BOOK_CHAPTERS

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
LONG_PAUSE_RATE = 0.05
LONG_PAUSE_MIN = 45.0
LONG_PAUSE_MAX = 75.0
# 偶发快速翻页（真人连续读几页小内容时的典型节奏）
QUICK_FLIP_RATE = 0.10
QUICK_FLIP_MIN = 8.0
QUICK_FLIP_MAX = 18.0

# 会话级行为参数：一次运行只读一本书、章节顺序推进、中途定时休息
CHAPTER_READS_MIN = 15   # 每章阅读的请求次数下限（约 8-18 分钟/章）
CHAPTER_READS_MAX = 35
BREAK_EVERY_MIN = 25     # 每连续阅读多少次后休息一次（约 13-23 分钟）
BREAK_EVERY_MAX = 45
REST_MIN = 60.0          # 休息时长（秒），模拟真人放下手机
REST_MAX = 180.0
RT_MIN = 10              # rt 下限，避免重试/刷新 cookie 后出现异常小的值

# 请求稳定性参数，仅针对网络抖动重试，不重放成功请求
REQUEST_TIMEOUT = 10
REQUEST_RETRIES = 3
RETRY_BACKOFF_FACTOR = 0.8

# 失败熔断参数：连续异常时指数退避，超过阈值立即终止，避免不断重试加重账号风控
MAX_CONSEC_FAILURES = 8
FAILURE_BACKOFF_CAP = 300.0


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
    """返回（下一轮节奏，本次等待时长），模拟翻页间隔的缓慢漂移和偶尔停留。

    节奏分三档，混合后接近真人翻页间隔的偏态分布：
    - 快速翻页（约 10%）：8-18 秒，连续读小段内容时出现；
    - 正常阅读（约 85%）：25-35 秒，基准值缓慢随机漂移；
    - 长时间停留（约 5%）：45-75 秒，遇到长段落或走神。
    """
    roll = random.random()
    if roll < LONG_PAUSE_RATE:
        return current_wait, random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
    if roll < LONG_PAUSE_RATE + QUICK_FLIP_RATE:
        # 快速翻页不改变基准节奏，之后仍回到正常区间
        return current_wait, random.uniform(QUICK_FLIP_MIN, QUICK_FLIP_MAX)
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

def failure_backoff(consec_fail):
    """连续失败时的指数退避等待时长（秒），上限 FAILURE_BACKOFF_CAP。"""
    return min(FAILURE_BACKOFF_CAP, random.uniform(5, 10) * (2 ** (consec_fail - 1)))

def abort_on_repeat_failures(consec_fail):
    """连续多次异常时终止运行，避免不断重试加重账号风控。"""
    msg = f"连续 {consec_fail} 次阅读请求异常，疑似触发风控或会话失效，终止运行以保护账号。"
    logging.error(msg)
    push(msg, PUSH_METHOD, is_success=False)
    raise Exception(msg)

refresh_print = setup_logging(log_file="progress.log")
HTTP_SESSION, CURL_SESSION = create_http_session()

def refresh_cookie(allow_existing=False):
    """刷新cookie密钥；allow_existing=True 时续期失败可退回现有密钥（启动阶段容错）"""
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies['wr_skey'] = new_skey
        logging.info(f"密钥刷新成功，新密钥：{new_skey[:2]}***")
        logging.info("重新本次阅读。")
        return
    if allow_existing and cookies.get('wr_skey'):
        # 已有密钥时续期失败不退出：先用现有密钥阅读，密钥真正过期时循环内会终止
        logging.warning("续期失败，但已有 wr_skey，继续使用现有密钥阅读。")
        return
    ERROR_CODE = "无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。"
    logging.error(ERROR_CODE)
    push(ERROR_CODE, PUSH_METHOD, is_success=False)
    raise Exception(ERROR_CODE)

refresh_cookie(allow_existing=True)
index = 1
consec_fail = 0
page_wait = random.uniform(PAGE_WAIT_MIN, PAGE_WAIT_MAX)
lastTime = int(time.time()) - int(page_wait)

# 会话状态：一次运行固定读一本书，接着上次进度从中间某章开始顺序往后读
session_book = random.choice(book)
session_chapters = BOOK_CHAPTERS.get(session_book) or chapter
chapter_idx = random.randrange(len(session_chapters))
ci_state = random.randint(3, 80)          # 章节序号，随章节推进单调递增
pr_state = random.uniform(30.0, 70.0)     # 全书进度百分比，缓慢增长
co_state = random.randint(30, 120)        # 章节内已读偏移（秒），随阅读时间累加
reads_in_chapter = 0
chapter_reads = random.randint(CHAPTER_READS_MIN, CHAPTER_READS_MAX)
reads_since_break = 0
break_threshold = random.randint(BREAK_EVERY_MIN, BREAK_EVERY_MAX)
logging.info(f"本次阅读书籍：{session_book}，从第 {ci_state} 章开始。")
logging.info(f"一共需要阅读 {READ_NUM} 次。")

while index <= READ_NUM:
    data.pop('s', None)
    data['b'] = session_book
    data['c'] = session_chapters[chapter_idx]
    data['ci'] = ci_state
    data['co'] = co_state
    data['pr'] = int(pr_state)
    thisTime = int(time.time())
    data['ct'] = thisTime
    data['rt'] = max(RT_MIN, thisTime - lastTime)
    data['ts'] = int(thisTime * 1000) + random.randint(0, 1000)
    data['rn'] = random.randint(0, 1000)
    data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
    data['s'] = cal_hash(encode_data(data))

    refresh_print(f"阅读进度: 第 {index}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
    logging.debug("data: %s", data)
    try:
        response = post_json(READ_URL, data)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        resData = response.json()
    except Exception as exc:
        # 网络重试耗尽或响应非 JSON（限流页/网关错误），指数退避后重试；连续失败达到阈值则终止
        consec_fail += 1
        if consec_fail >= MAX_CONSEC_FAILURES:
            abort_on_repeat_failures(consec_fail)
        wait = failure_backoff(consec_fail)
        logging.warning("阅读请求异常（连续第 %d 次），%.0f 秒后重试：%s", consec_fail, wait, exc)
        time.sleep(wait)
        continue
    logging.debug("response: %s", resData)

    if 'succ' in resData:
        consec_fail = 0
        if 'synckey' in resData:
            lastTime = thisTime
            index += 1
            reads_in_chapter += 1
            reads_since_break += 1
            co_state += data['rt']
            if index % 10 == 0 or index == READ_NUM:
                logging.info("进度 %d/%d（%.1f 分钟）", index, READ_NUM, (index - 1) * 0.5)
            if reads_in_chapter >= chapter_reads:
                # 读完一章，进入下一章：章节序号前进，偏移和计数重置
                chapter_idx = (chapter_idx + 1) % len(session_chapters)
                ci_state += 1
                co_state = random.randint(20, 90)
                reads_in_chapter = 0
                chapter_reads = random.randint(CHAPTER_READS_MIN, CHAPTER_READS_MAX)
                pr_state = min(99.0, pr_state + random.uniform(1.5, 4.0))
                logging.info(f"进入第 {ci_state} 章。")
            if index <= READ_NUM:
                if reads_since_break >= break_threshold:
                    # 连续阅读一段时间后休息，模拟真人中途放下手机
                    pause = random.uniform(REST_MIN, REST_MAX)
                    logging.info(f"已连续阅读 {reads_since_break} 次，休息 {pause:.0f} 秒。")
                    time.sleep(pause)
                    reads_since_break = 0
                    break_threshold = random.randint(BREAK_EVERY_MIN, BREAK_EVERY_MAX)
                    # 休息后重新计时，避免 rt 出现异常大的值
                    lastTime = int(time.time())
                page_wait, wait_seconds = next_page_wait(page_wait)
                time.sleep(wait_seconds)
            refresh_print(f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        else:
            consec_fail += 1
            if consec_fail >= MAX_CONSEC_FAILURES:
                abort_on_repeat_failures(consec_fail)
            logging.warning("无 synckey（连续第 %d 次），尝试修复...", consec_fail)
            fix_no_synckey()
            time.sleep(failure_backoff(consec_fail))
    else:
        consec_fail += 1
        if consec_fail >= MAX_CONSEC_FAILURES:
            abort_on_repeat_failures(consec_fail)
        logging.warning("cookie 已过期或返回异常（连续第 %d 次），尝试刷新...", consec_fail)
        refresh_cookie()
        time.sleep(failure_backoff(consec_fail))

logging.info("阅读脚本已完成。")

if PUSH_METHOD not in (None, ''):
    logging.info("开始推送...")
    push(f"微信读书自动阅读完成。\n阅读时长：{(index - 1) * 0.5} 分钟。", PUSH_METHOD, is_success=True)
else:
    logging.info("未配置推送渠道，跳过推送。")
