# -*- coding: utf-8 -*-
"""R121M2：S3 配音改为豆包「单向流式语音合成（HTTP）」。

为什么不再用异步长文本（`/api/v3/tts/submit` + `/query`）：火山客服确认异步长文本本来就是排队处理、
承诺 6 小时内返回，不是实时接口；复刻音色提交后可能长时间 `task_status=1`，引擎 20 分钟超时失败。
同一音色走流式接口能正常出声。1.2.1 起配音一律走流式。

官方文档：
- 接口：https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http?lang=zh
- 错误码：https://docs.volcengine.com/docs/6561/2534853?lang=zh（「单向流式语音合成 HTTP 接口」一节）

本模块只管「一段文本 → 一段 PCM + 字级时间戳」：切段、发请求、解析流、认错误、重试。
缓存、记账、拼接、写产物在 `steps.step_s3`。
"""
import base64
import codecs
import json
import re
import time
import urllib.error
import urllib.request
import uuid

from pipeline.engine import errors, netretry, stopflag
from pipeline.engine.errors import StepError

# --------------------------------------------------------------------------
# 常量（回执 §4 逐条说明取值理由）
# --------------------------------------------------------------------------

TTS_STREAM_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
TTS_STREAM_VERSION = "tts-stream-v1"          # 进每段的缓存键；改了请求结构就升号
TTS_STREAM_MAX_CHARS = 300                     # 单段字数上限（文档没写上限，按任务书定 300）
TTS_STREAM_CONCURRENCY = 3                     # 最多同时 3 段
TTS_STREAM_TIMEOUT_SEC = 120                   # 单段超时（含读完整个流）
TTS_STREAM_RETRY_WAITS = (5, 15, 30)           # 可重试的失败：最多重试 3 次，间隔递增（秒）
# 段与段之间补的静音（秒）。每段流式音频自带首尾留白（文档示例首字从 0.195 秒开始），
# 段内按句切开的接缝不再加；段落之间加 0.2 秒，接近异步整篇合成时段落间的停顿。没有真实试听，留给 R121W 定。
TTS_STREAM_PARAGRAPH_GAP_SEC = 0.2
TTS_STREAM_SENTENCE_GAP_SEC = 0.0
# 复刻音色必须带 model（文档：仅当 speaker 为复刻音色时指定；错误码表 InvalidModel 一行写明
# 枚举值只有 seed-tts-2.0-standard / seed-tts-2.0-expressive，默认 standard）。公共音色不带。
TTS_CLONE_MODEL = "seed-tts-2.0-standard"
# 音频格式：与现有成品 narration.wav 相同（实测 59-ST3F1：单声道 / 16 bit / 24000 Hz）。
# 文档：流式场景推荐 pcm，不建议 wav；wav/pcm 默认采样率 24000。
TTS_FORMAT = "pcm"
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2
CHANNELS = 1

_SENT_END = "。！？!?"
_CLOSERS = "”’」』）)》】\"'"
_SENT_RE = re.compile(rf"[^{_SENT_END}]*[{_SENT_END}]+[{re.escape(_CLOSERS)}]*|[^{_SENT_END}]+$")


# --------------------------------------------------------------------------
# 切段
# --------------------------------------------------------------------------


def split_sentences(paragraph):
    """按句号 / 问号 / 感叹号切句（句末标点和紧跟的右引号、右括号留在句子里）。拼回去与原文相同。"""
    return [m.group(0) for m in _SENT_RE.finditer(paragraph) if m.group(0)]


def split_chunks(text, max_chars=TTS_STREAM_MAX_CHARS):
    """口播稿 → 合成段。

    - 先按段落（换行）切，空行丢掉；
    - 单段超过 `max_chars` 字再按句切，相邻句子在不超过上限的前提下拼成一段；
    - 绝不在句子中间切：一句本身就超过上限时整句单独成段（`over_limit=True`，记进 stream_chunks.json）。

    返回 `[{"index": 1.., "text", "chars", "paragraph": 段落序号, "paragraph_start": bool, "over_limit": bool}]`。
    """
    out = []
    paras = [p.strip() for p in (text or "").split("\n")]
    paras = [p for p in paras if p]
    for pi, para in enumerate(paras, 1):
        if len(para) <= max_chars:
            pieces = [para]
        else:
            pieces, cur = [], ""
            for s in split_sentences(para):
                if cur and len(cur) + len(s) > max_chars:
                    pieces.append(cur)
                    cur = ""
                cur += s
            if cur:
                pieces.append(cur)
        for k, piece in enumerate(pieces):
            piece = piece.strip()
            if not piece:
                continue
            out.append({"index": len(out) + 1, "text": piece, "chars": len(piece),
                        "paragraph": pi, "paragraph_start": k == 0,
                        "over_limit": len(piece) > max_chars})
    return out


