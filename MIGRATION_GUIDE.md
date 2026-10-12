# LiveTranslate 云端 API 迁移指南

> 目的：把当前默认的**阿里云百炼（DashScope）**三处云端调用，替换成**公司内部 AI API**。
> 本指南可直接交给公司 AI IDE 作为改造依据，也可人工按步骤执行。

---

## 0. 先读：三个云端调用点

当前架构是「本地录音 + 本地 VAD 切分 → 云端 ASR → 云端翻译」，云端一共 **3 处**，难度差异很大：

| # | 服务 | 当前实现 | 模型 | API 形态 | 迁移难度 |
|---|------|---------|------|---------|---------|
| A | **实时翻译**（逐句字幕） | `translator.py` → `Translator` | `qwen-plus` | 标准 OpenAI 兼容 `chat/completions` | ★☆☆☆☆ 最简单 |
| B | **整段校对翻译**（每分钟一次） | `translator.py` → `RecapTranslator` | `qwen-mt-flash` | OpenAI 兼容 + **Qwen-MT 专有** `translation_options` | ★★★☆☆ 中等 |
| C | **云端语音转文字** | `asr_cloud.py` → `CloudASREngine` | `qwen3-asr-flash` | **Qwen 专有** `input_audio` + base64 | ★★★★☆ 最难 |

> **本地部分不用改**：WASAPI 录音、Silero VAD 切分、PyQt6 字幕界面、`transcripts/` 存档，全部与云端无关。

### 判断公司 API 属于哪一类（决定工作量）

- **A/B（翻译）**：只要公司 API 是 **OpenAI 兼容**（能用 `openai` SDK 指向公司 base_url 调 `chat/completions`），基本只改配置，不改代码。
- **C（ASR）**：关键看公司 ASR 的接口形态。三种常见情况：
  1. **OpenAI 兼容 `/audio/transcriptions`**（Whisper 风格，multipart 上传文件）→ 改 `transcribe()` 约 10 行，见 §3.3。
  2. **Qwen 风格 `chat/completions` + base64 音频**（和现在一样）→ 只改配置。
  3. **公司自研 REST/gRPC/WebSocket**（自有鉴权、自有音频格式）→ 需重写 `asr_cloud.py` 的请求部分，见 §3.4。

---

## 1. 迁移前准备

### 1.1 收集公司 API 信息（填这张表）

把下表填好，连同本指南一起交给 AI IDE，可以省掉大量来回确认：

```
【翻译 API】(对应服务 A/B)
  base_url        : https://____.internal/v1
  api_key         : 环境变量名 = ____   （建议不要写死在文件里）
  模型名          : ____
  是否 OpenAI 兼容: 是 / 否
  是否支持流式    : 是 / 否
  是否需要额外请求参数（如关闭思考模式）: ____
  鉴权方式        : Bearer Token / API-Key 头 / mTLS / 其他 ____

【ASR API】(对应服务 C)
  base_url        : ____
  api_key         : 环境变量名 = ____
  模型名          : ____
  接口形态        : OpenAI /audio/transcriptions / Qwen chat+base64 / 自研 REST / WebSocket
  音频要求        : 格式 ____ 采样率 ____ 单段最大时长 ____ 大小上限 ____
  语言参数怎么传  : ____
  返回结构        : ____
  是否返回识别语种: 是 / 否
```

### 1.2 环境变量约定

当前代码用 `env:VARNAME` 前缀从环境变量读 Key（实现见 `translator.py` 的 `resolve_api_key()`），
这样 **Key 不会落进任何配置文件**，也不会被提交到 git。建议公司版沿用这个机制，只换变量名：

```
当前: DASHSCOPE_API_KEY
公司: 例如 COMPANY_AI_API_KEY
```

> `resolve_api_key()` 是通用的，只认 `env:` 前缀，**不含任何阿里云逻辑，不需要改**。

### 1.3 ⚠️ 拷贝前必须清理的硬编码密钥

`main.py` 首次启动向导里**残留了一个 LM Studio 的真实 API Key**，拷到公司前务必清掉：

