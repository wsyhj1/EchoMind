# EchoMind 检索配对评测

Gold 状态：待人工审核；以下为合成语料暂定指标，不代表生产准确率。

样本数：100；K=5；对照 vector_only → vector_plus_graph；未启用证据补检。

| 指标 | vector_only | vector_plus_graph | 差值 |
|---|---:|---:|---:|
| recall_at_k | 0.9205 | 0.9848 | +0.0644 |
| hit_at_k | 1.0000 | 1.0000 | +0.0000 |
| complete_evidence_at_k | 0.8295 | 0.9545 | +0.1250 |
| mrr_at_k | 0.9697 | 0.9924 | +0.0227 |

| 题型 | 样本数 | vector_only CompleteEvidence | vector_plus_graph CompleteEvidence |
|---|---:|---:|---:|
| ambiguous | 15 | 1.0 | 1.0 |
| distractor | 10 | 1.0 | 1.0 |
| isolation | 5 | 1.0 | 1.0 |
| multi_evidence | 20 | 0.65 | 0.8 |
| multi_hop | 20 | 0.6 | 1.0 |
| single_fact | 20 | 1.0 | 1.0 |
| unanswerable | 10 | unavailable | unavailable |

| 运行 | 失败题数 | 降级题数 | P50 ms | P95 ms |
|---|---:|---:|---:|---:|
| vector_only | 0 | 0 | 6626.416550017893 | 8475.374495028518 |
| vector_plus_graph | 0 | 0 | 5367.639450007118 | 6230.767170025501 |

## 逐题变化