# --------------------------------------------------------------------------
# 资源 / 模型
# --------------------------------------------------------------------------


def is_clone_speaker(speaker):
    """复刻音色：ID 以 `S_` 开头（与 steps.tts_resource_id 的判断方式相同）。"""
    return str(speaker or "").strip().startswith("S_")


def model_for(speaker):
    """复刻音色必须带 model；公共音色不带（None）。"""
    return TTS_CLONE_MODEL if is_clone_speaker(speaker) else None


def product_name(resource_id):
    """资源 ID → 控制台里的产品名（提示文字用）。"""
    return {"seed-icl-2.0": "声音复刻 2.0", "seed-tts-2.0": "语音合成 2.0",
            "seed-tts-1.0": "语音合成 1.0"}.get(resource_id, resource_id or "豆包语音")


def request_body(text, speaker, model=None, speech_rate=25, loudness_rate=0):
    params = {
        "text": text,
        "speaker": speaker,
        "audio_params": {
            "format": TTS_FORMAT,
            "sample_rate": SAMPLE_RATE,
            "speech_rate": speech_rate,
            "loudness_rate": loudness_rate,
            "enable_subtitle": True,
        },
    }
    if model:
        params["model"] = model
    return {"user": {"uid": "s1-engine"}, "req_params": params}


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------


class TTSStreamError(Exception):
    """一次流式请求失败。`retryable` 决定要不要重试；`kind` 是 error_kind；`friendly` 是给用户看的大白话。"""

    def __init__(self, raw, *, retryable, kind, friendly, http=None, code=None):
        super().__init__(raw)
        self.raw = str(raw or "")
        self.retryable = retryable
        self.kind = kind
        self.friendly = friendly
        self.http = http
        self.code = code


_RE_CONCURRENCY = re.compile(r"(?i)quota exceeded for types:\s*concurrency|concurrency|too many requests|rate ?limit")
_RE_AUTH = re.compile(r"(?i)unauthori[sz]ed|authenticat|invalid (x-api-)?(api[ _-]?)?key|api[ _-]?key (is )?(invalid|not found|missing)"
                      r"|access ?denied|invalid token|signature")
_RE_BALANCE = re.compile(r"(?i)quota exceeded|insufficient|balance|arrear|overdue|exhaust|no (more )?quota|resource ?pack"
                         r"|余额|欠费|字数(包)?不足|额度不足|用量不足|超出(配额|额度)")
_RE_MISMATCH = re.compile(r"(?i)resource id is mismatched")
_RE_NOT_OPEN = re.compile(r"(?i)not granted|not activated|not (been )?open|service (is )?not (enabled|available for)"
                          r"|未开通|没有开通")
_RE_SPEAKER = re.compile(r"(?i)speaker not found|invalid speaker|speaker .*not found|voice not found")