```python
# main.py 约 1917-1922 行（首次启动向导的预填模型）
dlg = ModelEditDialog(None, {
    "name": "",
    "api_base": "http://127.0.0.1:1234/v1",
    "api_key": "",      # ← 保持为空，由用户在向导里填自己的 Key
    "model": "",
})
```

> 历史上这里曾残留一个 LM Studio 的真实 Key，现已清空。**任何情况下都不要把明文 Key
> 写进这里**——它会被提交进 git。一律用 `env:变量名` 形式（见 §1.2）。

同时检查 **`user_settings.json`** 是否含个人配置（`models[]` 里的 Key、`audio_device`/`mic_device` 是你本机声卡名，公司机器上必然不同）。
建议拷贝前直接**删掉 `user_settings.json`**，让程序按 `config.yaml` 重新生成，或按 §5 手工改。

另外这些也建议清理（含个人使用痕迹）：

```
logs/          transcripts/       __pycache__/
_rt_cloud_*.py  _cloud_asr_*.py   （如有残留临时测试文件）
models/        （体积大，且公司会重新下载/或不需要）
```

---

## 2. 迁移服务 A：实时翻译（最简单）

### 2.1 改配置即可（如果公司 API 是 OpenAI 兼容）

**`config.yaml` → `translation:` 段（约 45-75 行，其中 api_base/api_key/model 在 50-53 行）**

```yaml
translation:
  api_base: "https://你的公司域名/v1"      # 原: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: "env:COMPANY_AI_API_KEY"        # 原: env:DASHSCOPE_API_KEY
  model: "公司翻译模型名"                    # 原: qwen-plus
  proxy: "system"                          # 内网直连可改 "none"
  target_language: "zh"
  source_language: "auto"
  max_tokens: 256
  temperature: 0.3
  streaming: true                          # 公司 API 不支持流式则改 false
```

**`user_settings.json` → `models[0]`（约 20-32 行）**
这个文件**优先级高于 config.yaml**，运行时实际用的是它，必须一起改：

```json
{
  "name": "公司翻译模型",
  "api_base": "https://你的公司域名/v1",
  "api_key": "env:COMPANY_AI_API_KEY",
  "model": "公司翻译模型名",
  "proxy": "system",
  "streaming": true,
  "thinking_style": "off",
  "no_think": true,
  "context_turns": 4,
  "input_price": 0,
  "output_price": 0
}
```
`"active_model": 0` 保持不变（指向这一项）。

> 也可以不改文件：启动后在 **设置界面 → 模型编辑** 里直接改，会自动写回 `user_settings.json`。

### 2.2 关键参数说明

- **`thinking_style`**：控制如何关闭模型的"思考模式"。取值见 `translator.py` 的 `THINKING_STYLES`：

  | 值 | 发送的请求参数 | 适用 |
  |----|--------------|------|
  | `off` | 什么都不发 | **非思考模型 / 公司自部署（推荐先试这个）** |
  | `qwen` | `{"enable_thinking": false}` | 阿里云 DashScope、硅基流动 |
  | `deepseek` | `{"thinking": {"type": "disabled"}}` | DeepSeek、火山方舟、智谱 GLM |
  | `vllm` | `{"chat_template_kwargs": {"enable_thinking": false}}` | 自部署 vLLM / SGLang |
  | `openai` | `{"reasoning_effort": "none"}` | OpenAI GPT-5+ / Grok |
  | `auto` | 按 endpoint/模型名猜 | 不确定时 |

  **踩坑提示**：很多严格实现的 OpenAI 兼容网关会**拒绝未知参数**。如果公司 API 报 400
  `unexpected parameter`，就把 `thinking_style` 设成 `"off"`。
  反过来，如果公司模型是思考型且翻译返回空，日志会出现
  `thinking burned the budget` 警告，此时要改成对应风格。

- **`proxy`**：`"system"` = 读系统/环境变量代理；`"none"` = 显式绕过代理（内网直连常用）；也可填具体代理 URL。
  实现见 `translator.py` 的 `make_openai_client()`。**公司内网 API 通常应设 `"none"`**，否则可能被系统代理劫持。

