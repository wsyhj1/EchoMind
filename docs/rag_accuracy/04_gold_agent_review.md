# Gold 全量子代理审核（agent_review）

审核日期：2026-10-23（Asia/Shanghai）。审核版本：`echomind-gold-agent-review-2026-10-23-v1`。

本次按用户要求由子代理逐题检查100题及16份合成原文，检查答案事实、必要证据、用户与知识库范围、时间、版本和题型。它是代理来源审核，不是已经发生的真人审核。旧集 `pending_human_review`、旧实验结果和明确要求真人的 gate 均未改写。

## 审核结论

88道有答案题的短答案事实均可在允许范围的原文中找到；12道未知或隔离题的无答案标注成立。没有发现跨租户或跨指定知识库的 Gold 泄漏。

标注仍有明确缺陷：2题把辅助文档误当成必需证据，12题操作答案遗漏强制前置备份，4题要求关系链却只给末端团队。另有2题使用未固定的相对时间，4题时长短答案缺少单位。修订已另存v2，原v1及fixtures生成器保持不变。

逐题判断计数：`{"accept_with_notes": 19, "accept": 61, "revise_annotation": 20}`。有修订并不意味着旧答案含有假事实；应区分‘有事实支持’与‘完整回答了问题和操作前提’。

## 必要证据与干扰题

| 题号 | 判断 | v2处理 |
|---|---|---|
| Q079 | order_architecture直接说明7200秒，足以回答不是60秒；decoy不必需。 | 只保留order_architecture为必需源。 |
| Q083 | decoy逐字说明培训支持组不维护生产赤霄；ownership不是问题必需。 | 只保留decoy为必需源。 |
| Q076 | 问句与Gold同时包含生产实例映射、培训组否定和实际团队；原3源完整支持。 | 保留architecture＋ownership＋decoy的保守证据政策。 |
| Q080 | history明确旧职责2025终止，ownership明确2026实际维护组。 | 保留两源；不单凭正向维护关系假设排他性。 |
| Q077/Q078/Q084 | 演练实例、60秒和不同实例均在decoy直接支持。 | 原标注正确。 |
| Q081/Q085 | 历史维护者及职责终止年份在history直接支持。 | 原标注正确，不被2026新团队覆盖。 |

`evidence_metrics()`目前把gold_sources整体当作不可缺少的集合，因此Q079/Q083的过标注会误罚有充分证据的输出。v2修正只作用于新标注，不追写旧报告；如以后引入替代证据组，需要按完整声明覆盖评分，不能只计算所有候选证明的并集。

## 答案完整性与题型

操作类需补备份：Q041、Q045、Q046、Q050、Q051、Q055、Q056、Q060、Q069–Q072。原动作与步骤顺序有支持；缺的是同一手册的‘执行前必须备份实例配置’，不能把漏前提的短答案当作完整可执行建议。v2只增加原文前置条件，没有增加新来源。

关系链题Q024/Q029/Q034/Q039应显示‘系统→具体缓存集群→维护团队’。原来只标团队不足以审核链路表达，虽不影响原两份证据的必要性。

Q061–Q075主要是口语改写，不能用category=ambiguous宣称已验证实际指代消歧。v2保留旧category方便对照，并加paraphrase审核类型。Q061–Q064补‘秒’；短实体名与完整实例名应允许等价匹配。

Q005/Q044/Q065/Q069使用短名赤霄；语料同时有赤霄演练缓存。此处保留生产实例解释，但明记澄清可接受，后续应增加真正多候选、无上下文的别名题。该容错条款需要答案审核器实现，不能假称现有证据ID指标已经实现。

## 时间、版本与权限

v2所有题增加固定as_of=2026-10-23，表示合成文档快照的解释日期，不是从外部核验的真实系统事实。Q082改为‘根据2026年文档快照，赤霄缓存集群计划于2028年的迁移已经执行了吗？’；Q093改为‘根据2026年文档快照，支付系统在2027年的维护团队是谁？’。Q082仍为尚未执行，Q093仍无答案；2026缓存团队不能外推成2027整个系统团队。

Q086不应将2026团队推成2029负责人。Q092中的2026是文档版本，不是数据库版本。Q094应指出迁移未执行，不能编造验收号。其余无答案题均应限定为‘现有授权文档无法确定’，不能宣称现实中该信息不存在。

权限规则已对照实际代码：`默认知识库`表示当前用户所有知识库，指定库只检索该库。Q096不能读租户乙私有标记；Q097不能越指定库；Q098允许当前用户跨自己的库获取合规维护组；Q099/Q100是相应正控制。全部Gold源的user_id、指定库与原文quote检查通过。

