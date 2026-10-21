"""cli 与 service 共用的那几个动作。

单独一层是因为**两份调用方**(命令行与 qi-lab 面板)必须看到同一套行为:
"什么叫可升级"、"声明怎么解析"、"客户端谁来建"。各写一份的结果是同一个条目
在终端里说"最新"、在网页上说"可升级"。

这一层不打印、不询问、不 import argparse。
"""

from __future__ import annotations

import os

from typing import Any

from . import config
from .catalog import Catalog, StoreItem, load as load_catalog
from .client import StoreClient
from .config import Source, StoreConfig
from .ledger import Ledger, Record


def open_client(source: Source, http_factory: Any = None, *,
                on_auth_change: Any = None, **kwargs: Any) -> StoreClient:
    """建一个客户端,并把"令牌变了就写回用户级配置"接上。

    `http_factory` 是测试的注入点(`httpx.MockTransport`);生产传 `None` 走真实 httpx。
    `on_auth_change` 允许调用方**自己**决定怎么落盘(默认就是写用户级 `store.json`)——
    早先它是写死的,于是 `service.login` 再传一个就变成重复关键字。

    `QI_STORE_TOKEN` 在这里统一生效:文档里说它"优先于文件"(CI 通道),那只在
    `whoami` 里认是不够的 —— 命令行的每条取货命令都得能用它。
    """
    from . import paths

    override = kwargs.pop("token_override", None) or os.environ.get(paths.QI_STORE_TOKEN)
    if override:
        kwargs["token_override"] = override

    def _save(auth: dict[str, Any] | None) -> None:
        config.save_auth(source.name, auth, base_url=source.base_url)

    callback = on_auth_change or _save
    if http_factory is not None:
        return http_factory(source, on_auth_change=callback, **kwargs)
    return StoreClient(source, on_auth_change=callback, **kwargs)


def load_for(source: Source, http_factory: Any = None, *, kinds: list[str] | None = None,
             offline: bool = False) -> Catalog:
    client = open_client(source, http_factory)
    try:
        return load_catalog(client, store_name=source.name, kinds=kinds, offline=offline)
    finally:
        client.close()


def resolve(catalog: Catalog, key: str) -> StoreItem:
    """`[kind:]key` → 条目。歧义/不存在都抛 `NotFound`(退出码 4 / HTTP 404)。"""
    return catalog.find(key)


def wanted_specs(cfg: StoreConfig) -> list[str]:
    """`wanted[]` 的每项允许字符串,也容忍 `{"id"/"name"/"spec"}` 结构体
    (照 qi 的 `settings.packages` 口径:写法宽一点,不让人因为多包一层而失败)。"""
    specs: list[str] = []
    for entry in cfg.wanted:
        if isinstance(entry, str) and entry.strip():
            specs.append(entry.strip())
        elif isinstance(entry, dict):
            for key in ("id", "name", "spec", "source"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    specs.append(value.strip())
                    break
    return specs


def diff(ledger: Ledger, catalog: Catalog) -> list[tuple[Record, StoreItem | None, str]]:
    """逐条比账本与目录。返回 `(记录, 上游条目|None, 状态描述)`。

    四种状态都是**事实**,不是建议:

    * `本地已被移除` —— 磁盘上已经没有了(别处删的 / 手改的)。**优先于其余三种**:
      这是唯一"能马上处理"的状态
    * `可升级` —— 同 itemId 但内容指纹变了
    * `上游不可见` —— 下架了 / 不在你账号的可见域里(两者对客户端是一回事)
    * `最新` —— 指纹相同

    "本地还在不在"必须查磁盘:账本只记"装过什么",从设置面 A2A/MCP 那两节删除、
    手工改 json、`rm -rf` 技能目录都不经过商店 —— 不查的话商店会一直标着"已安装"。
    """
    from .verify import presence

    index = {item.id: item for item in catalog.items}
    rows: list[tuple[Record, StoreItem | None, str]] = []
    for record in ledger.items:
        item = index.get(record.itemId)
        ok, why = presence(record)
        if not ok:
            rows.append((record, item, f"本地已被移除({why})"))
        elif item is None:
            rows.append((record, None, "上游不可见(下架或没权限)"))
        elif item.version != record.version:
            rows.append((record, item, f"可升级 {record.version} → {item.version}"))
        else:
            rows.append((record, item, "最新"))
    return rows
