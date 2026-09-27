# 建设体质辨识建议留痕与复核服务基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

`advice` 模块在此基础上实现**体质辨识建议的可复核留痕服务**，针对"口头建议像确定诊断、补充信息后看不出旧结论为何被替换"的投诉：

- **授权闸门与最小必要**：只有在参与者授权（字段范围 + 用途）生效后才能落盘问询摘要；记录字段超出授权范围一律拒绝（403）。撤回授权后内容对所有角色红acted，且不得再生成新版本。
- **规则版本快照**：建议版本绑定出具时有效的规则版本及其快照哈希；规则换版不改写历史，发布时重新确认规则仍在生效窗口。
- **复核版本链**：补充信息只能生成新版本（须注明 `supplementary_info` 等原因），旧版本标记 `superseded` 留档可查，仅当前版本（`published`）有效；撤回授权使当前版本 `invalidated`。
- **分段签署**：生活方式建议与就医风险提示分段，评审专家（reviewer）只能签署自己创建的段落；发布时逐条重算签名哈希，并校验签署人角色与组织。
- **发布重校验**：发布事务内重新确认授权范围、规则快照、全部签名，且必须同时包含生活方式建议和就医风险提示。
- **文化体验边界**：每个版本恒定附带 `nature=cultural_experience` 与"不属于医学诊断"声明，由服务渲染而非专家自由声明。
- **角色视角**：reviewer 见全量问询明细；operator/admin 仅见建议正文、问询字段隐藏；auditor 只见元数据；跨组织访问一律拒绝；撤回后所有角色只见红acted视图。
- **生效/失效解释**：`/advice/versions/explain` 面向 API 与自动化测试，结构化说明某版本为何生效（发布时间、规则快照、授权范围、签名有效性）或失效（被复核取代的版本与原因 / 授权撤回），不含健康内容。
- **审计事实保留**：撤回前的访问（`advice.accessed`）、发布、取代、作废等事件继续保留在哈希链上并可离线验链。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `advice.py` / `advice_api.py`：建议授权、规则版本、复核版本、分段签署与角色红acted；
  - `advice_acceptance.py`：建议留痕服务的离线端到端验收；
- `tests/`：基础规则、事务边界、接口路由、建议留痕服务和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m night_market_foundation.acceptance
PYTHONPATH=src python3 -m night_market_foundation.advice_acceptance
```

验收命令会在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，核对幂等回执与审计链；建议留痕验收额外走通"授权 → 规则快照 → 双人分段签署 → 发布 → 补充复核 → 规则换版 → 撤回红acted → 审计保留"完整链路。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

### 建议留痕接口（均以 `/advice` 开头）

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /advice/consents` | operator/admin/reviewer | 登记参与者授权（scope.fields 为字段白名单） |
| `POST /advice/consents/withdraw` | operator/admin/reviewer | 撤回授权，当前建议立即作废并红acted |
| `GET  /advice/consents?consent_id=` | 同组织全员 | 查询授权状态（撤回后字段清单红acted） |
| `POST /advice/rule-versions` | admin | 登记规则版本与生效时间，快照哈希留痕 |
| `POST /advice/rule-versions/supersede` | admin | 关闭规则版本生效窗口 |
| `GET  /advice/rule-versions?rule_version_id=` | 同组织全员 | 查询规则版本及当前是否生效 |
| `POST /advice/cases` | operator/admin/reviewer | 凭有效授权建立建议档案 |
| `POST /advice/versions` | operator/admin/reviewer | 起草首版或复核版本（复核须给 change_reason_code） |
| `POST /advice/sections` | reviewer | 新增 lifestyle / medical_risk 内容段 |
| `POST /advice/sections/sign` | reviewer | 仅能签署本人创建的内容段 |
| `POST /advice/versions/publish` | operator/admin/reviewer | 事务内重校验授权、规则快照与全部签名后发布 |
| `GET  /advice/versions?case_id=&version_no=` | 同组织全员 | 按角色红acted查看版本（每次访问入审计链） |
| `GET  /advice/versions/explain?case_id=&version_no=` | 同组织全员 | 说明版本生效/失效原因，不含健康内容 |
| `GET  /advice/cases?case_id=` | 同组织全员 | 版本链与当前版本号 |