## 新版本、复算与限制

新增 `evaluation/datasets/rag_v2_agent_reviewed.jsonl`，保留100题、全部负面样本和原语料版本；gold_review=agent_reviewed，human_reviewed=false，并记录dataset_version、review_version、as_of和预期声明。版本文件为审核快照；原fixtures脚本仍仅生成v1，避免悄悄改写旧基线。

`04_gold_v2_changes.json`列出100题的前后查询、答案、源组及query_changed。98题查询完全相同，可在固定原语料和源映射下另作离线证据复算。Q082/Q093必须重新检索，不能把旧问句结果冒充v2问句结果；任何答案rubric变化也不等于已有生成答案自动合格。

没有在本子任务运行云端检索或100题生成答案，不读取业务用户数据。仅看合成来源，不能证明回答准确率、引用质量、真实企业图谱效果或正式发布门槛。原16份语料很短，4个业务模板高度重复，没有真实同时间同实例互相矛盾的维护者、长文档跨块证据、PDF抽取难例或复杂版本链。后续应扩展独立留出集，再验证冲突保留、拒答、引用和业务数据效果。

## 校验与可追溯性

- 原数据 SHA256：`2d1dcde0b4ef2f3f67c3957f8d34568af8631d440f933f415e832bc2cab485eb`。
- fixtures SHA256：`647ddb3cd2acbb4b85279aec587a944ab2e5fd54ef173a6875f4b38141beed1a`。
- v2 SHA256：`d924d74baa233dde7b709bea2f4fa3c2258f474d7b464453e484e297d832e246`。
- 100题ID唯一且连续；逐项quote与原文完全一致；有答案题均有允许范围证据，无答案题源为空；v1与原生成器内容一致；生成前后v1和fixtures哈希不变。
- 逐题机读审核：`04_gold_agent_review.json`；逐题变更：`04_gold_v2_changes.json`。

## 逐题审核索引