def classify(http_status, code, message, resource_id, speaker):
    """（HTTP 状态, 回复里的 code, message）→ TTSStreamError 的分类参数。

    依据：上面错误码文档「单向流式语音合成 HTTP 接口」一节；文档没列出的（鉴权、字数不足）按 HTTP 状态和关键词认，
    回执 §5 逐条写明。顺序有讲究：先认超并发（45000000 里也有），再认不可重试的几种，最后按 4 / 5 开头兜底。
    """
    msg = str(message or "")
    c = str(code or "")
    prod = product_name(resource_id)
    if http_status == 429 or _RE_CONCURRENCY.search(msg):
        return dict(retryable=True, kind=errors.KIND_SERVICE,
                    friendly="火山引擎豆包语音同时合成的请求超过了账户并发上限（已自动重试）")
    if _RE_BALANCE.search(msg) or http_status == 402:
        return dict(retryable=False, kind=errors.KIND_BALANCE,
                    friendly=f"火山引擎『{prod}』剩余字数不足，请到火山引擎控制台 → 豆包语音 → 开通管理 "
                             f"增购字数包后点继续")
    if _RE_MISMATCH.search(msg):
        # R121M3：音色 ID 不存在 / 拼错（S_notexist000 实测）和没开通，火山都回 55000000
        # "resource ID is mismatched with speaker related resource"，分不开（R121W1 §4.2）。
        # 先说更常见的「ID 填错」，再说开通；仍不重试。
        # R121M4：「复刻音色已过期」只对复刻音色（S_ 开头）说，公共音色不提。
        expired = "、复刻音色已过期" if is_clone_speaker(speaker) else ""
        return dict(retryable=False, kind=errors.KIND_SERVICE,
                    friendly=f"音色「{speaker}」用不了。可能是音色 ID 不存在或拼写错误{expired}，"
                             f"也可能是账户还没开通『{prod}』。请先到 设置 核对配音音色 ID，"
                             f"再到火山引擎控制台 → 豆包语音 确认开通状态")
    if _RE_NOT_OPEN.search(msg):
        return dict(retryable=False, kind=errors.KIND_SERVICE,
                    friendly=f"当前音色需要开通『{prod}』，请到控制台确认已开通"
                             f"（也请检查音色 ID 是否拼写正确、复刻音色是否已过期）")
    if http_status in (401, 403) or _RE_AUTH.search(msg):
        return dict(retryable=False, kind=errors.KIND_KEY_INVALID,
                    friendly="火山引擎豆包语音的 API Key 无效或没有权限，请到 设置 里检查配音密钥"
                             "（火山引擎控制台 → 豆包语音 → API Key 管理）")
    if c in ("45002000", "45001115") or _RE_SPEAKER.search(msg):
        return dict(retryable=False, kind=errors.KIND_SERVICE,
                    friendly=f"音色「{speaker}」不存在或当前账户不能用，请到 设置 里检查配音音色 ID"
                             f"（火山引擎控制台 → 豆包语音 → 音色库）")
    if "InvalidModel" in msg:
        return dict(retryable=False, kind=errors.KIND_SERVICE,
                    friendly=f"复刻音色的 model 参数被拒绝（引擎填的是 {TTS_CLONE_MODEL}），请把这条提示发给开发者")
    if c == "45002001" or "No readable text" in msg:
        return dict(retryable=False, kind=errors.KIND_INPUT_INVALID,
                    friendly="这一段口播里没有能念的文字，请检查口播稿")
    if c.startswith("5") or (http_status or 0) >= 500:
        return dict(retryable=True, kind=errors.KIND_SERVICE, friendly="火山引擎豆包语音服务端出错")
    if c.startswith("4") or (http_status and 400 <= http_status < 500):
        return dict(retryable=False, kind=errors.KIND_SERVICE, friendly="火山引擎豆包语音拒绝了这次配音请求")
    return dict(retryable=True, kind=errors.KIND_SERVICE, friendly="火山引擎豆包语音返回了无法识别的错误")


def _error(http_status, code, message, raw, resource_id, speaker):
    return TTSStreamError(raw, http=http_status, code=code,
                          **classify(http_status, code, message, resource_id, speaker))


# --------------------------------------------------------------------------
# 解析流
# --------------------------------------------------------------------------


def parse_stream(chunks, *, resource_id=None, speaker=None, deadline=None):
    """把 HTTP chunked 回复（字节块迭代器）解析成 `{"pcm", "sentences", "text_words", "frames"}`。

    每个 JSON 对象一帧（文档响应示例）：`data` 是 base64 音频块，`sentence` 是一句的字级时间戳，
    `usage.text_words` 是计费字数。帧之间可能是换行、也可能直接相连，还兼容 SSE 的 `data:` 前缀。
    `code` 为 0 / 20000000（结束帧）正常，其他值按错误码认。
    """
    dec = codecs.getincrementaldecoder("utf-8")()
    jd = json.JSONDecoder()
    buf = ""
    pcm = bytearray()
    sentences = []
    text_words = None
    frames = 0

    def take(final=False):
        nonlocal buf, text_words, frames
        while True:
            s = buf.lstrip()
            while s.startswith("data:"):
                s = s[5:].lstrip()
            if not s:
                buf = ""
                return
            try:
                obj, end = jd.raw_decode(s)
            except json.JSONDecodeError:
                if final:
                    raise _error(None, None, "流式回复不完整", "流式回复不完整：" + s[:200], resource_id, speaker)
                buf = s
                return
            buf = s[end:]
            frames += 1
            if not isinstance(obj, dict):
                continue
            code = obj.get("code")
            if code not in (None, 0, 20000000, "0", "20000000"):
                raise _error(None, code, obj.get("message"), json.dumps(obj, ensure_ascii=False)[:500],
                             resource_id, speaker)
            data = obj.get("data")
            if isinstance(data, str) and data:
                pcm.extend(base64.b64decode(data))
            sent = obj.get("sentence")
            if isinstance(sent, dict) and sent.get("words"):
                sentences.append(sent)
            usage = obj.get("usage")
            if isinstance(usage, dict) and usage.get("text_words") is not None:
                text_words = int(usage["text_words"])

    for chunk in chunks:
        if deadline is not None and time.monotonic() > deadline:
            raise TTSStreamError(f"单段超过 {TTS_STREAM_TIMEOUT_SEC} 秒还没合成完", retryable=True,
                                 kind=errors.KIND_NETWORK, friendly=f"超过 {TTS_STREAM_TIMEOUT_SEC} 秒没有合成完")
        buf += dec.decode(chunk)
        take()
    buf += dec.decode(b"", final=True)
    take(final=True)
    if len(pcm) % (SAMPLE_WIDTH * CHANNELS):
        del pcm[-(len(pcm) % (SAMPLE_WIDTH * CHANNELS)):]
    return {"pcm": bytes(pcm), "sentences": sentences, "text_words": text_words, "frames": frames}