- **`system_prompt`**：在 `user_settings.json` 的 `system_prompt` 字段（当前是一份 IT 术语表提示词）。
  这段**与云厂商无关，可以直接沿用**，只需按公司业务改术语表。

### 2.3 代码里的默认值（可选清理）

这些只在配置缺失时兜底，改了更干净，不改也能跑：

| 文件 | 位置 | 内容 |
|------|------|------|
| `asr_cloud.py` | 第 13 行 | `DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"` |
| `main.py` | `build_recap_translator()` 约 177 行 | `cfg.get("api_base", "https://dashscope...")` |
| `main.py` | `_switch_asr_engine()` 约 578 行 | 同上，ASR 的 api_base 兜底 |
| `main.py` | 约 473 行 | `config.get("cloud_asr_api_key") or "env:DASHSCOPE_API_KEY"` |
| `control_panel.py` | 约 441/451/456 行 | 设置界面云端配置组的默认值与提示文字 |

---

## 3. 迁移服务 C：云端 ASR（最需要改代码）

### 3.1 当前实现长什么样

`asr_cloud.py` 的 `transcribe()` 用的是 **Qwen3-ASR 专有格式**——把音频塞进 chat 消息的
`input_audio` 字段，参数通过 `extra_body.asr_options` 传：

```python
# asr_cloud.py 约 113-138 行（transcribe() 的请求部分）
asr_options = {"enable_itn": True}
if self.language:
    asr_options["language"] = self.language

resp = self._client.chat.completions.create(
    model=self._model,
    messages=[{
        "role": "user",
        "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
    }],
    extra_body={"asr_options": asr_options},
    stream=False,
)
text = (resp.choices[0].message.content or "").strip()
```

其中 `data_uri` 由 `_encode_audio()` 生成：float32 → int16 PCM → WAV 字节 → base64 →
`"data:audio/wav;base64,..."`。**16kHz 单声道**，这个编码逻辑与厂商无关，可保留。

### 3.2 情况一：公司 API 也是 Qwen 风格（chat + base64）

**只改配置，不动代码。** 需要改的地方：

- `config.yaml` → `asr.cloud:` 段（约 38-43 行）：`api_base` / `api_key` / `model` / `proxy` / `timeout`
- `user_settings.json` 末尾三个字段（约 82-84 行）：
  `cloud_asr_model` / `cloud_asr_api_base` / `cloud_asr_api_key`
  > ⚠️ 这三个字段**优先级高于 config.yaml**，必须一起改，否则界面里会覆盖回去。

### 3.3 情况二：公司 API 是 OpenAI 兼容 `/audio/transcriptions`（Whisper 风格）

这是最常见的情况。把 `asr_cloud.py` 的 `transcribe()` 请求部分换成 multipart 上传：

```python
def transcribe(self, audio: np.ndarray, word_timestamps: bool = False, **kwargs) -> dict | None:
    """Send one VAD segment to the company ASR API and return the text."""
    if audio.size == 0:
        return None

    # 复用现有编码，但要拿到原始 WAV 字节而不是 base64 data URL
    wav_bytes = self._encode_wav_bytes(audio)   # 见下方说明

    try:
        resp = self._client.audio.transcriptions.create(
            model=self._model,
            file=("segment.wav", wav_bytes, "audio/wav"),
            language=self.language or None,     # 未知语种传 None 让服务端自动检测
            response_format="verbose_json",     # 需要返回语种时用；否则 "json"
        )
    except Exception as e:
        log.error(f"Cloud ASR request failed: {e}")
        return None

    text = (resp.text or "").strip()
    if not text:
        return None

    # verbose_json 会带 language 字段；普通 json 没有，回退到配置的语种提示
    detected = getattr(resp, "language", None) or self.language or "auto"
    return {"text": text, "language": detected, "language_name": detected}
```

配套把 `_encode_audio()` 拆成返回字节（base64 那版可保留备用）：

