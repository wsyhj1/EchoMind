"""Independent report-domain holdout candidate, authored before graph adaptation.

Synthetic project reports, never claims about a deployed user's real report.
No cache/operations templates, model calls, or production data are used.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path

VERSION = "echomind-report-holdout-v1"
REVIEW = "agent_draft"
AS_OF = "2026-10-24"
KB = "report_holdout"


def documents():
    long_body = "\n".join(
        f"设计讨论第{i}段：阅读者从作品总览进入章节索引，再查看实验解释。此段只讨论呈现顺序、滚动位置和可读性，不能从页面排列推导模块依赖，也不提供任何实验成绩。"
        for i in range(1, 24))
    rows = [
        ("report_overview", "作品概览.md", "# 映澈作品台作品报告\n文档快照：2026年。映澈作品台简称映澈。\n映澈作品台具有证据定位功能。映澈作品台具有实验对比功能。\n" + long_body + "\n# 附录：限制\n映澈作品台尚未实现自动评分功能。未来计划加入跨作品推荐功能。", 4101, KB),
        ("implementation", "实现章节.md", "# 实现章节\n证据定位功能由页码锚点模块实现。实验对比功能由对照视图模块实现。\n页码锚点模块简称页签。\n映澈作品台包含表格规整模块。映澈作品台包含页码锚点模块。\n表格规整模块使用列边界模型。对照视图模块与页码锚点模块独立开发；文中未说明两者相互依赖。", 4101, KB),
        ("evaluation_data", "评估方案.md", "# 评估范围\n表格规整模块在版面样本集L2上评估。版面样本集L2包含180份匿名版面，不包含真实姓名与电话号码。\n页码锚点模块在章节导航集N1上评估。章节导航集N1仅用于定位，不用于打分训练。\n列边界模型在合成网格集G4上评估。合成网格集G4与版面样本集L2不是同一数据集。", 4101, KB),
        ("experiment", "消融章节.md", "# 2026年实验章节\n映澈作品台包含去除页码实验。去除页码实验在章节导航集N1上评估。\n去除页码实验报告定位命中率指标。去除页码实验报告平均跳转步数指标。\n去除页码实验的定位命中率为81.2%，平均跳转步数为2.8。该实验未报告准确率指标。", 4101, KB),
        ("table_extract", "表格抽取.txt", "原PDF表5按行抽取：\n实验 | 评估数据 | 宏平均F1 | 样本数\n列切分实验 | 版面样本集L2 | 0.842 | 180\n去噪实验 | 合成网格集G4 | 0.901 | 320\n表注：列切分实验与去噪实验的数据不同，不能把两行数值拼为一次实验。此表未报告置信区间。", 4101, KB),
        ("history", "历史结果.md", "2024年映澈作品台的定位命中率为67.0%。2024年版面样本集L0含80份版面。2026年已停止将L0用于当前评估。历史结果不能直接与新数据的结果比较。", 4101, KB),
        ("current_a", "定位结果初版.md", "2026年6月评估记录：页码锚点模块在章节导航集N1的定位命中率为93.0%。记录没有注明草稿或撤回。", 4101, KB),
        ("current_b", "定位结果复核.md", "2026年6月评估复核记录：页码锚点模块在章节导航集N1的定位命中率为91.5%。文中未解释与初版结果的差异，也未声明替代初版。", 4101, KB),
        ("alias_conflict", "别名登记.md", "对照视图模块也使用简称页签。页码锚点模块和对照视图模块是两个不同模块。只有短名页签时，本文无法唯一确定指向哪个模块。", 4101, KB),
        ("bibliography", "参考文献.md", "参考文献：某研究组. 网格模型与表格解析综述. 2025。示例系统可假设使用公开训练集。该引文与示例不代表映澈作品台实际采用这些数据或由该组开发。", 4101, KB),
        ("private", "另一作者私有附录.md", "另一作者私有实验的访问标记为 AUTHOR_PRIVATE_612。未对映澈作品台作者开放。", 4102, KB),
        ("other_space", "制作预算.md", "映澈作品台制作预算为860元。该表只在制作预算知识库登记。预算核对标记为 BUDGET_SPACE_735。", 4101, "report_budget"),
    ]
    return [{"doc_id": doc_id, "file_name": filename, "text": text, "user_id": user,
             "knowledge_base_id": kb, "format": "text", "corpus_version": VERSION,
             "text_sha256": sha256(text.encode()).hexdigest()} for doc_id, filename, text, user, kb in rows]


def dataset():
    docs = {row["doc_id"]: row for row in documents()}
    rows = []
    def add(query, category, refs, answer, *, user=4101, kb=KB, mode="answer", history=None):
        rows.append({"question_id": f"BH{len(rows)+1:03d}", "query": query, "category": category,
            "user_id": user, "knowledge_base_id": kb, "answerable": mode in ("answer", "conflict"),
            "answer_mode": mode, "gold_answer": answer,
            "gold_sources": [{"doc_id": doc_id, "quote": quote} for doc_id, quote in refs],
            "corpus_version": VERSION, "dataset_version": VERSION, "gold_review": REVIEW,
            "human_reviewed": False, "as_of": AS_OF, "history": history or []})
    def ref(doc_id, quote):
        assert quote in docs[doc_id]["text"]
        return doc_id, quote
    add("映澈作品台有哪些已实现的功能？", "long_report", [ref("report_overview", "映澈作品台具有证据定位功能。映澈作品台具有实验对比功能。")], "证据定位功能、实验对比功能；自动评分尚未实现，跨作品推荐只是计划。")
    add("证据定位功能由哪个模块实现？", "feature_module", [ref("implementation", "证据定位功能由页码锚点模块实现。")], "页码锚点模块")
    add("映澈作品台的证据定位功能由什么实现？", "cross_section", [ref("report_overview", "映澈作品台具有证据定位功能。"), ref("implementation", "证据定位功能由页码锚点模块实现。")], "映澈作品台→证据定位功能→页码锚点模块")
    add("表格规整模块用什么数据评估？", "dataset", [ref("evaluation_data", "表格规整模块在版面样本集L2上评估。")], "版面样本集L2")
    add("映澈作品台的表格规整模块在哪个数据集评估？", "cross_section", [ref("implementation", "映澈作品台包含表格规整模块。"), ref("evaluation_data", "表格规整模块在版面样本集L2上评估。")], "映澈作品台→表格规整模块→版面样本集L2")
    add("表格规整模块使用哪个模型？", "module_model", [ref("implementation", "表格规整模块使用列边界模型。")], "列边界模型")
    add("去除页码实验报告哪些指标？", "experiment_metric", [ref("experiment", "去除页码实验报告定位命中率指标。去除页码实验报告平均跳转步数指标。")], "定位命中率、平均跳转步数；未报告准确率。")
    add("去除页码实验的平均跳转步数是多少？", "numeric_fact", [ref("experiment", "去除页码实验的定位命中率为81.2%，平均跳转步数为2.8。")], "2.8步")
    add("映澈作品台的去除页码实验用哪份评估数据？", "cross_section", [ref("experiment", "映澈作品台包含去除页码实验。去除页码实验在章节导航集N1上评估。")], "章节导航集N1")
    add("表5中列切分实验的宏平均F1是多少，用了多少样本？", "table_row", [ref("table_extract", "实验 | 评估数据 | 宏平均F1 | 样本数\n列切分实验 | 版面样本集L2 | 0.842 | 180")], "宏平均F1为0.842，180个样本")
    add("去噪实验和列切分实验用了相同的数据吗？", "table_comparison", [ref("table_extract", "列切分实验 | 版面样本集L2 | 0.842 | 180\n去噪实验 | 合成网格集G4 | 0.901 | 320")], "不同；列切分用L2，去噪用G4。")
    add("2024年映澈作品台的定位命中率是多少？", "historical", [ref("history", "2024年映澈作品台的定位命中率为67.0%。")], "67.0%")
    add("2026年6月页码锚点模块在N1的定位命中率确定是多少？", "same_time_conflict", [ref("current_a", "2026年6月评估记录：页码锚点模块在章节导航集N1的定位命中率为93.0%。"), ref("current_b", "2026年6月评估复核记录：页码锚点模块在章节导航集N1的定位命中率为91.5%。")], "两份记录冲突，分别为93.0%和91.5%；没有替代声明，不能确定唯一值。", mode="conflict")
    add("映澈具有哪些功能？", "unique_alias", [ref("report_overview", "映澈作品台简称映澈。\n映澈作品台具有证据定位功能。映澈作品台具有实验对比功能。")], "证据定位、实验对比")
    add("页签究竟是哪个模块？", "ambiguous_alias", [], None, mode="clarify")
    add("映澈作品台已经实现自动评分了吗？", "negation", [ref("report_overview", "映澈作品台尚未实现自动评分功能。")], "尚未实现")
    add("跨作品推荐现在已经上线了吗？", "planned", [ref("report_overview", "未来计划加入跨作品推荐功能。")], "文档仅说未来计划，不能确认已经上线。")
    add("对照视图模块依赖页码锚点模块吗？", "parallel_not_dependency", [ref("implementation", "对照视图模块与页码锚点模块独立开发；文中未说明两者相互依赖。")], "没有相互依赖证据，只明确独立开发。")
    add("版面样本集L2与合成网格集G4是同一个数据集吗？", "identity_negation", [ref("evaluation_data", "合成网格集G4与版面样本集L2不是同一数据集。")], "不是")
    add("参考文献中的某研究组就是映澈作品台开发方吗？", "bibliography_not_ownership", [ref("bibliography", "该引文与示例不代表映澈作品台实际采用这些数据或由该组开发。")], "不能由参考文献推出开发方，文档明确不代表该组开发。")
    add("去除页码实验的准确率是多少？", "unanswerable", [], None, mode="abstain")
    add("列切分实验宏平均F1的95%置信区间是多少？", "unanswerable", [], None, mode="abstain")
    add("2027年映澈作品台的正式评分成绩是多少？", "future_unanswerable", [], None, mode="abstain")
    add("映澈作品台总共有多少真实用户？", "unanswerable", [], None, mode="abstain")
    add("另一作者私有实验的访问标记是什么？", "tenant_isolation", [], None, mode="abstain")
    add("制作预算知识库的预算核对标记是什么？", "kb_isolation", [], None, mode="abstain")
    add("制作预算是多少？", "default_scope_positive", [ref("other_space", "映澈作品台制作预算为860元。")], "860元", kb="默认知识库")
    add("另一作者私有实验的访问标记是什么？", "tenant_positive", [ref("private", "另一作者私有实验的访问标记为 AUTHOR_PRIVATE_612。")], "AUTHOR_PRIVATE_612", user=4102)
    add("它在哪个数据集上评估？", "multi_turn", [ref("evaluation_data", "表格规整模块在版面样本集L2上评估。")], "版面样本集L2", history=[{"role": "user", "content": "下面只谈表格规整模块。"}, {"role": "assistant", "content": "好的，继续讨论表格规整模块。"}])
    add("它的定位命中率是哪个数值？", "multi_turn_conflict", [ref("current_a", "2026年6月评估记录：页码锚点模块在章节导航集N1的定位命中率为93.0%。"), ref("current_b", "2026年6月评估复核记录：页码锚点模块在章节导航集N1的定位命中率为91.5%。")], "93.0%与91.5%存在未解决分歧，无法给唯一值。", mode="conflict", history=[{"role": "user", "content": "对照2026年6月两份页码锚点模块评估记录。"}, {"role": "assistant", "content": "可以针对这两份记录提问。"}])
    add("L0今年还用于当前评估吗？", "retired_data", [ref("history", "2026年已停止将L0用于当前评估。")], "依据2026文档，已停止")
    add("2024年的67.0%可以直接与2026年的93.0%比较并宣称提高26个百分点吗？", "invalid_comparison", [ref("history", "历史结果不能直接与新数据的结果比较。"), ref("current_a", "2026年6月评估记录：页码锚点模块在章节导航集N1的定位命中率为93.0%。")], "不能直接比较；历史与新数据评估范围不同，且2026另有冲突记录。")
    assert len(rows) == 32
    return rows


def write(output):
    output.mkdir(parents=True, exist_ok=True)
    source_path = output / "business_holdout_v1.sources.json"
    data_path = output / "business_holdout_v1.jsonl"
    source_path.write_text(json.dumps(documents(), ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    data_path.write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in dataset()), encoding="utf-8")
    manifest = {"dataset_version": VERSION, "gold_review": REVIEW, "human_reviewed": False,
        "as_of": AS_OF, "documents": len(documents()), "questions": len(dataset()),
        "sources_sha256": sha256(source_path.read_bytes()).hexdigest(), "dataset_sha256": sha256(data_path.read_bytes()).hexdigest(),
        "new_retrieval_calls": 0, "new_model_calls": 0, "evaluation_status": "candidate_pending_independent_source_review",
        "not_a_live_accuracy_result": True,
        "coverage": ["long_report", "cross_section", "table_row", "same_time_conflict", "ambiguous_alias", "historical", "multi_turn", "unanswerable", "tenant_isolation", "kb_isolation"],
        "execution_requirements": ["Dedicated isolated schema/collection and users; never ingest into a real user's library.",
            "Long documents require source-span/parent mapping rather than requiring the full document in one parent.",
            "History-bearing cases require a runner that actually supplies history; the existing single-query answer runner is not sufficient.",
            "Numeric and conflict answers require full evidence and claim review; graph edge hit is not answer correctness."]}
    (output / "business_holdout_v1.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evaluation/datasets"))
    print(json.dumps(write(parser.parse_args().output), ensure_ascii=False))