# --------------------------------------------------------------------------
# 发请求
# --------------------------------------------------------------------------


def _read_chunks(resp, size=8192):
    while True:
        b = resp.read(size)
        if not b:
            return
        yield b


def http_open(body, headers, timeout):
    """真实发送：返回 (HTTP 状态, 字节块迭代器 或 错误正文字节)。测试把它换成桩。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(TTS_STREAM_URL, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        return e.code, raw
    return resp.status, _read_chunks(resp)


def synth_once(text, *, speaker, resource_id, model, api_key, speech_rate=25, opener=None):
    """一段文本发一次流式请求。成功返回 parse_stream 的结果（另带 `request_id`）；失败抛 TTSStreamError。"""
    opener = opener or http_open
    body = request_body(text, speaker, model, speech_rate)
    rid = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": rid,
        "X-Control-Require-Usage-Tokens-Return": "*",
        "Connection": "keep-alive",
    }
    deadline = time.monotonic() + TTS_STREAM_TIMEOUT_SEC
    try:
        status, payload = opener(body, headers, TTS_STREAM_TIMEOUT_SEC)
        if status != 200:
            raw = payload if isinstance(payload, (bytes, bytearray)) else b"".join(payload)
            raw_s = raw.decode("utf-8", "replace")
            try:
                j = json.loads(raw_s)
                code, msg = j.get("code"), j.get("message")
            except Exception:
                code, msg = None, raw_s
            raise _error(status, code, msg, f"HTTP {status} {raw_s[:500]}", resource_id, speaker)
        out = parse_stream(payload, resource_id=resource_id, speaker=speaker, deadline=deadline)
    except TTSStreamError:
        raise
    except Exception as e:
        if netretry.is_transient_exc(e):
            raise TTSStreamError(netretry.describe_exc(e), retryable=True, kind=errors.KIND_NETWORK,
                                 friendly="网络连接中断或超时") from e
        raise
    if not out["pcm"]:
        raise TTSStreamError("流式回复里没有音频", retryable=True, kind=errors.KIND_SERVICE,
                             friendly="火山引擎豆包语音没有返回音频")
    if not out["sentences"]:
        raise TTSStreamError("流式回复里没有字级时间戳（sentence.words）", retryable=True, kind=errors.KIND_SERVICE,
                             friendly="火山引擎豆包语音没有返回字级时间戳")
    out["request_id"] = rid
    return out


def synth_with_retry(index, text, *, speaker, resource_id, model, api_key, speech_rate=25,
                     opener=None, log=None, sleep=None):
    """一段：可重试的失败最多重试 3 次（间隔递增）；仍失败或遇到不可重试的错误 → StepError「配音第 N 段失败」。"""
    sleep = sleep or stopflag.sleep
    attempt = 0
    while True:
        try:
            return synth_once(text, speaker=speaker, resource_id=resource_id, model=model,
                              api_key=api_key, speech_rate=speech_rate, opener=opener)
        except TTSStreamError as e:
            if e.retryable and attempt < len(TTS_STREAM_RETRY_WAITS):
                wait = TTS_STREAM_RETRY_WAITS[attempt]
                attempt += 1
                netretry._notify("豆包配音", attempt, wait, errors.redact(e.raw)[:160], f"第 {index} 段", log)
                sleep(wait)
                continue
            tail = "（已重试 3 次）" if e.retryable else ""
            err = StepError(f"配音第 {index} 段失败{tail}：{e.friendly}。服务商原话：{errors.redact(e.raw)[:300]}",
                            kind=e.kind, service=errors.SERVICE_SPEECH)
            err.provider_raw = e.raw
            err.retryable = e.retryable
            raise err from e