| 题号 | 类型 | vector_only 状态 | vector_plus_graph 状态 | 证据集合相同 | 最终顺序相同 | CompleteEvidence 差值 |
|---|---|---|---|---|---|---:|
| Q001 | single_fact | completed | completed | True | True | 0.0 |
| Q002 | single_fact | completed | completed | True | True | 0.0 |
| Q003 | single_fact | completed | completed | True | True | 0.0 |
| Q004 | single_fact | completed | completed | True | True | 0.0 |
| Q005 | single_fact | completed | completed | True | True | 0.0 |
| Q006 | single_fact | completed | completed | True | True | 0.0 |
| Q007 | single_fact | completed | completed | True | True | 0.0 |
| Q008 | single_fact | completed | completed | True | True | 0.0 |
| Q009 | single_fact | completed | completed | True | True | 0.0 |
| Q010 | single_fact | completed | completed | True | True | 0.0 |
| Q011 | single_fact | completed | completed | True | True | 0.0 |
| Q012 | single_fact | completed | completed | True | True | 0.0 |
| Q013 | single_fact | completed | completed | True | True | 0.0 |
| Q014 | single_fact | completed | completed | True | True | 0.0 |
| Q015 | single_fact | completed | completed | True | True | 0.0 |
| Q016 | single_fact | completed | completed | True | True | 0.0 |
| Q017 | single_fact | completed | completed | True | True | 0.0 |
| Q018 | single_fact | completed | completed | True | True | 0.0 |
| Q019 | single_fact | completed | completed | True | True | 0.0 |
| Q020 | single_fact | completed | completed | True | True | 0.0 |
| Q021 | multi_hop | completed | completed | False | False | 1.0 |
| Q022 | multi_hop | completed | completed | True | False | 0.0 |
| Q023 | multi_hop | completed | completed | False | False | 1.0 |
| Q024 | multi_hop | completed | completed | False | False | 1.0 |
| Q025 | multi_hop | completed | completed | False | False | 1.0 |
| Q026 | multi_hop | completed | completed | True | True | 0.0 |
| Q027 | multi_hop | completed | completed | True | True | 0.0 |
| Q028 | multi_hop | completed | completed | True | True | 0.0 |
| Q029 | multi_hop | completed | completed | True | True | 0.0 |
| Q030 | multi_hop | completed | completed | True | True | 0.0 |
| Q031 | multi_hop | completed | completed | True | True | 0.0 |
| Q032 | multi_hop | completed | completed | True | True | 0.0 |
| Q033 | multi_hop | completed | completed | True | True | 0.0 |
| Q034 | multi_hop | completed | completed | True | True | 0.0 |
| Q035 | multi_hop | completed | completed | True | True | 0.0 |
| Q036 | multi_hop | completed | completed | False | False | 1.0 |
| Q037 | multi_hop | completed | completed | False | False | 1.0 |
| Q038 | multi_hop | completed | completed | True | False | 0.0 |
| Q039 | multi_hop | completed | completed | False | False | 1.0 |
| Q040 | multi_hop | completed | completed | False | False | 1.0 |
| Q041 | multi_evidence | completed | completed | False | False | 1.0 |
| Q042 | multi_evidence | completed | completed | False | False | 1.0 |
| Q043 | multi_evidence | completed | completed | True | True | 0.0 |
| Q044 | multi_evidence | completed | completed | True | True | 0.0 |
| Q045 | multi_evidence | completed | completed | False | False | 0.0 |
| Q046 | multi_evidence | completed | completed | True | True | 0.0 |
| Q047 | multi_evidence | completed | completed | True | True | 0.0 |
| Q048 | multi_evidence | completed | completed | True | True | 0.0 |
| Q049 | multi_evidence | completed | completed | True | True | 0.0 |
| Q050 | multi_evidence | completed | completed | True | True | 0.0 |
| Q051 | multi_evidence | completed | completed | True | False | 0.0 |
| Q052 | multi_evidence | completed | completed | True | True | 0.0 |
| Q053 | multi_evidence | completed | completed | True | True | 0.0 |
| Q054 | multi_evidence | completed | completed | True | True | 0.0 |
| Q055 | multi_evidence | completed | completed | True | True | 0.0 |
| Q056 | multi_evidence | completed | completed | True | False | 0.0 |
| Q057 | multi_evidence | completed | completed | False | False | 1.0 |
| Q058 | multi_evidence | completed | completed | True | True | 0.0 |
| Q059 | multi_evidence | completed | completed | True | True | 0.0 |
| Q060 | multi_evidence | completed | completed | True | False | 0.0 |
| Q061 | ambiguous | completed | completed | True | True | 0.0 |
| Q062 | ambiguous | completed | completed | True | True | 0.0 |
| Q063 | ambiguous | completed | completed | True | True | 0.0 |
| Q064 | ambiguous | completed | completed | True | True | 0.0 |
| Q065 | ambiguous | completed | completed | True | True | 0.0 |
| Q066 | ambiguous | completed | completed | True | True | 0.0 |
| Q067 | ambiguous | completed | completed | True | True | 0.0 |
| Q068 | ambiguous | completed | completed | True | True | 0.0 |
| Q069 | ambiguous | completed | completed | True | True | 0.0 |
| Q070 | ambiguous | completed | completed | True | True | 0.0 |
| Q071 | ambiguous | completed | completed | True | True | 0.0 |
| Q072 | ambiguous | completed | completed | True | True | 0.0 |
| Q073 | ambiguous | completed | completed | True | True | 0.0 |
| Q074 | ambiguous | completed | completed | True | True | 0.0 |
| Q075 | ambiguous | completed | completed | True | True | 0.0 |
| Q076 | distractor | completed | completed | True | False | 0.0 |
| Q077 | distractor | completed | completed | True | True | 0.0 |
| Q078 | distractor | completed | completed | True | True | 0.0 |
| Q079 | distractor | completed | completed | True | True | 0.0 |
| Q080 | distractor | completed | completed | True | True | 0.0 |
| Q081 | distractor | completed | completed | True | True | 0.0 |
| Q082 | distractor | completed | completed | True | True | 0.0 |
| Q083 | distractor | completed | completed | True | True | 0.0 |
| Q084 | distractor | completed | completed | True | True | 0.0 |
| Q085 | distractor | completed | completed | True | True | 0.0 |
| Q086 | unanswerable | completed | completed | True | True | None |
| Q087 | unanswerable | completed | completed | True | True | None |
| Q088 | unanswerable | completed | completed | True | True | None |
| Q089 | unanswerable | completed | completed | True | True | None |
| Q090 | unanswerable | completed | completed | True | False | None |
| Q091 | unanswerable | completed | completed | True | True | None |
| Q092 | unanswerable | completed | completed | True | True | None |
| Q093 | unanswerable | completed | completed | True | True | None |
| Q094 | unanswerable | completed | completed | True | True | None |
| Q095 | unanswerable | completed | completed | True | False | None |
| Q096 | isolation | completed | completed | True | True | None |
| Q097 | isolation | completed | completed | True | True | None |
| Q098 | isolation | completed | completed | True | True | 0.0 |
| Q099 | isolation | completed | completed | True | True | 0.0 |
| Q100 | isolation | completed | completed | True | True | 0.0 |

变化题目：Q021, Q022, Q023, Q024, Q025, Q036, Q037, Q038, Q039, Q040, Q041, Q042, Q045, Q051, Q056, Q057, Q060, Q076, Q090, Q095

延迟包含真实外部服务波动，单次串行运行不构成性能提升结论。无答案题的召回指标为 unavailable；本报告不评估拒答或答案正确率。
失败题仍保留在样本数中；已解析 Gold 的运行失败按未命中计，未能解析 Gold 的题目不伪造零分。