```python
def _encode_wav_bytes(self, audio: np.ndarray) -> bytes:
    if audio.dtype != np.int16:
        pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    else:
        pcm16 = audio
    buf = io.BytesIO()
    sf.write(buf, pcm16, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()
```

> 若公司接口只收 PCM 裸流，去掉 WAV 封装直接 `pcm16.tobytes()` 即可（当前
> `asr_remote.py` 就是发裸 PCM 的例子，可参考）。

### 3.4 情况三：公司自研 REST / gRPC / WebSocket

保留 `CloudASREngine` 的**类结构不变**（下面这份接口契约是管线依赖的），只重写内部的请求实现：

```
必须实现的接口（ASRClient 兼容 shim，管线靠这些方法驱动）:
  __init__(...)                      加载期做连通性校验，失败抛 ConnectionError
  status -> "ready"                  属性
  pid -> None                        属性；返回 None 可让内存监控/worker回收逻辑跳过
  transcribe(audio, word_timestamps=False, **kwargs) -> dict | None
                                     成功: {"text": str, "language": str, "language_name": str}
                                     失败/无语音: 返回 None（不要抛异常，管线会丢弃该段继续跑）
  set_language(language: str)        "auto" 表示自动检测
  set_input_padding(pad_seconds)     云端可空实现
  to_device(device) -> True          云端可空实现
  shutdown() / terminate()           关连接
  unload()                           释放资源
```

改造要点：
- **鉴权**：若不是标准 Bearer，就不能直接用 `openai` SDK，改用 `httpx`/`requests`
  （两者都已安装）。注意 `httpx.Client(trust_env=False)` 可绕过系统代理。
- **超时**：`timeout` 从配置传入，务必设置连接超时（当前 `make_openai_client` 用
  `httpx.Timeout(timeout, connect=5.0)`），否则网络故障会卡死整条管线。
- **失败必须返回 `None`**：这是管线的容错契约。抛异常会被上层当作 worker 崩溃处理，
  触发重启/回退逻辑。
- **WebSocket 实时流式**（如公司只提供流式 ASR）：当前管线是「VAD 切段 → 整段同步识别」，
  最简单做法是把每段音频一次性推完再收结果（伪实时）。真正的流式需要改 `main.py` 的
  管线循环，工作量大，建议第一版先不做。

### 3.5 ASR 相关的注册点（换引擎名才需要改）

**如果沿用 `cloud-asr` 这个引擎标识，下面都不用动。** 只有想改名叫 `company-asr` 之类才需要：

| 文件 | 位置 | 内容 |
|------|------|------|
| `model_manager.py` | `ASR_DISPLAY_NAMES` 约 145 行 | `"cloud-asr": "阿里云 Qwen-ASR"` ← 改显示名 |
| `main.py` | `_load_engine_client()` 约 468-479 行 | `engine_type == "cloud-asr"` 分支，实例化 `CloudASREngine` |
| `main.py` | `_switch_asr_engine()` 约 575-593 行 | 读云端配置 `cloud_asr` dict |
| `main.py` | `_switch_asr_engine()` 约 603-607 / 646-647 行 | 引擎签名、`display_name` |
| `main.py` | 约 697 / 732 行 | `device_label` 显示 "cloud" |
| `control_panel.py` | 约 215-231 行 | 引擎下拉框第 5 项 + `engine_map_idx`（`"cloud-asr": 4` 在 229 行）|
| `control_panel.py` | 约 426-461 行 | `_cloud_group` 配置组（Model / API Base / API Key）|
| `control_panel.py` | `_on_engine_changed_whisper_vis()` 约 1176-1177 行 | `index == 4` 时显示配置组 |
| `control_panel.py` | `_apply_settings()` 约 1487-1511 行 | `engine_map[4]`（1491 行）+ 保存三个 cloud 字段（1501-1510 行）|
| `i18n/zh.yaml` `i18n/en.yaml` | `asr_cloud` 键 | 下拉框标签「云端」/「Cloud」 |

