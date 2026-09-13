/**
 * 设置面 —— 布局照 dsh(左分区导航 + 右侧卡片式「标签/值」行),
 * **内容按 qi 的实际能力填**:模型与凭证 / Agent / 技能 / 插件 / 会话与路径 / 诊断。
 *
 * 取值全部来自真实端点(`/api/config`、`/api/agents`、`/api/skills`、`/api/plugins`),
 * 没有的字段就不显示 —— 不编造 dsh 有而 qi 没有的项目(它那些「沙箱/审批/遥测」qi 还没有)。
 *
 * 唯一可写动作是**凭证**:必须过二次确认层(显示目标文件绝对路径 + 掩码值),
 * 请求再带 `X-Qi-Confirm: yes`(后端也拦,双保险)。
 */
import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type {
  AgentInfo,
  ConfigView,
  PluginList,
  SkillInfo,
} from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";

type SectionKey =
  | "models"
  | "agents"
  | "skills"
  | "plugins"
  | "sessions"
  | "diagnostics";

const SECTIONS: { key: SectionKey; label: string }[] = [
  { key: "models", label: "模型" },
  { key: "agents", label: "Agent" },
  { key: "skills", label: "技能" },
  { key: "plugins", label: "插件" },
  { key: "sessions", label: "会话与路径" },
  { key: "diagnostics", label: "诊断" },
];

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="row">
      <span className="row__label">{label}</span>
      <span className="row__value">{value}</span>
    </div>
  );
}

