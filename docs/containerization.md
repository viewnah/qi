# 隔离运行(容器 / VM)

qi **不做进程内沙箱**:`bash` 能跑任何东西,文件工具只是"相对路径基于会话 cwd"的约定。
工具与扩展都以 qi 进程本身的权限运行(见 [security.md](security.md))。所以**真边界必须来自 qi 之外**。

需要隔离的典型场景:

- 无人值守地跑(`-p` / `--mode json` 喂给 CI),没人盯着它按 `escape`;
- 让 qi 碰不信任的仓库 —— 里面可能有 `.qi/extensions/`(`-na` 只挡加载,不挡 `bash`);
- 把 qi 暴露给别的机器或别的用户。

## 选一条隔离路线

| 路线 | 得到什么 | 代价 |
| --- | --- | --- |
| **容器**(Docker / Podman) | 文件系统与进程隔离;工作目录按需挂载 | 要自己搭镜像与挂载;凭证得想办法传进去 |
| **VM** | 与宿主机完全隔离(内核级) | 重、启动慢,适合长期跑或跑高风险任务 |
| **受限用户 + 只读挂载** | 便宜:换个没有权限的用户跑,仓库只读挂 | 同一内核,隔离弱于容器 |
| **远端机器 / 一次性实例** | 物理隔离 | 要同步代码与密钥 |

**共同原则**:只把**要干活的目录**放进去;**凭证走运行时注入**(环境变量 / 只读挂载),不要烤进镜像;
`~/.qi/agent/`(会话、trust、auth)单独挂一个卷,否则每次重建都会丢历史。

## 容器示例

qi 目前**没有发布到 PyPI**(见 [quickstart.md](quickstart.md)),所以镜像是"把仓库放进去装一遍":

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git bash \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

WORKDIR /opt/qi
COPY . /opt/qi
RUN uv sync --no-dev && ln -s /opt/qi/.venv/bin/qi /usr/local/bin/qi

# 工作目录由运行时挂载进来;agent 目录单独挂卷(会话与 trust 都在这)
VOLUME ["/root/.qi/agent"]
WORKDIR /workspace
ENTRYPOINT ["qi"]
```

```bash
docker build -t qi-local .
docker run --rm -it \
  -v "$PWD:/workspace" \
  -v qi-agent-home:/root/.qi/agent \
  -e DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  qi-local
```

要点:

- **凭证只从环境变量进**(或把 `auth.json` 只读挂载进去),不要 `COPY` 进镜像层 —— 镜像层会被推送、缓存。
- **工作目录挂载的是宿主机的仓库**,所以 qi 的写操作会真的落到宿主机上;要更安全就挂 `:ro` 再把可写区
  单独挂成临时目录。
- **`~/.qi/agent/` 用卷**:会话历史、`trust.json`、agent 级设置都在里面;不挂就每次重建。
- 想跑无头:`docker run --rm -v "$PWD:/workspace" qi-local -p "把 CHANGELOG 里最近 10 条整理成一句话"`。

## 只读工作目录 + 可写输出目录

只想让它**看**代码、把结果写到别处:

```bash
docker run --rm -v "$PWD:/workspace:ro" -v "$PWD/out:/out" -w /out qi-local -p "审查 /workspace/src"
```

文件工具的越界检查只覆盖 `read` / `ls` / `find` / `grep` / `write` / `edit`,而 `bash` 不受限 ——
所以**只读挂载是这里唯一真正的保证**,不是提示词。

## 网络

qi 只需要能连到模型 provider。要收紧就在容器/防火墙层限制出网;qi 没有内置的网络开关
(`--offline` 被接受但不做任何事,因为启动期本来就没有网络操作)。

## 与其它页面的关系

- [security.md](security.md):有哪些闸门、哪些地方没有闸门;信任门控挡什么。
- [bash-allowlist.md](../design/bash-allowlist.md):为什么不做命令白名单(以及它为什么不是边界)。