> `model_manager.py` 的 `is_asr_cached()` 对未知引擎返回 `True`，所以云端引擎
> **不会触发模型下载弹窗**——这个行为要保持，否则公司机器上会卡在下载界面。

---

## 4. 迁移服务 B：整段校对翻译（RecapTranslator）

### 4.1 当前的 Qwen-MT 专有写法

`RecapTranslator`（`translator.py` 约 532-591 行）**不是** `Translator` 的子类，因为
Qwen-MT 只接受单条 user 消息、不支持 system role，所有翻译设置都塞在
`extra_body.translation_options` 里：

```python
options = {
    "source_lang": self._lang_name(source_language),
    "target_lang": self._lang_name(target_language),
}
if self._terms:
    options["terms"] = self._terms        # [{"source": "...", "target": "..."}]
if self._domains:
    options["domains"] = self._domains    # 英文领域描述

resp = self._client.chat.completions.create(
    model=self._model,
    messages=[{"role": "user", "content": text}],
    extra_body={"translation_options": options},
)
```

### 4.2 公司 API 若不支持 translation_options

需要把 `terms` 和 `domains` **折叠进 system prompt**，改成标准 chat 调用。替换 `translate()` 为：

```python
def translate(self, text, source_language="auto", target_language="zh"):
    """Return (translation, prompt_tokens, completion_tokens)."""
    src = self._lang_name(source_language)
    tgt = self._lang_name(target_language)

    lines = [
        f"Translate the following {src} text into {tgt}.",
        "Output ONLY the translation, no explanations.",
    ]
    if self._domains:
        lines.append(f"\nContext: {self._domains}")
    if self._terms:
        lines.append("\nGlossary (keep the target wording exactly):")
        for term in self._terms:
            lines.append(f"- {term['source']} -> {term['target']}")
    system_prompt = "\n".join(lines)

    resp = self._client.chat.completions.create(
        model=self._model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        timeout=self._timeout,
    )
    prompt_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
    self.last_usage = (prompt_tokens, completion_tokens)
    result = (resp.choices[0].message.content or "").strip()
    if not result:
        raise ValueError(f"{self._model} returned an empty translation")
    return result, prompt_tokens, completion_tokens
```

> 同时把构造函数里 `model="qwen-mt-flash"` 的默认值（约 547 行）改成公司模型名。
> `_lang_name()` 依赖 `LANGUAGE_DISPLAY`，与厂商无关，保留即可。
> 注意 `RecapTranslator` 默认**不开流式**（整段长文本一次返回），公司 API 若强制要求
> 流式参数需自行补 `stream=False`。

### 4.3 Recap 配置

**`config.yaml` → `recap:` 段（约 83-141 行）**

```yaml
recap:
  enabled: true                              # 不想要这个功能就设 false，会自动回退本地翻译
  api_base: "https://你的公司域名/v1"
  api_key: "env:COMPANY_AI_API_KEY"          # 当前是 ""，靠 DASHSCOPE_API_KEY 环境变量兜底
  model: "公司模型名"                          # 原: qwen-mt-flash
  timeout: 60
  proxy: "system"                            # 内网直连建议 "none"
  terms:                                     # 术语表：与厂商无关，按公司业务改
    - source: "Databricks"
      target: "Databricks"
    # ...
  domains: >-                                # 领域描述，必须是英文
    The text is a transcript of an IT software engineering meeting. ...
```

> **兜底行为**：`build_recap_translator()`（`main.py` 约 158-185 行）在 `enabled: false`
> 或 **Key 解析为空** 时返回 `None`，程序自动回退到本地 GPU 翻译整段。所以公司 API 没配好时
> 不会崩，只是变慢——这也是个不错的降级路径。
> 该函数第 168 行的 `os.environ.get("DASHSCOPE_API_KEY", "")` 兜底要改成公司变量名。

---

## 5. 需要修改的文件总清单

### 5.1 配置文件（改这些就能跑通大部分）

