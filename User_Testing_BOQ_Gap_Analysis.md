# 用户测试 BOQ 差距分析 / User Testing BOQ Gap Analysis

> 测试案例 / Cases：**766481**（只有 SLD 图纸）、**775368**（New Habshan 400/220kV）、**776060**（KIZAD-B1 132/11kV）
> 对比 / Compared：AI 生成的 BOQ vs 业务同事标注的真实 BOQ 及批注

---

## 一、关键结论：这三次为什么没做对 / Key Points: Why the Three Cases Failed

**关键点 1 — AI 只会"列产品清单"，不会"做工程配置"。**
**Key Point 1 — The AI produces a "product list," not an "engineered configuration."**

- 中文：业务真实的 BOQ 是一份按"间隔 → 盘柜 → 设备 → 配件 → 系统机柜 → 服务"搭出来的可报价配置单；而 AI 只给了一张"某类产品 × 多少个"的汇总表。就算个别数字接近，结构上也没法直接拿去报价。
- EN: The real BOQ is a quotable configuration built up as *bay → panel → device → accessories → system cabinet → services*. The AI only produced a "product category × quantity" summary. Even where a number is close, the structure can't be used to quote.

**关键点 2 — 数量算错了，还选错设备、报了不属于 Qualitrol 供货范围的东西。**
**Key Point 2 — Quantities are wrong, it picked the wrong devices, and it quoted items outside Qualitrol's scope of supply.**

- 中文：AI 把图纸上数出来的回路标签直接当成产品数量（严重偏多），又把"整个项目只需要 1 套"的软件/服务器当成按台数计算；还报了**不属于 Qualitrol 供货范围**的设备（GIS 局放监测随 GIS 包提供、变压器测温是变压器本体标配附件），业务批注"不需要"。根本原因是 AI 只看到项目里有 GIS、有变压器，就想当然地要为它们报监测设备，缺乏对"谁供什么、供货范围如何划分"的理解。
- EN: The AI used raw drawing labels directly as product quantities (far too high), and treated software/servers that are "one per project" as if scaled per device. It also quoted items **outside Qualitrol's scope of supply** (GIS partial-discharge monitoring comes with the GIS package; transformer temperature is a standard transformer-body accessory), which the reviewer marked "not required." The root cause: seeing that the project has GIS and transformers, the AI assumed Qualitrol must quote monitoring for them — with no understanding of scope-of-supply boundaries (who supplies what).

**关键点 3 — 报价里最大的一块（配套件和服务）几乎没算，图纸没文字就直接卡住。**
**Key Point 3 — The largest part of the quote (accessories & services) is mostly missing, and image-only drawings break it.**

- 中文：真实 BOQ 里占比最大的是盘柜、GPS、交换机、防火墙、工控机、软件授权、以及安装调试培训备件等服务——这些 AI 基本没生成。而 766481 因为图纸是"扫描图片、没有文字"，AI 读不出任何内容，直接没结果。
- EN: The biggest share of a real BOQ is cabinets, GPS, switches, firewalls, industrial PCs, software licenses, and services (installation/commissioning/training/spares) — which the AI barely generated. And 766481 failed entirely because its drawings are scanned images with no text for the AI to read.

---

## 二、三个案例分别发生了什么

| 案例 | 客户给的资料 | AI 的结果 |
|---|---|---|
| **766481** | 只有 2 张 SLD 图纸，没有说明书 | **没生成有效 BOQ** |
| **775368** | 上百页招标说明书 + 1 张 SLD | 出了 BOQ，但产品和数量都不对 |
| **776060** | 上百页招标说明书 + 3 张 SLD | 出了 BOQ，但没区分电压等级，产品和数量都不对 |

补充：766481 的图纸经确认是"扫描/图片型 PDF"，里面没有任何可读文字；另外两个项目的说明书内容很丰富，明确写了 FMS / PQM / PMU 等应用。

---

## 三、AI 的 BOQ 和真实 BOQ 差在哪

**1. 做出来的东西不是一个层级。**
真实 BOQ 是按盘柜一块块搭出来的：每个间隔配一台采集单元（DAU），标清楚通道规格，再配上机柜、系统机柜和服务。AI 只给了"某类产品一共多少个"的粗汇总。

**2. 选错了设备。**
这类变电站监测项目里，FMS（故障录波）、PQM（电能质量）、PMU（相量测量）基本都是用同一类硬件（IDM+ / Informa 采集单元）加不同软件授权来实现的。AI 却把它们拆成了好几种不同产品，还报了不属于 Qualitrol 供货范围的 GIS 局放、变压器测温设备——这些在项目里确实存在，但分别由 GIS 包和变压器本体附件提供，不该由 Qualitrol 报价。

**3. 数量算错。**
- AI 把图纸上数出来的回路标签数直接当数量（比如 40、60），实际应该看"真正需要监测的间隔数"（真实是十几台）。
- 主站软件（iQ+）应该"整个项目 1 套"，AI 却按台数报了十几套。

