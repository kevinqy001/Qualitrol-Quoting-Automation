# Step 4 报价文档（Quotation Doc）字段映射与实现计划

> 目标：把标准 Qualitrol Quotation 文档（参考样本 `Gemba Samples/1/1/773306/3. QUOTE/108704-749714.docx`，
> 项目 773306 — Ibri 400kV substation, GCCIA）拆解到字段级，标注每个字段
> **能否由现有 Step 1 / Step 2 产出**，以及 Step 4 落地前还需要补齐的缺口。

---

## 1. 数据流回顾

| 步骤 | 输出 | 关键结构 |
|------|------|---------|
| Step 1 — Extract Info | `step1_extract_info.json` | `documents`、`detected_scenarios`、`extracted_evidence`、`drawing_asset_list`、`structured_requirements` |
| Step 2 — Create BOQ | `step2_create_boq.json` | `product_matching`、`compatibility_flags`、`draft_boq`、`missing_info_questions` |
| Step 3 — Pricing（**未实现**） | — | 单价、折扣、小计、税、总价 |
| Step 4 — Quotation Doc | `.docx` | 套模板 + 填充上述结构（现有 `webapp/docgen.py` 为雏形） |

---

## 2. 文档区块总览

| # | 文档区块 | 状态 | 主要来源 |
|---|---------|:----:|---------|
| 1 | Revision 记录表 | ➖ 元数据 | Step 4 生成时填 |
| 2 | Project Information（end user / tender ref / 地点 / SFDC） | ⚠️ 部分 | Step 1（需新增元数据抽取） |
| 3 | Customer Requirement（报价依据文档清单） | ✅ 可得 | Step 1 `documents` |
| 4 | Important Notes on Offer（范围假设） | ⚠️ 部分 | Step 2 `assumption` + `missing_info_questions`，其余需 LLM/人工 |
| 5 | Schedule of Pricing（各项金额） | ❌ 不可得 | **Step 3 定价层** |
| 6 | Standard T&C（Validity/Delivery/Payment/Warranty/PO） | ➖ 模板 | Step 4 固定模板 |
| 7 | **Attachment 1a 设备清单（标准 BOQ）** | ⚠️ 半可得 | Step 2 `draft_boq`（见第 3 节） |
| 8 | Attachment 1b/2a/2b（Spares/Services/Training） | ❌ 不可得 | Step 3 定价层 + 模板 |
| 9 | 法律条款全文 | ➖ 模板 | Step 4 固定模板 |

图例：✅ 已能产出 ｜ ⚠️ 部分可得/需补 ｜ ❌ 缺失 ｜ ➖ 模板/元数据（无需 Step1/2）

---

## 3. 核心：Attachment 1a 标准 BOQ 表逐列映射

样本 BOQ 行（FMS/PMU/FL Panel 1）：

| QTY | PRODUCT DESCRIPTION |
|-----|---------------------|
| 2 | IDM+ 36A/64D with DFR, DDR, and PMU Functionality / 6U DAU / 32GB CF |
| 1 | FL8-2 with 1 Line Module for monitoring 2 Lines |
| 2 | GPS Antenna & Cable (100m) + amplifier & mounting kit |
| 1 | 8-Way GPS Splitter |
| 1 | Cubicle fully mounted, wired and tested |
| 1 | FACTORY ACCEPTANCE TEST – 3rd PARTY INSPECTION |

| BOQ 表的列 | 状态 | 来源字段 | 说明 |
|-----------|:----:|---------|------|
| QTY（主设备数量） | ✅ | Step 2 `BOQLine.quantity` ← Step 1 `drawing_asset_list` 计数 + `Quantity_Rules` | 主设备数量可从 SLD 资产计数推导 |
| QTY（附属项数量） | ❌ | — | GPS Splitter / Cubicle / FAT 等"每 Panel 固定 1 套"目前无规则 |
| PRODUCT DESCRIPTION | ⚠️ | Step 2 `product_model` + `product_description`(=family_name) | 还原不出带配置的完整描述（"36A/64D … 6U … 32GB CF"），需补 product master 的 description |
| 产品型号 | ✅ | Step 2 `candidate_model` | 前提：data package 该 family 有真实 model，否则为 `_TBD` |
| 关联场景/资产（追溯） | ✅ | Step 2 `scenario_id` / `related_assets` | 不直接进表，用于审核 |
| 单价 / 小计 | ❌ | — | 需 Step 3 |

---

## 4. Step 4 落地前的关键缺口

| 优先级 | 缺口 | 影响 | 建议落点 |
|:----:|------|------|---------|
| P0 | **价格层（Step 3）** | 整份报价金额（单价/小计/Services/Optional/Spares/Training）全缺 | 新增 Step 3，接 `2026-05-12 IP 2026 Price List.xlsx` |
| P1 | **完整产品规格描述** | BOQ 描述还原不到样本精度 | 在 product master 增加/补全 `description` 字段 |
| P1 | **每 Panel 标配附件 BOM** | Cubicle/GPS/Switch/Router/FAT 等附属项缺失 | 新增"family/scenario → 标配附件清单"规则表 |
| P2 | **Panel 分组结构** | 文档按 Panel 分组，Step 2 BOQ 按 family 平铺 | 新增"资产 → Panel"归组映射 |
| P2 | **项目元数据抽取** | end user / tender ref / 地点 / SFDC 缺失 | Step 1 增加元数据抽取字段 |

---

## 5. 现在已能直接喂给 Step 4 的内容

1. ✅ 主设备**数量** — `draft_boq.quantity`（SLD 资产识别 + Quantity Rules）
2. ✅ 主设备**型号 / 产品族** — `candidate_model` / `product_description`
3. ✅ **报价依据文档清单** — Step 1 `documents`
4. ✅ **澄清问题清单** — `missing_info_questions`（`webapp/docgen.py` 已能渲染为 "Open Clarification Questions"）
5. ✅ **场景 / 资产追溯信息** — 供工程师审核

---

## 6. 建议的 Step 4 实现顺序

1. 先做 **Step 3 定价层**（P0），否则文档只有数量没有金额。
2. 补 **product description + 标配附件 BOM 规则**（P1），让 BOQ 描述达到样本精度。
3. 扩展 `webapp/docgen.py`：增加 Project Information、Customer Requirement、Schedule of Pricing、按 Panel 分组的 Attachment 1a，以及 T&C/法律条款模板段。
4. 最后补 **项目元数据抽取 + Panel 分组**（P2）做锦上添花。