| 文件 | 段落 | 改什么 |
|------|------|--------|
| **`config.yaml`** | `asr:` 约 11-43 行 | `asr_engine: "cloud-asr"`（保留）；`asr.cloud.*`（38-43 行）的 base_url/key/model/proxy/timeout |
| | `translation:` 约 45-75 行 | `api_base` / `api_key` / `model` / `proxy`；`system_prompt` 按业务改术语 |
| | `recap:` 约 83-141 行 | `api_base` / `api_key` / `model` / `proxy`；`terms` / `domains` 按业务改 |
| **`user_settings.json`** | `asr_engine`（约 10 行） | 保持 `"cloud-asr"` |
| | `models[0]`（约 20-32 行） | 翻译 API 的 name/base/key/model/proxy/thinking_style |
| | `active_model`（约 46 行） | 保持 `0` |
| | `cloud_asr_*`（约 82-84 行） | **三个字段必改**，优先级高于 config.yaml |
| | `audio_device` / `mic_device`（约 51-52 行） | **必改**，公司机器声卡名不同；或直接删文件重新生成 |
| | `overlay_x/y/w/h`（约 78-81 行） | 分辨率不同可删，程序会重算 |

> **优先级规则**：`user_settings.json` > `config.yaml`。只改 config.yaml 而 settings 里有旧值时，
> 界面会用 settings 的值覆盖回去。最省事的做法是**删掉 `user_settings.json`**，
> 但那样会走首次启动向导，需要重配模型。

### 5.2 代码文件

| 文件 | 是否必改 | 改什么 |
|------|---------|--------|
| `asr_cloud.py` | **视情况必改** | §3.3 / §3.4；另改第 13-14 行 `DEFAULT_API_BASE` / `DEFAULT_MODEL`，第 31/41 行 `DASHSCOPE_API_KEY` 字面量与报错文字 |
| `translator.py` | 通常不改 | 仅当公司 API 不支持 `translation_options` 时改 `RecapTranslator.translate()`（§4.2）；约 535-553 行的注释与 `qwen-mt-flash` 默认值 |
| `main.py` | 建议改 | 约 168 行 env 变量名兜底；约 177/179、473、578/582/585 行的默认值；**约 1920 行硬编码 Key 必须删** |
| `control_panel.py` | 建议改 | 约 431/433/441/451/456 行的默认值与界面提示文字 |
| `model_manager.py` | 可选 | `ASR_DISPLAY_NAMES` 里 `"cloud-asr"` 的显示名 |
| `i18n/zh.yaml` `i18n/en.yaml` | 可选 | `asr_cloud` 标签；`thinking_style_qwen` 等描述性文字 |

### 5.3 依赖

`requirements` 无需新增——云端调用只用 `openai` + `httpx`（都已装）。
本地部分依赖不变：`torch`、`silero-vad`、`pyaudiowpatch`、`PyQt6`、`soundfile`、`numpy`。
若公司 ASR 走 gRPC，才需要额外装对应 SDK。

---

## 6. 交给 AI IDE 的提示词模板

把 §1.1 的表格填好后，可以直接用这段提示词：

```
这是一个 PyQt6 实时会议翻译工具 LiveTranslate。管线是：
本地 WASAPI 录音 → 本地 Silero VAD 切分成几秒一段 → 云端 ASR → 云端 LLM 翻译 → 本地字幕显示。

请先阅读 MIGRATION_GUIDE.md，然后把所有阿里云百炼（DashScope）的云端调用替换成公司内部 API。

公司 API 信息：
【翻译】base_url=____  模型=____  Key环境变量=____  OpenAI兼容=是/否  流式=是/否  额外参数=____
【ASR】 base_url=____  模型=____  Key环境变量=____  接口形态=____  音频格式要求=____  返回结构=____

要求：
1. 按指南 §2 改实时翻译（config.yaml 的 translation 段 + user_settings.json 的 models[0]，两处都要改）
2. 按指南 §3 改云端 ASR（asr_cloud.py）。必须保持 CloudASREngine 的接口契约不变：
   transcribe() 失败时返回 None 而不是抛异常；status/pid/shutdown/set_language 等 shim 方法保留。
3. 按指南 §4 改整段校对翻译（RecapTranslator）。若公司 API 不支持 translation_options，
   把 terms/domains 折叠进 system prompt。
4. 所有 API Key 一律用 "env:变量名" 形式，禁止把明文 Key 写进任何文件。
5. 删除 main.py 约 1920 行残留的硬编码 LM Studio API Key。
6. 把所有硬编码的 dashscope.aliyuncs.com / qwen-* / DASHSCOPE_API_KEY 默认值和注释一并更新。
7. 内网直连，proxy 统一设为 "none"（如公司有代理则说明）。
8. 不要改动本地部分：录音采集、Silero VAD、字幕界面、transcripts 存档。
9. 改完用 py_compile 校验语法，并列出所有改动文件。
```