export function Settings({
  config,
  onChanged,
  onError,
}: {
  config: ConfigView | null;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const [section, setSection] = useState<SectionKey>("models");
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [plugins, setPlugins] = useState<string[]>([]);
  const [editing, setEditing] = useState<{
    provider: string;
    key: string;
  } | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [agentList, skillList, pluginList] = await Promise.all([
          api.agents(),
          api.skills(),
          api.plugins(),
        ]);
        if (!alive) return;
        setAgents(agentList.agents);
        setSkills(skillList.skills);
        setPlugins((pluginList as PluginList).plugins);
      } catch (err) {
        onError(err instanceof ApiError ? err.detail : String(err));
      }
    })();
    return () => {
      alive = false;
    };
  }, [onError]);

  const runWrite = (action: () => Promise<void>) => {
    void (async () => {
      try {
        await action();
        onChanged();
      } catch (err) {
        onError(err instanceof ApiError ? err.detail : String(err));
      }
    })();
  };

  const authPath = config?.auth_file ?? "(未知)";

  return (
    <div className="settings">
      <nav className="settings__nav" aria-label="设置分区">
        {SECTIONS.map((item) => (
          <button
            key={item.key}
            type="button"
            className="settings__nav-item"
            aria-current={section === item.key}
            onClick={() => setSection(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div className="settings__body">
        {section === "models" ? (
          <section className="card">
            <div className="card__title">模型与凭证</div>
            <div className="card__desc">
              来自 <code>models.json</code> + <code>auth.json</code>
              ;凭证只显示尾 4 位。
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th>provider</th>
                  <th>baseUrl</th>
                  <th>api</th>
                  <th>模型</th>
                  <th>凭证</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(config?.providers ?? []).map((provider) => (
                  <tr key={provider.name}>
                    <td className="mono">{provider.name}</td>
                    <td className="mono">{provider.base_url ?? "(内置)"}</td>
                    <td className="mono">{provider.api ?? "—"}</td>
                    <td>{provider.models}</td>
                    <td>
                      {provider.credential_ok ? (
                        <span className="check--ok">
                          ✓{" "}
                          {provider.credential_masked ||
                            provider.credential_source}
                        </span>
                      ) : (
                        <span className="check--bad">
                          ✗ {provider.credential_source}
                        </span>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="ghost"
                        onClick={() =>
                          setEditing({ provider: provider.name, key: "" })
                        }
                      >
                        设置凭证
                      </button>
                      {provider.credential_source === "auth" ? (
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => setRemoving(provider.name)}
                        >
                          清除
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Row label="默认模型" value={config?.default_model ?? "(未配置)"} />
            {config?.router_model ? (
              <Row label="分派模型" value={config.router_model} />
            ) : null}
            <Row
              label="配置文件"
              value={(config?.config_files ?? []).join("\n") || "未找到"}
            />
            <Row
              label="设置文件"
              value={(config?.settings_files ?? []).join("\n") || "未找到"}
            />
          </section>
        ) : null}

        {section === "agents" ? (
          <section className="card">
            <div className="card__title">Agent({agents.length})</div>
            <div className="card__desc">
              优先级:项目 <code>.qi/agents/</code> &gt; 用户{" "}
              <code>~/.qi/agent/agents/</code> &gt; 包内置。tools 为{" "}
              <code>*</code> 时展开成实际清单。
            </div>
            {agents.map((agent) => (
              <div className="card" key={agent.name}>
                <div className="row">
                  <span className="row__label">
                    <strong>{agent.display_name || agent.name}</strong>
                    <span className="mono"> ({agent.name})</span>
                  </span>
                  <span className="row__value">{agent.source}</span>
                </div>
                {agent.description ? (
                  <div className="card__desc">{agent.description}</div>
                ) : null}
                <Row label="工具" value={agent.tools.join(", ") || "(无)"} />
                <Row
                  label="资源"
                  value={`技能 ${agent.skills} · 数据源 ${agent.data_sources} · 私有 MCP ${agent.mcp_private}`}
                />
                {agent.keywords.length > 0 ? (
                  <Row label="关键词" value={agent.keywords.join(", ")} />
                ) : null}
              </div>
            ))}
          </section>
        ) : null}

        {section === "skills" ? (
          <section className="card">
            <div className="card__title">技能({skills.length})</div>
            <div className="card__desc">
              渐进披露:只有描述进系统提示词,正文由 agent 需要时用{" "}
              <code>read</code> 读全文。
            </div>
            {skills.length === 0 ? (
              <div className="card__desc">
                没有发现技能。放到 ~/.qi/agent/skills/ 或项目 .qi/skills/ 。
              </div>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>来源</th>
                    <th>描述</th>
                  </tr>
                </thead>
                <tbody>
                  {skills.map((skill) => (
                    <tr key={skill.path || skill.name}>
                      <td className="mono">{skill.name}</td>
                      <td>{skill.source}</td>
                      <td>{skill.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        ) : null}

        {section === "plugins" ? (
          <section className="card">
            <div className="card__title">插件({plugins.length})</div>
            <div className="card__desc">
              两条通道:pip entry point <code>qi.plugins</code> + 本地目录
              <code> ~/.qi/agent/plugins/&lt;名&gt;/plugin.py</code>。
            </div>
            {plugins.length === 0 ? (
              <div className="card__desc">未装载任何插件。</div>
            ) : (
              plugins.map((name) => (
                <Row key={name} label="已装载" value={name} />
              ))
            )}
          </section>
        ) : null}

        {section === "sessions" ? (
          <section className="card">
            <div className="card__title">会话与路径</div>
            <Row label="会话目录" value={config?.session_dir ?? "(未知)"} />
            <Row label="凭证文件" value={authPath} />
            <div className="card__desc">
              会话是 JSONL 每会话一个文件;`settings.json` 的{" "}
              <code>sessionDir</code> 可覆盖目录。
            </div>
          </section>
        ) : null}

        {section === "diagnostics" ? (
          <section className="card">
            <div className="card__title">诊断</div>
            <div className="card__desc">
              等价于 <code>qi doctor</code> 的核心检查项。
            </div>
            {(config?.checks ?? []).map((check) => (
              <div className="row" key={check.name}>
                <span className="row__label">
                  <span className={check.ok ? "check--ok" : "check--bad"}>
                    {check.ok ? "✓" : "✗"}
                  </span>{" "}
                  {check.name}
                </span>
                <span className="row__value">{check.detail}</span>
              </div>
            ))}
          </section>
        ) : null}
      </div>

      {editing ? (
        <ConfirmDialog
          title={`写入 ${editing.provider} 的凭证?`}
          rows={[
            { label: "provider", value: editing.provider },
            { label: "目标文件", value: authPath },
            {
              label: "密钥",
              value: editing.key ? `…${editing.key.slice(-4)}` : "(空)",
            },
          ]}
          warning="会覆盖该 provider 现有凭证;文件权限置为 0600。"
          confirmLabel="确认写入"
          onCancel={() => setEditing(null)}
          onConfirm={() => {
            const target = editing;
            setEditing(null);
            if (!target.key) return;
            runWrite(() => api.setAuth(target.provider, target.key));
          }}
        >
          <input
            value={editing.key}
            autoFocus
            aria-label="API key"
            placeholder="粘贴 API key"
            onChange={(event) =>
              setEditing({ ...editing, key: event.target.value })
            }
          />
        </ConfirmDialog>
      ) : null}

      {removing ? (
        <ConfirmDialog
          title={`清除 ${removing} 的凭证?`}
          rows={[
            { label: "provider", value: removing },
            { label: "目标文件", value: authPath },
          ]}
          warning="清除后该 provider 会变成缺密钥状态。"
          danger
          confirmLabel="确认清除"
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const target = removing;
            setRemoving(null);
            runWrite(() => api.deleteAuth(target));
          }}
        />
      ) : null}
    </div>
  );
}
