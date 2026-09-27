# 建设体质辨识建议留痕与复核服务基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。在其稳定边界之上，`advice_service` 实现体质辨识**建议留痕与复核**：

- **授权先行、最少必要**：记录问询摘要前必须取得参与者授权，授权只登记用途范围（`intake_record`/`advice_generation`/`advice_disclosure`）与最少必要字段清单；问询字段超出清单直接拒绝。
- **规则版本快照**：建议依据起草当时有效的规则版本，并冻结规则内容哈希；规则失效后，引用旧快照的草稿不能发布、已发布版本被解释为失效。
- **复核版本链**：补充信息只能生成新版本（`supplement` 须填写补充说明），旧版本转为 `superseded` 并保留替换原因，可查但不再作为当前结论。
- **文化体验与诊断分离**：每个版本固定附带非诊断声明，建议区分 `lifestyle`（生活方式建议）与 `risk_alert`（就医风险提示）。
- **专家分签**：每位专家只能在草稿上添加并签署自己负责的段落；签名哈希覆盖段落内容、问询摘要、规则快照、授权范围与非诊断声明。
- **发布闸口**：发布时一次性确认授权范围（含披露用途）、规则快照、全部签署（签署人在职、内容未改）均有效。
- **撤回与留痕**：撤回授权后立即擦除该授权下问询摘要与建议正文、禁止继续使用，但版本结构、签名哈希与全部历史访问审计事实保留。
- **按角色脱敏与可解释**：审计员只能看到结构与哈希、看不到健康内容；`/advice-explain` 可说明任意一条建议当前生效或失效的具体原因。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、建议留痕复核服务、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、建议留痕复核与端到端验收测试。

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
```

验收命令会在临时 SQLite 数据库中登记机构、操作者、站点和参考资料，然后走完整链路：授权 → 问询首版 → 两位专家分签 → 发布 → 审计员脱敏核对 → 补充过敏史生成第二版 → 旧版失效留痕 → 撤回授权后擦除但保留访问事实，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

### 建议留痕复核接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/rule-versions` | reviewer/admin | 登记新生效规则版本（旧版本自动失效） |
| POST | `/rule-versions/retire` | reviewer/admin | 宣布规则版本失效 |
| GET | `/rule-versions?rule_set_id=&version=` | 全部角色 | 查询规则版本（审计员只见哈希，不见正文） |
| POST | `/consents` | operator/admin | 登记参与者授权（用途范围 + 字段清单） |
| POST | `/consents/withdraw` | operator/admin | 撤回授权并擦除受权内容 |
| POST | `/consultations` | operator/admin | 在有效授权下开启辨识会话 |
| GET | `/consultations?consultation_id=` | 全部角色 | 查看版本链（审计员见参与者脱敏） |
| POST | `/advice-versions` | operator/admin | 依据问询摘要生成新复核版本（补充版本带 `trigger=supplement`、`supplement_summary`，新字段需另传 `consent_id`） |
| POST | `/advice-sections` | expert/admin | 在草稿上添加本人负责的建议段 |
| POST | `/advice-sections/sign` | expert/admin | 专家签署本人段落 |
| POST | `/advice-versions/publish` | reviewer/admin | 发布闸口：校验授权范围、规则快照、全部签署 |
| GET | `/advice-versions?version_id=` | 全部角色 | 查看版本（含生效/失效原因、按角色脱敏） |
| GET | `/advice-explain?section_id=` | 全部角色 | 说明某条建议为何生效或失效 |
| GET | `/access-records?consultation_id=` | auditor/admin | 查询敏感信息访问留痕（撤回后仍保留） |

