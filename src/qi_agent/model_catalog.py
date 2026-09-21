"""从 provider 的 OpenAI 兼容 `/models` 端点拉模型列表(给 `qi init --refresh`)。

**为什么需要它**:`presets.py` 里那张表是**种子** —— 离线可用、带核过的 `contextWindow` /
`maxTokens`,但**模型 id 会漂**(上新 / 下线 / 改名)。实测例子:DeepSeek 把
`deepseek-v4-flash` 换成了 `deepseek-flash`(V4.1 Flash,旧名仍路由到新模型)——
这种变更只能靠厂商公告或接口,写死的表迟早落后一拍。所以另给一条**从接口拉**的路。

全部国产 provider 都提供 OpenAI 兼容的 `GET {baseUrl}/models`(返回 `{"data": [{"id": …}]}`),
拿同一个 API key 鉴权即可。这里**不做网络以外的猜测**:接口返回什么 id 就用什么 id;
`contextWindow` / `maxTokens` 接口不返回,新加进来的条目先用 qi 的默认值(要精确就照厂商文档补)。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

#: 一次刷新的超时(秒)。厂商的 /models 通常几十毫秒,20s 只用于兜住挂死的网络。
DEFAULT_TIMEOUT = 20.0


class CatalogError(Exception):
    """拉取失败(网络 / HTTP 状态 / 返回体不是预期的形状)。"""


def models_url(base_url: str) -> str:
    """`baseUrl` → `/models` 端点。

    规则就是拼一个 `/models`:各家的 OpenAI 兼容前缀都直接支持 ——
    `https://api.deepseek.com` → `/models`,`…/compatible-mode/v1` → `/compatible-mode/v1/models`。

    **只认 http(s)** —— `baseUrl` 来自用户的 `models.json`,与其它配置同信任域,但
    `urlopen` 遇到 `file://` 会去读本地文件,那种“靠配置读盘”的口子不值得留
    (`ftp://` / `data:` 同理)。写错了就报错,不静默换个东西去读。
    """
    url = base_url.rstrip("/") + "/models"
    if not url.lower().startswith(("http://", "https://")):
        raise CatalogError(f"baseUrl 必须是 http(s),拿到的是:{base_url!r}")
    return url


def model_ids_from_payload(payload: Any) -> list[str]:
    """从 `/models` 的返回体里取模型 id(去重、保序)。

    认三种形状:OpenAI 的 `{"data": [{"id": …}]}`、裸列表 `[{…}]`、以及
    `{"data": ["id", …]}`(少数网关直接给字符串)。取不到就抛 `CatalogError`
    —— 不静默返回空列表(那会让人以为“厂商没有模型”)。
    """
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise CatalogError("返回体里没有 `data` 列表")
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("id") or item.get("model")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    if not ids:
        raise CatalogError("返回体里没有任何模型 id")
    return ids


def fetch_model_ids(base_url: str, api_key: str | None, *,
                    timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """`GET {baseUrl}/models` 并取出模型 id。失败一律抛 `CatalogError`。

    只读、需要网络:**只由显式的 `qi init --refresh` 调用** —— qi 的启动路径不做任何网络请求。
    """
    if not base_url:
        raise CatalogError("这个 provider 没有 baseUrl,拉不了模型列表")
    url = models_url(base_url)          # 顺带把非 http(s) 的 baseUrl 挡在外面
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 URL 已过 scheme 检查
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        hint = "API key 不对或没权限" if exc.code in (401, 403) else exc.reason
        raise CatalogError(f"{url} 返回 HTTP {exc.code}({hint})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CatalogError(f"连不上 {url}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{url} 返回的不是 JSON") from exc
    return model_ids_from_payload(payload)


def merge_model_ids(entry: dict[str, Any], ids: list[str]) -> tuple[list[str], list[str]]:
    """把接口来的 id 并进 provider 段(`entry` 原地改),返回 `(新增, 本地有但接口没返回)`。

    **只加不删**:厂商的 `/models` 有可能只列你**有权限**的模型、或者干脆列不全,
    所以本地多出来的条目保留(调用方可以提示用户,但不替他删配置)。
    新加的条目只写 `id` —— `contextWindow` / `maxTokens` 走 qi 的默认值,
    要精确就照厂商文档在 `models.json` 里补(或让 `--preset` 的种子覆盖它)。
    """
    models: list[Any] = entry.setdefault("models", [])
    existing = [str(m.get("id")) for m in models if isinstance(m, dict) and m.get("id")]
    added = [model_id for model_id in ids if model_id not in set(existing)]
    for model_id in added:
        models.append({"id": model_id})
    return added, [model_id for model_id in existing if model_id not in ids]