---

## 7. 验证清单

改完按顺序验证，能快速定位是哪一层出问题：

```
□ 1. 语法：.venv\Scripts\python.exe -m py_compile asr_cloud.py translator.py main.py control_panel.py
□ 2. Key 解析：确认 resolve_api_key("env:COMPANY_AI_API_KEY") 返回真实 Key 而非空串
□ 3. 翻译单测（不启动 GUI）：
       构造 Translator(api_base, "env:...", model, streaming=True, system_prompt=..., proxy="none")
       调 translate_iter("The notebook failed.", "en")，确认流式出字
□ 4. ASR 单测：用 Windows TTS 生成一段英语 wav，读成 float32/16k，调 CloudASREngine.transcribe()
       确认返回 {"text":..., "language":...}，且耗时在秒级
□ 5. Recap 单测：调 RecapTranslator.translate() 传一段长文本，确认术语表生效
□ 6. 启动 GUI：确认没有弹模型下载框（云端引擎 is_asr_cached 应返回 True）
□ 7. 实跑：播放英语视频，确认字幕出现；监控栏 ASR 设备显示 "[cloud]"
□ 8. 等 1 分钟，确认底部整段校对栏有内容
□ 9. 断网测试：拔网后应只丢字幕、程序不崩（transcribe 返回 None）
□ 10. 检查 git status，确认没有明文 Key 被提交
```

### 常见故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 400 `unexpected parameter` | 公司网关拒绝 `enable_thinking` 等非标准参数 | `thinking_style` 设为 `"off"` |
| 翻译返回空 + 日志 `thinking burned the budget` | 模型是思考型，token 全被推理吃掉 | 改 `thinking_style` 为对应风格，或调大 `max_tokens` |
| 连接超时/被代理劫持 | `proxy: "system"` 走了公司代理 | 内网直连改 `proxy: "none"` |
| ASR 一直无字幕，日志报 request failed | 音频格式/时长/大小不符公司限制 | 核对采样率 16kHz、单段时长、大小上限 |
| 界面配置改了不生效 | `user_settings.json` 覆盖了 `config.yaml` | 两处都改，或删掉 settings 文件 |
| 启动卡在下载对话框 | 引擎名改了但 `is_asr_cached` 未覆盖 | 确认引擎标识仍是 `cloud-asr`，或让该函数对新名返回 True |
| Recap 栏空白 | `recap.enabled=false` 或 Key 为空 → 已回退本地且本地失败 | 查日志 `Recap cloud translation disabled` |

---

## 8. 迁移后可考虑的改进

- **云端失败自动回退本地**：目前 `transcribe()` 失败只返回 `None`（丢这一段）。
  可以在 `main.py` 的 ASR 循环里加计数，连续失败 N 次自动切回本地 FunASR/Whisper，
  实现"云端优先、本地兜底"，断网也能继续用。
- **离线兜底引擎**：下拉框里仍保留本地 `funasr` / `whisper` / `anime-whisper`，
  公司内网若无法访问外部 AI，也可以全部用本地模型（GPU 够的话）。
- **涉密合规**：录音内容会离开本机上传到公司服务器。确认公司 API 的数据留存策略，
  必要时关闭 `auto_save_transcript`（`user_settings.json`）避免本地留存明文转录。
