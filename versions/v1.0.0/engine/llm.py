# -*- coding: utf-8 -*-
"""第 4 段：大语言模型推理（LLM Engine）

三种后端，按可用性自动降级：
  1. Ollama        本地大模型，HTTP 接口（默认 http://localhost:11434），离线可用
  2. OpenAI 兼容   任何 OpenAI 兼容 API（OpenAI / DeepSeek / 本地 vLLM 等），需要 base_url + key
  3. 规则化兜底    无模型时，基于检索到的上下文做抽取式摘要（关键词定位关键句）
"""
import json
import urllib.request
import urllib.error
import re

_OLLAMA_URL = "http://localhost:11434"


class LLMError(RuntimeError):
    """带中文可读提示的模型调用异常。"""


def _friendly_error(e, target=""):
    """把 urllib 的原始异常翻译成人能看懂、能操作的中文提示。"""
    code = getattr(e, "code", None)
    body = ""
    try:
        body = (e.read() or b"").decode("utf-8", "ignore")[:300]
    except Exception:
        pass

    if code == 404:
        if "11434" in target or "ollama" in target.lower():
            models = ollama_models()
            have = "、".join(models) if models else "（未安装任何模型）"
            return LLMError(
                "本地模型不存在（HTTP 404）。已安装模型：%s。"
                "请在「模型设置」里选择已安装的模型，或执行 ollama pull 下载。" % have)
        return LLMError("接口地址或模型名不存在（HTTP 404）：%s。"
                        "请检查「模型名」是否拼写正确。" % (body or target))
    if code == 401:
        return LLMError("API Key 无效或与所选服务商不匹配（HTTP 401）。"
                        "请确认 Key 属于当前选择的服务商（DeepSeek 的 Key 以 sk- 开头，"
                        "智谱的 Key 形如 32位字母数字.16位字母数字）。")
    if code == 402:
        return LLMError("账户余额/免费额度不足（HTTP 402）。")
    if code == 429:
        return LLMError("请求过于频繁或额度已用尽（HTTP 429），请稍后再试。")
    if code:
        return LLMError("模型服务返回错误 HTTP %s：%s" % (code, body or "无详细信息"))
    # 连接类错误
    return LLMError("无法连接模型服务（%s）。若用本地 Ollama，请确认它已启动。" % e)


def ollama_models(url=_OLLAMA_URL, timeout=3):
    """返回本地已安装的 Ollama 模型名列表；不可用时返回空列表。"""
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def _param_size(name):
    m = re.search(r"(\d+(?:\.\d+)?)\s*b", name.lower())
    return float(m.group(1)) if m else 99.0


def pick_ollama_model(preferred=None):
    """选出真正可用的 Ollama 模型。

    1) 配置里指定的模型已安装 -> 用它
    2) 否则在已安装列表里挑参数最小的（机器跑得动）
    3) 一个都没有 -> None
    """
    models = ollama_models()
    if not models:
        return None
    if preferred:
        preferred = preferred.strip()
        if preferred in models:
            return preferred
        base = preferred.split(":")[0].lower()
        for m in models:
            if m.split(":")[0].lower() == base and ":" not in preferred:
                return m
    return sorted(models, key=_param_size)[0]


def _post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _friendly_error(e, url) from None
    except urllib.error.URLError as e:
        raise LLMError("无法连接 %s（%s）。若用本地 Ollama，请确认 ollama 已启动。" %
                       (url, getattr(e, "reason", e))) from None
    except json.JSONDecodeError:
        raise LLMError("模型服务返回了无法解析的内容：%s" % url) from None


def ollama_available():
    return bool(ollama_models())