**4. 没有区分电压等级和功能。**
776060 真实 BOQ 是分开做的：132kV 和 11kV 各自的 FMS / PQM / PMU 分别配盘。AI 把它们混成了一张表，业务明确批注"没有分电压等级""没有单独区分 FMS"。

**5. 漏掉了报价里最重要的一大部分。**
真实 BOQ 里数量最多、金额占比也大的，是这些 AI 没生成的内容：
- 各类盘柜（FMS 盘、PQM 盘、系统机柜 LEV/PDC）
- 每台设备的配件：测试开关、GPS 天线、交换机、防火墙
- 系统机柜里的：工控机、杀毒/白名单/备份软件、显示器、打印机等
- 软件授权（PMU / WAMS License）
- 服务：工厂验收、现场调试、通信联调、网络安全、送电配合、培训、备件

**6. 图纸没文字就卡住。**
AI 目前主要靠"读文字"来判断项目内容。766481 的图纸是扫描图片、没有文字层，所以什么都读不出来，最终没有结果。

---

## 四、根本原因（一句话版）

AI 现在是把一个"需要工程师按间隔逐个配置、共用同一类硬件、还包含大量盘柜/网络/软件/服务的复杂工程报价"，简单当成了"识别产品类型 + 数个数"。所以问题不在"算得准不准"，而在"理解得对不对"。

---

## 五、下一步怎么改进（按优先级）

**优先做（价值最大）**
1. 用这几份真实 BOQ 沉淀出一套"配置模板"：让 AI 知道每种应用该配哪种采集单元、按间隔怎么配、还要带哪些标准配件和服务。
2. 把数量的依据从"数图纸标签"改成"数真正要监测的间隔"，并且分电压等级来算；同时把"整个项目 1 套"的软件/服务器单独归类。

**其次做**
3. BOQ 输出按"电压等级 → 功能（FMS/PQM/PMU）→ 盘柜"分块呈现，贴近业务真实格式。
4. 把配件、网络设备、软件授权、服务（含天数估算）也纳入自动生成，而不是留空。

**同步补短板**
5. 对"只有图纸、且图纸是扫描图片"的情况，让 AI 用识图能力去读，读不出时至少给出"需要补充资料"的提示，而不是直接空结果。

---

## 六、一句话总结

当前 demo 更像一个"产品识别器"，还不是一个"报价配置器"。要真正能用，关键是把业务真实的配置经验（配什么、配多少、还要带哪些配套和服务）教给它，并解决纯图纸读不出内容的问题。

---

## Appendix — How the "% confidence" is calculated

The percentage on each BOQ line is a **product match score**: how sure the tool is that the identified application/product applies to this project. It is a heuristic (rule + keyword based), **not a statistical probability**, and it is capped at 95%. It is built up as follows:

1. **Base score (scenario detection):** driven by how *specific* the matched keyword is in the documents — a precise/controlled term scores higher (~0.6+), a generic word (e.g. "relay") scores lower (~0.45).
2. **Multiple-evidence bonus:** small bonus when several independent sentences point to the same scenario (up to +0.10).
3. **Asset corroboration bonus:** additional ~+0.12 when the physical asset is also confirmed in the text (e.g. "GIS", "transformer" actually appears).
4. Capped at 0.97. If the **LLM review** step runs, its own confidence overrides the rule-based value.
5. When converted into a product line, it is **capped at 0.95** (hence many lines show 95%); if the product model/capability is still TBD it is capped lower (~0.6).

So **95% = a strongly corroborated scenario hitting the 0.95 ceiling; 94% = the scenario confidence landed at 0.94.**

> ⚠️ Important: confidence reflects *whether the scenario/product applies*, **not whether the quantity is correct** — a line can be 95% confident yet still have the wrong quantity (as seen in these test cases).

---

## 附录：How the "% confidence" is calculated

The percentage shown on each product line is its **match score** — essentially "how sure we are that this application scenario / product applies to this project", **capped at 95%**. It is built up step by step:

1. **Base score (Step 1 scenario detection):** based on how *specific* the matched keyword is in the documents — a precise controlled term scores higher (~0.6), a generic word (e.g. "relay") scores lower (~0.45).
2. **Multiple-evidence bonus:** small increase when several independent sentences point to the same scenario (up to +0.10).
3. **Asset corroboration bonus:** ~+0.12 if the physical asset (e.g. "GIS", "transformer") actually appears in the text.
4. Steps 1–3 are capped at 0.97; **if the LLM review step runs, its own confidence overrides the rule-based score.**
5. **When turned into a product line, it is capped at 0.95** (hence many lines show 95%); if the product model/capability is still TBD, it is capped lower (~0.6).

So **95% = a strongly corroborated scenario hitting the 0.95 ceiling; 94% = the scenario confidence came out at 0.94.**

> ⚠️ Note: this is a **heuristic confidence** (keyword strength + rules), not a statistical probability. It reflects whether the *scenario/product* applies — it does **not** mean the *quantity* is correct.