| 题号 | 结论 | 必要源（v2） | 发现或预期声明 |
|---|---|---|---|
| Q001 | accept_with_notes | order_architecture | entity_alias_normalization |
| Q002 | accept | order_architecture | 订单系统缓存有效期为7200秒 |
| Q003 | accept | order_ownership | 赤霄缓存集群由基础设施甲组维护 |
| Q004 | accept | order_runbook | 赤霄缓存集群典型故障为连接池耗尽 |
| Q005 | accept_with_notes | order_runbook | short_alias_context_risk |
| Q006 | accept_with_notes | payment_architecture | entity_alias_normalization |
| Q007 | accept | payment_architecture | 支付系统缓存有效期为1800秒 |
| Q008 | accept | payment_ownership | 青岚缓存集群由支付平台乙组维护 |
| Q009 | accept | payment_runbook | 青岚缓存集群典型故障为主从切换 |
| Q010 | accept | payment_runbook | 执行前必须备份实例配置 |
| Q011 | accept_with_notes | stock_architecture | entity_alias_normalization |
| Q012 | accept | stock_architecture | 库存系统缓存有效期为3600秒 |
| Q013 | accept | stock_ownership | 白泽缓存集群由仓储技术丙组维护 |
| Q014 | accept | stock_runbook | 白泽缓存集群典型故障为缓存击穿 |
| Q015 | accept | stock_runbook | 执行前必须备份实例配置 |
| Q016 | accept_with_notes | shipping_architecture | entity_alias_normalization |
| Q017 | accept | shipping_architecture | 物流系统缓存有效期为900秒 |
| Q018 | accept | shipping_ownership | 玄鹤缓存集群由履约服务丁组维护 |
| Q019 | accept | shipping_runbook | 玄鹤缓存集群典型故障为内存告警 |
| Q020 | accept | shipping_runbook | 执行前必须备份实例配置 |
| Q021 | accept | order_architecture+order_ownership | 订单系统使用赤霄缓存集群；赤霄缓存集群由基础设施甲组维护 |
| Q022 | accept | order_architecture+order_ownership | 订单系统使用赤霄缓存集群；赤霄缓存集群由基础设施甲组维护 |
| Q023 | accept | order_architecture+order_ownership | 订单系统使用赤霄缓存集群；赤霄缓存集群由基础设施甲组维护 |
| Q024 | revise_annotation | order_architecture+order_ownership | chain_answer_incomplete |
| Q025 | accept | order_architecture+order_ownership | 订单系统使用赤霄缓存集群；赤霄缓存集群由基础设施甲组维护 |
| Q026 | accept | payment_architecture+payment_ownership | 支付系统使用青岚缓存集群；青岚缓存集群由支付平台乙组维护 |
| Q027 | accept | payment_architecture+payment_ownership | 支付系统使用青岚缓存集群；青岚缓存集群由支付平台乙组维护 |
| Q028 | accept | payment_architecture+payment_ownership | 支付系统使用青岚缓存集群；青岚缓存集群由支付平台乙组维护 |
| Q029 | revise_annotation | payment_architecture+payment_ownership | chain_answer_incomplete |
| Q030 | accept | payment_architecture+payment_ownership | 支付系统使用青岚缓存集群；青岚缓存集群由支付平台乙组维护 |
| Q031 | accept | stock_architecture+stock_ownership | 库存系统使用白泽缓存集群；白泽缓存集群由仓储技术丙组维护 |
| Q032 | accept | stock_architecture+stock_ownership | 库存系统使用白泽缓存集群；白泽缓存集群由仓储技术丙组维护 |
| Q033 | accept | stock_architecture+stock_ownership | 库存系统使用白泽缓存集群；白泽缓存集群由仓储技术丙组维护 |
| Q034 | revise_annotation | stock_architecture+stock_ownership | chain_answer_incomplete |
| Q035 | accept | stock_architecture+stock_ownership | 库存系统使用白泽缓存集群；白泽缓存集群由仓储技术丙组维护 |
| Q036 | accept | shipping_architecture+shipping_ownership | 物流系统使用玄鹤缓存集群；玄鹤缓存集群由履约服务丁组维护 |
| Q037 | accept | shipping_architecture+shipping_ownership | 物流系统使用玄鹤缓存集群；玄鹤缓存集群由履约服务丁组维护 |
| Q038 | accept | shipping_architecture+shipping_ownership | 物流系统使用玄鹤缓存集群；玄鹤缓存集群由履约服务丁组维护 |
| Q039 | revise_annotation | shipping_architecture+shipping_ownership | chain_answer_incomplete |
| Q040 | accept | shipping_architecture+shipping_ownership | 物流系统使用玄鹤缓存集群；玄鹤缓存集群由履约服务丁组维护 |
| Q041 | revise_annotation | order_architecture+order_ownership+order_runbook | mandatory_precondition_omitted |
| Q042 | accept | order_architecture+order_ownership | 订单系统使用赤霄缓存集群；订单系统缓存有效期为7200秒；赤霄缓存集群由基础设施甲组维护 |
| Q043 | accept | order_architecture+order_runbook | 订单系统使用赤霄缓存集群；赤霄缓存集群典型故障为连接池耗尽 |
| Q044 | accept_with_notes | order_ownership+order_runbook | short_alias_context_risk |
| Q045 | revise_annotation | order_architecture+order_ownership+order_runbook | mandatory_precondition_omitted |
| Q046 | revise_annotation | payment_architecture+payment_ownership+payment_runbook | mandatory_precondition_omitted |
| Q047 | accept | payment_architecture+payment_ownership | 支付系统使用青岚缓存集群；支付系统缓存有效期为1800秒；青岚缓存集群由支付平台乙组维护 |
| Q048 | accept | payment_architecture+payment_runbook | 支付系统使用青岚缓存集群；青岚缓存集群典型故障为主从切换 |
| Q049 | accept | payment_ownership+payment_runbook | 青岚缓存集群由支付平台乙组维护；执行前必须备份实例配置 |
| Q050 | revise_annotation | payment_architecture+payment_ownership+payment_runbook | mandatory_precondition_omitted |
| Q051 | revise_annotation | stock_architecture+stock_ownership+stock_runbook | mandatory_precondition_omitted |
| Q052 | accept | stock_architecture+stock_ownership | 库存系统使用白泽缓存集群；库存系统缓存有效期为3600秒；白泽缓存集群由仓储技术丙组维护 |
| Q053 | accept | stock_architecture+stock_runbook | 库存系统使用白泽缓存集群；白泽缓存集群典型故障为缓存击穿 |
| Q054 | accept | stock_ownership+stock_runbook | 白泽缓存集群由仓储技术丙组维护；执行前必须备份实例配置 |
| Q055 | revise_annotation | stock_architecture+stock_ownership+stock_runbook | mandatory_precondition_omitted |
| Q056 | revise_annotation | shipping_architecture+shipping_ownership+shipping_runbook | mandatory_precondition_omitted |
| Q057 | accept | shipping_architecture+shipping_ownership | 物流系统使用玄鹤缓存集群；物流系统缓存有效期为900秒；玄鹤缓存集群由履约服务丁组维护 |
| Q058 | accept | shipping_architecture+shipping_runbook | 物流系统使用玄鹤缓存集群；玄鹤缓存集群典型故障为内存告警 |
| Q059 | accept | shipping_ownership+shipping_runbook | 玄鹤缓存集群由履约服务丁组维护；执行前必须备份实例配置 |
| Q060 | revise_annotation | shipping_architecture+shipping_ownership+shipping_runbook | mandatory_precondition_omitted |
| Q061 | accept_with_notes | order_architecture | category_is_paraphrase, unit_normalization |
| Q062 | accept_with_notes | payment_architecture | category_is_paraphrase, unit_normalization |
| Q063 | accept_with_notes | stock_architecture | category_is_paraphrase, unit_normalization |
| Q064 | accept_with_notes | shipping_architecture | category_is_paraphrase, unit_normalization |
| Q065 | accept_with_notes | order_ownership | short_alias_context_risk, category_is_paraphrase |
| Q066 | accept_with_notes | payment_ownership | category_is_paraphrase |
| Q067 | accept_with_notes | stock_ownership | category_is_paraphrase |
| Q068 | accept_with_notes | shipping_ownership | category_is_paraphrase |
| Q069 | revise_annotation | order_runbook | mandatory_precondition_omitted, short_alias_context_risk, category_is_paraphrase |
| Q070 | revise_annotation | payment_runbook | mandatory_precondition_omitted, category_is_paraphrase |
| Q071 | revise_annotation | stock_runbook | mandatory_precondition_omitted, category_is_paraphrase |
| Q072 | revise_annotation | shipping_runbook | mandatory_precondition_omitted, category_is_paraphrase |
| Q073 | accept_with_notes | order_architecture | category_is_paraphrase, entity_alias_normalization |
| Q074 | accept_with_notes | payment_architecture | category_is_paraphrase, entity_alias_normalization |
| Q075 | accept_with_notes | stock_architecture | category_is_paraphrase, entity_alias_normalization |
| Q076 | accept_with_notes | order_architecture+order_ownership+decoy | negative_evidence_policy |
| Q077 | accept | decoy | 赤霄演练缓存由培训支持组维护 |
| Q078 | accept | decoy | 订单演练系统使用赤霄演练缓存；演练环境缓存有效期为60秒 |
| Q079 | revise_annotation | order_architecture | unnecessary_required_source |
| Q080 | accept_with_notes | history+order_ownership | negative_evidence_policy |
| Q081 | accept | history | 2024年赤霄缓存集群由旧运维组维护 |
| Q082 | revise_annotation | history | relative_time_not_frozen |
| Q083 | revise_annotation | decoy | unnecessary_required_source |
| Q084 | accept | decoy | 赤霄演练缓存与赤霄缓存集群是两个不同实例 |
| Q085 | accept | history | 旧运维组对赤霄缓存集群的职责于2025年终止 |
| Q086 | accept | 无；拒答/权限限定 | 允许范围的文档未提供2029年负责人，不能将2026团队外推为2029负责人 |
| Q087 | accept | 无；拒答/权限限定 | 职责登记明确说未登记值班电话，不能编造号码 |
| Q088 | accept | 无；拒答/权限限定 | 允许范围的文档未提供青岚实例IP |
| Q089 | accept | 无；拒答/权限限定 | 允许范围的文档未提供白泽访问密码 |
| Q090 | accept | 无；拒答/权限限定 | 允许范围的文档未提供玄鹤月度费用 |
| Q091 | accept | 无；拒答/权限限定 | 架构仅写依赖星河审计服务，未提供其SLA数字 |
| Q092 | accept | 无；拒答/权限限定 | 允许范围的文档未提供订单系统数据库版本；2026是文档版本而非数据库版本 |
| Q093 | revise_annotation | 无；拒答/权限限定 | relative_time_not_frozen |
| Q094 | accept | 无；拒答/权限限定 | 文档说明迁移尚未执行，不能提供已完成验收编号 |
| Q095 | accept | 无；拒答/权限限定 | 允许范围的文档未提供旧运维组2026负责人手机号 |
| Q096 | accept | 无；拒答/权限限定 | user_id=1001不能使用user_id=1002的秘密标记；应说明当前权限范围没有答案 |
| Q097 | accept | 无；拒答/权限限定 | 指定rag_accuracy_v1不能使用rag_accuracy_other的独立标记 |
| Q098 | accept | other_kb | 默认知识库表示本用户所有知识库；user_id=1001可读other_kb，答案为合规维护组 |
| Q099 | accept | private | user_id=1002在自己的库内可读TENANT_B_ONLY_791 |
| Q100 | accept | other_kb | user_id=1001指定rag_accuracy_other可读OTHER_KB_ONLY_381 |