class OllamaBackend:
    def __init__(self, model=None, url=_OLLAMA_URL):
        self.url = url
        # 指定的模型没装时自动退回已安装的模型，避免 404
        self.model = pick_ollama_model(model) or model or "qwen2.5:3b"

    @property
    def label(self):
        return "本地 Ollama · " + self.model

    def generate(self, prompt):
        # 首次调用会把模型加载进内存，可能耗时较长，超时放宽到 180s
        res = _post_json(self.url + "/api/chat", {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=180)
        return res.get("message", {}).get("content", "").strip()


class OpenAICompatBackend:
    def __init__(self, base_url, api_key, model, name=""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name

    @property
    def label(self):
        return "在线模型 %s · %s" % (self.name or "OpenAI 兼容", self.model)

    def generate(self, prompt):
        res = _post_json(
            self.base_url + "/chat/completions",
            {"model": self.model,
             "messages": [{"role": "user", "content": prompt}],
             "temperature": 0.3,
             "max_tokens": 700},
            headers={"Authorization": "Bearer " + self.api_key},
            timeout=90,
        )
        try:
            return res["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise LLMError("模型返回格式异常：%s" % json.dumps(res, ensure_ascii=False)[:200])


class RuleBasedBackend:
    """无 LLM 时的兜底：抽取式摘要 + 关键词定位关键句。"""

    label = "规则化知识库兜底（未接入大模型）"

    def generate(self, prompt, contexts=None):
        contexts = contexts or []
        if not contexts:
            return "（未配置本地模型，且知识库中没有相关内容）"
        q = prompt
        qterms = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", q)
        lines = []
        for c in contexts[:3]:
            text = c["text"]
            sents = re.split(r"[。！？!?；;\n]+", text)
            best, best_score = [], 0
            for s in sents:
                s = s.strip()
                if len(s) < 2:
                    continue
                score = sum(1 for t in qterms if t and t in s)
                if score > best_score:
                    best, best_score = [s], score
                elif score == best_score and score > 0:
                    best.append(s)
            if best:
                lines.append("· " + "；".join(best[:2]))
        if not lines:
            return "（知识库中未找到与问题直接相关的片段）"
        return "根据本地知识库：\n" + "\n".join(lines[:5])


def build_llm(use_ollama=True, ollama_model=None,
              openai_base=None, openai_key=None, openai_model=None):
    """按优先级返回可用后端。"""
    if openai_base and openai_key:
        return OpenAICompatBackend(openai_base, openai_key, openai_model or "gpt-4o-mini")
    if use_ollama and ollama_available():
        return OllamaBackend(ollama_model)
    return RuleBasedBackend()


# 免费/低价模型预设（均为 OpenAI 兼容接口，需自行注册获取 API Key）
PROVIDERS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                 "name": "DeepSeek"},
    "zhipu":    {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
                 "name": "智谱 GLM"},
    "openai":   {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
                 "name": "OpenAI 兼容"},
}

# 智谱 Key 形如：32位十六进制 + "." + 16位字母数字
_ZHIPU_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}\.[A-Za-z0-9]{16}$")


def detect_provider_by_key(api_key):
    """按 Key 格式猜测服务商，识别不了返回 None。

    用户最容易犯的错：选了 DeepSeek 却粘贴智谱的 Key（或反之），
    结果拿到 401/404，还以为是软件坏了。
    """
    key = (api_key or "").strip()
    if not key:
        return None
    if _ZHIPU_KEY_RE.match(key):
        return "zhipu"
    if key.lower().startswith("sk-"):
        return "deepseek"
    return None


def build_llm_from_config(cfg):
    """根据配置字典构建后端。

    cfg 字段：
      provider    : auto | ollama | deepseek | zhipu | openai
      api_key     : API Key（在线模型必填）
      model       : 模型名（可选，缺省用预设）
      ollama_model: Ollama 模型名（可选，缺省自动挑选已安装的最小模型）
    """
    cfg = cfg or {}
    provider = (cfg.get("provider") or "auto").strip().lower()
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip()

    # Key 格式与所选服务商明显不符时，以 Key 为准自动纠偏（避免 401）
    if api_key and provider in ("deepseek", "openai"):
        guess = detect_provider_by_key(api_key)
        if guess and guess != provider:
            provider = guess
            model = ""  # 换服务商后模型名也不能沿用

    # 指定在线模型且给了 Key
    if provider in PROVIDERS and api_key:
        p = PROVIDERS[provider]
        return OpenAICompatBackend(p["base_url"], api_key, model or p["model"], p["name"])
    # 明确指定 Ollama
    if provider == "ollama":
        if ollama_available():
            return OllamaBackend(cfg.get("ollama_model"))
        return RuleBasedBackend()
    # auto：优先本地 Ollama，其次规则兜底
    if ollama_available():
        return OllamaBackend(cfg.get("ollama_model"))
    return RuleBasedBackend()


def describe(backend):
    """给界面用的后端描述。"""
    return getattr(backend, "label", None) or type(backend).__name__

