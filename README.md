# Ascend-SGLang-Skills

OpenCode 的自定义技能（Skill）是封装特定指令与工作流的 `.md` 文件，能有效提升 AI 代理执行复杂任务时的稳定性和规范性。以下是自定义两种作用域技能的核心方法。

### 项目级 / 全局级技能

OpenCode的技能分为**项目级**和**全局级**两种，它们的主要区别在于作用范围、存放路径和优先级。具体对比如下：

| 特性 | 项目级技能 (Project Skills) | 全局级技能 (Global Skills) |
| :--- | :--- | :--- |
| **作用范围** | 仅在特定项目内生效，适合存放与当前项目强相关的规范流程。 | 本机全局生效，适合存放通用的、与具体项目无关的技能。 |
| **存放路径** | `<项目根目录>/.opencode/skills/<技能名>/SKILL.md` | `<用户主目录>/.config/opencode/skills/<技能名>/SKILL.md` |
| **优先级** | **高**。当项目级和全局级存在同名技能时，项目级技能会覆盖全局级技能。 | **低**，作为通用的基础规范。 |
| **团队协作** | 技能文件会随代码提交，团队成员克隆后即可自动获得一致的技能支持。 | 仅本机生效，不与团队共享，适合个人偏好设置。 |

> **备注**：OpenCode 为兼容 Claude Code，也支持加载 `.claude/skills/<name>/SKILL.md` 和 `~/.claude/skills/<name>/SKILL.md` 的技能文件。

### 管理技能：权限控制与调试
创建技能后，你可以通过权限配置和调试来管理其使用。

- **设置技能权限**：你可以在 `opencode.json` 中通过 `pattern` 来精细控制哪些技能可以被加载，权限类型包括 `allow`、`deny` 和 `ask`。
  ```json
  {
    "skills": {
      "permissions": {
        "internal-*": "allow",
        "experimental-skill": "ask",
        "deprecated-skill": "deny"
      }
    }
  }
  ```
- **调试和确认**：如果一个技能未能被正确加载，可以依次检查以下几点:
    1.  `SKILL.md` 文件名是否**全部为大写字母**。
    2.  Frontmatter 中是否同时包含了 **`name`** 和 **`description`**。
    3.  技能名称在所有作用域下是否**唯一**。
    4.  `opencode.json` 中的权限设置是否将技能设为 **`deny`**。
