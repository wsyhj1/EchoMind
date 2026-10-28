"""Versioned synthetic corpus and reviewable source-based Gold, never model-generated Gold.

The generated PDF/DOCX files are ingestion test inputs, not presentation artifacts.
All Gold is provisional until a human reviews datasets/rag_v1.jsonl.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent
CORPUS_VERSION = "echomind-synthetic-v1"
KB = "rag_accuracy_v1"
SYSTEMS = [
    ("order", "订单系统", "赤霄", "基础设施甲组", "7200", "连接池耗尽", "限制突发请求，再将连接池上限调整到128，最后检查慢查询"),
    ("payment", "支付系统", "青岚", "支付平台乙组", "1800", "主从切换", "暂停写入，确认新主节点可写，然后恢复流量"),
    ("stock", "库存系统", "白泽", "仓储技术丙组", "3600", "缓存击穿", "启用互斥重建，预热热点键，再恢复正常过期时间"),
    ("shipping", "物流系统", "玄鹤", "履约服务丁组", "900", "内存告警", "停止大键写入，清理临时键，随后评估扩容"),
]


def corpus():
    documents = []
    for key, system, cluster, team, ttl, issue, procedure in SYSTEMS:
        for kind, text in (
            ("architecture", f"{system}架构说明 当前版本2026。{system}使用{cluster}缓存集群缓存业务状态，缓存有效期为{ttl}秒。{system}依赖星河审计服务记录变更。该集群是独立实例，不能用Redis技术概念代替。"),
            ("ownership", f"缓存职责登记 当前版本2026。{cluster}缓存集群由{team}维护。{team}的升级联系人是值班协调员，值班电话没有在本文登记。此登记仅适用于{cluster}实例。"),
            ("runbook", f"实例故障手册 当前版本2026。{cluster}缓存集群的典型故障是{issue}。遇到{issue}时，处理步骤为：{procedure}。执行前必须备份实例配置。不得把本流程直接用于其他集群。"),
        ):
            doc_id = f"{key}_{kind}"
            documents.append({"doc_id": doc_id, "user_id": 1001, "knowledge_base_id": KB,
                              "format": "pdf" if kind == "ownership" else "docx", "text": text})
    documents += [
        {"doc_id": "history", "user_id": 1001, "knowledge_base_id": KB, "format": "pdf",
         "text": "历史变更记录。2024年赤霄缓存集群由旧运维组维护，该职责已在2025年终止。当前维护团队以2026年缓存职责登记为准。赤霄缓存集群计划在2028年迁移到新平台，该计划尚未执行。"},
        {"doc_id": "decoy", "user_id": 1001, "knowledge_base_id": KB, "format": "docx",
         "text": "培训环境说明。订单演练系统使用赤霄演练缓存，由培训支持组维护。赤霄演练缓存与赤霄缓存集群是两个不同实例，演练环境缓存有效期为60秒。培训支持组不维护生产的赤霄缓存集群。"},
        {"doc_id": "private", "user_id": 1002, "knowledge_base_id": KB, "format": "docx",
         "text": "隔离测试专用。订单系统使用赤霄缓存集群。租户乙的秘密标记是 TENANT_B_ONLY_791。该租户的维护团队是隐蔽团队。"},
        {"doc_id": "other_kb", "user_id": 1001, "knowledge_base_id": "rag_accuracy_other", "format": "pdf",
         "text": "其他知识库专用。审计系统的独立标记是 OTHER_KB_ONLY_381。审计系统由合规维护组负责。"},
    ]
    return documents


def dataset():
    docs = {doc["doc_id"]: doc for doc in corpus()}
    rows = []

    def add(query, category, refs, answer, *, user_id=1001, kb=KB, answerable=True):
        rows.append({"question_id": f"Q{len(rows)+1:03d}", "query": query, "user_id": user_id,
                     "knowledge_base_id": kb, "category": category, "gold_answer": answer,
                     "gold_sources": [{"doc_id": ref, "quote": docs[ref]["text"]} for ref in refs],
                     "answerable": answerable, "corpus_version": CORPUS_VERSION,
                     "gold_review": "pending_human_review"})

    for key, system, cluster, team, ttl, issue, procedure in SYSTEMS:
        for query, ref, answer in (
            (f"{system}使用哪个缓存集群？", "architecture", cluster),
            (f"{system}的缓存有效期是多少秒？", "architecture", ttl),
            (f"{cluster}缓存集群由谁维护？", "ownership", team),
            (f"{cluster}缓存集群的典型故障是什么？", "runbook", issue),
            (f"{cluster}处理故障前必须做什么？", "runbook", "备份实例配置"),
        ):
            add(query, "single_fact", [f"{key}_{ref}"], answer)
    for key, system, cluster, team, ttl, issue, procedure in SYSTEMS:
        for query in (f"{system}使用的缓存由哪个团队维护？", f"谁负责{system}依赖的缓存实例？",
                      f"{system}缓存出问题应找哪个维护组？", f"查找{system}到缓存维护团队的关系链。",
                      f"{system}的生产缓存归哪个团队管？"):
            add(query, "multi_hop", [f"{key}_architecture", f"{key}_ownership"], team)
    for key, system, cluster, team, ttl, issue, procedure in SYSTEMS:
        for query, refs, answer in (
            (f"{system}的缓存谁维护，{issue}怎么处理？", ["architecture", "ownership", "runbook"], f"{team}；{procedure}"),
            (f"{system}缓存多久过期，谁负责？", ["architecture", "ownership"], f"{ttl}秒；{team}"),
            (f"{system}缓存集群叫什么，典型故障是什么？", ["architecture", "runbook"], f"{cluster}；{issue}"),
            (f"{cluster}由谁维护，处理故障前做什么？", ["ownership", "runbook"], f"{team}；备份实例配置"),
            (f"说明{system}的缓存名称、维护组和排障步骤。", ["architecture", "ownership", "runbook"], f"{cluster}；{team}；{procedure}"),
        ):
            add(query, "multi_evidence", [f"{key}_{r}" for r in refs], answer)
    for i in range(15):
        key, system, cluster, team, ttl, issue, procedure = SYSTEMS[i % 4]
        query = (f"{system}缓存的保鲜时长是多少？", f"{cluster}这个实例谁来管？",
                 f"{cluster}遇到{issue}有哪些操作？", f"{system}把业务状态缓存在什么地方？")[i // 4]
        ref = ("architecture", "ownership", "runbook", "architecture")[i // 4]
        answer = (ttl, team, procedure, cluster)[i // 4]
        add(query, "ambiguous", [f"{key}_{ref}"], answer)
    distractors = [
        ("生产订单系统的缓存是培训组维护的吗？", ["order_architecture", "order_ownership", "decoy"], "不是，生产缓存由基础设施甲组维护"),
        ("赤霄演练缓存由谁维护？", ["decoy"], "培训支持组"),
        ("订单演练系统的缓存有效期？", ["decoy"], "60秒"),
        ("生产订单系统缓存有效期也是60秒吗？", ["order_architecture", "decoy"], "不是，是7200秒"),
        ("2026年赤霄缓存还由旧运维组维护吗？", ["history", "order_ownership"], "不是，基础设施甲组"),
        ("2024年赤霄缓存集群由谁维护？", ["history"], "旧运维组"),
        ("2028年的迁移计划现在执行了吗？", ["history"], "尚未执行"),
        ("培训支持组维护赤霄生产缓存吗？", ["decoy", "order_ownership"], "不维护"),
        ("赤霄演练缓存与赤霄缓存集群是同一实例吗？", ["decoy"], "不是"),
        ("赤霄缓存的旧职责在哪一年终止？", ["history"], "2025年"),
    ]
    for query, refs, answer in distractors:
        add(query, "distractor", refs, answer)
    for query in ["2029年赤霄维护负责人是谁？", "基础设施甲组的值班电话号码？", "青岚缓存实例IP是什么？",
                  "白泽实例的访问密码？", "玄鹤缓存的月度费用？", "星河审计服务的SLA数字？",
                  "订单系统的数据库版本？", "支付系统明年维护团队是谁？", "2028年的迁移已完成的验收编号？",
                  "旧运维组2026年的负责人手机号？"]:
        add(query, "unanswerable", [], None, answerable=False)
    add("租户乙的秘密标记是什么？", "isolation", [], None, answerable=False)
    add("其他知识库中审计系统的独立标记？", "isolation", [], None, answerable=False)
    add("审计系统由谁负责？", "isolation", ["other_kb"], "合规维护组", kb="默认知识库")
    add("租户乙的秘密标记是什么？", "isolation", ["private"], "TENANT_B_ONLY_791", user_id=1002)
    add("审计系统的独立标记？", "isolation", ["other_kb"], "OTHER_KB_ONLY_381", kb="rag_accuracy_other")
    assert len(rows) == 100
    return rows


def write_docx(path, text):
    """Minimal deterministic Word test input consumed by the real Docx2txtLoader."""
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = ZipInfo(name, date_time=(2026, 10, 21, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8"))


def build_files(output):
    import fitz
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for doc in corpus():
        path = output / f"{doc['doc_id']}.{doc['format']}"
        if doc["format"] == "docx":
            write_docx(path, doc["text"])
        else:
            pdf = fitz.open()
            page = pdf.new_page()
            remaining = page.insert_textbox(fitz.Rect(45, 45, 550, 780), doc["text"], fontname="china-s", fontsize=12)
            if remaining < 0:
                raise ValueError("PDF fixture overflow")
            pdf.save(path, no_new_id=True)
            pdf.close()
        manifest.append({**doc, "path": str(path.resolve()), "file_hash": sha256(path.read_bytes()).hexdigest()})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime/rag_accuracy/fixtures"))
    args = parser.parse_args()
    folder = ROOT / "datasets"
    folder.mkdir(exist_ok=True)
    rows = dataset()
    (folder / "rag_v1.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    # Stratified smoke: contains every category, including all isolation scopes.
    ids = [1, 6, 11, 16, 21, 26, 31, 36, 41, 46, 61, 76, 80, 86, 87, 96, 97, 98, 99, 100]
    (folder / "rag_smoke_v1.jsonl").write_text("".join(json.dumps(rows[i-1], ensure_ascii=False) + "\n" for i in ids), encoding="utf-8")
    manifest = build_files(args.output)
    print(f"Generated {len(manifest)} ingestion files, 100 draft Gold cases, 20 smoke cases; human review pending.")


if __name__ == "__main__":
    main()
