# charset-normalizer 决策链追踪（从字节到 `best()`）

- 版本：仓库 `src/charset_normalizer`（包版本 3.5.1，纯 Python 后端；编辑安装，无 C 扩展）
- 范围：`from_bytes()`（`src/charset_normalizer/api.py:43`）→ `mess_ratio()`（`src/charset_normalizer/md.py:875`）
  → `coherence_ratio()` / `merge_coherence_ratios()`（`src/charset_normalizer/cd.py:383`, `cd.py:313`）
  → `CharsetMatch` / `CharsetMatches`（`src/charset_normalizer/models.py:11`, `models.py:243`）
- 本文不使用“置信度”一词：源码里没有 confidence 概念。真实比较量只有两个：
  `chaos = mean_mess_ratio`（`models.py:165`）与 `coherence = self._languages[0][1]`（`models.py:172`）。
- 所有数字均可由 `uv run python analysis/diagnose.py` 复现；fixture 全部位于
  `analysis/fixtures/`，无任何网络下载。
- 排序细节（`__lt__` 的 0.005/0.02 阈值、同分时的多字节用量）属于**实现现状**，
  在 docstring/类型契约中只承诺“默认从最可能到较不可能排序”（`models.py:245`），
  不构成跨版本稳定契约。

## 0. 端到端流水线（函数级）

1. **采样参数确定**（`api.py:115`）：
   - `length <= chunk_size*steps`（默认 512*5=2560）→ `steps=1; chunk_size=length`，整段一次性检查。
   - 否则若 `length/steps < chunk_size`，缩小 `chunk_size=int(length/steps)`（`api.py:120`）。
   - `< TOO_SMALL_SEQUENCE=32`（`constant.py:22`）仅影响日志；`>= TOO_BIG_SEQUENCE=10_000_000`
     （`constant.py:23`）开启 lazy 解码。
2. **声明提示**：`any_specified_encoding(seq)`（`utils.py:191`）只在前 8192 字节用 ASCII 忽略解码，
   正则 `RE_POSSIBLE_ENCODING_INDICATION`（`constant.py:803`，匹配 `encoding|charset|coding` +
   分隔符 + 名称）提取，并经 `_IANA_NAMES` 归一化。提示只决定候选测试顺序，不会被当作定论。
3. **BOM/SIG 提示**：`identify_sig_or_bom()`（`utils.py:264`）按 `ENCODING_MARKS`（`constant.py`，
   含 utf_8/utf_7 四个签名/gb18030/utf_32/utf_16）做前缀匹配。
4. **候选测试顺序**（`api.py:184`）：
   `[sig_encoding?, declared?, "ascii", "utf_8"] + IANA_SUPPORTED_MB_FIRST`。
   `IANA_SUPPORTED_MB_FIRST`（`api.py:38`）= 全部 IANA 编码稳定排序为“多字节优先，单字节按字母序在后”。
   `tested` 集合去重；`cp_isolation/cp_exclusion` 在 `api.py:187` 过滤。
5. **逐候选**：
   - 无 BOM 时 `utf_16/utf_32/utf_7` 直接跳过（`api.py:218`,`api.py:228`）。
   - `soft_failure_skip`：与已软败编码 ≥80% 相似的编码在解码前跳过（`api.py:240`，
     相似表 `IANA_SUPPORTED_SIMILAR`，由 `cp_similarity()` 离线生成）。
   - 整段/惰性解码：单字节且非大文件时 **deferred decoding**——先只解码 chunk 做探测，
     通过后才整段解码（`api.py:300` 附近 `deferred_decoding`）；多字节先整段严格解码，
     失败即 `UnicodeDecodeError` → **hard failure**（`api.py:356`）。
   - chunk 由 `cut_sequence_chunks()`（`utils.py:363`）产出；偏移为
     `range(sig_len if bom else 0, length, length//steps)`。
   - 每个 chunk 调 `mess_ratio(chunk, threshold)`；`md_ratios[-1] >= threshold` 则
     `early_stop_count += 1`；一旦 `early_stop_count >= max_chunk_gave_up`
     （`max(len(r_)/4, 2)`）或“有 BOM 但不剥离”的特殊情形，立刻 break（`api.py:411`）。
   - `mean_mess_ratio = sum(md_ratios)/len(md_ratios)`（`api.py:423`）。
   - **软败**：`mean >= threshold or early_stop_count >= max_chunk_gave_up`
     （`api.py:449`）→ 进 `tested_but_soft_failure`，登记相似跳过，必要时准备
     ascii/utf_8/指定编码/utf_16/utf_32 的 fallback（`api.py:467`），`continue`。
   - 通过后单字节才补做整段解码（`api.py:494`），再对同一批 `md_chunks` 做
     `coherence_ratio(chunk, language_threshold, lg_inclusion)`（`api.py:540`，ascii 跳过 CD）。
   - `merge_coherence_ratios(cd_ratios)` 按语言对各 chunk 分数取算术平均并降序排序。
   - 构造 `CharsetMatch` 后 `results.append()`（`api.py:562`）：fingerprint+chaos 相同则
     变成已有候选的 submatch，否则成为顶层候选（`models.py:330`）。
6. **提前返回**（多种，见第 5 节）；否则遍历完所有编码后必要时使用 fallback（`api.py:626`），
   最终 `results.best()` 只是排序后的第 0 个（`models.py:344`）。

## 1. `mess_ratio` 内部：两级“提前中断”与值的完整性

`mess_ratio(seq, maximum_threshold)`（`md.py:875`）按块步进：`<511→32`、`<1024→64`、否则 `128`
字符（`md.py:884`）。每喂完一个块，把 10 个插件的 `ratio` 求和（`md.py:969`）：

- `TooManySymbolOrPunctuationPlugin`（门限 0.30，符号权重 2）、`TooManyAccentuatedPlugin`
  （需 ≥8 字符，门限 0.35）、`UnprintablePlugin`（不可打印权重 8；遇 ESC 直接 1.0）、
  `SuspiciousDuplicateAccentPlugin`、`SuspiciousRange`（需 >13 字符，
  `2*可疑相邻数/字符数`）、`SuperWeirdWordPlugin`（坏词字符占比；`_invalid_word_count>0` 直接 1.0）、
  `CjkUncommonPlugin`、`SuspiciousKatakanaPlugin`、`ArchaicUpperLowerPlugin`、`ArabicIsolatedFormPlugin`。
- **块内提前中断**：块和 `>= maximum_threshold` 立即 `break`，返回该前缀块值并 `round(...,3)`
  （`md.py:972`）。这个值不是完整 chunk 的 mess——它是扫描中断时的**前缀值**。
- 纯 ASCII 快速路径（`decoded_sequence.isascii()`）只喂 unprintable/weird-word/punct 三个
  必然相关的检测器（`md.py:952`）。

关键结构性质（本文用插件分子/分母推导，并用诊断脚本验证）：所有插件 ratio 都是
“前缀计数 ÷ 当前长度”，块检查点的分母只增不减，因此**块内中断得到的 reported 值必然
≥ 完整扫描值**（不存在“中断时显得更干净、完整扫描反而更脏”的方向）。例如
`SuspiciousRange` 的分子是固定事件数、分母持续增大：把 3 个 `a€`（Basic Latin vs
Currency Symbols，`is_suspiciously_successive_range` 为 True）放在 chunk 最前面、
后面全是普通文本时，32 字符检查点 ≈0.312 即中断，而 512 字符完整值只有 0.035。

> 这把用户问题“部分值是否进入跨候选比较”拆成两个不同层次，答案不同：
> - **chunk 级提前退出**（gave_up 达上限）：候选直接软败，根本不创建 `CharsetMatch`，
>   其部分 `mean_mess_ratio` **不进入**任何跨候选排序。
> - **chunk 内块级中断但候选存活**：该 chunk 的部分值被纳入 `mean_mess_ratio`，
>   候选照常创建，该部分值**会进入** `__lt__` 跨候选比较。默认 threshold=0.2 下要构造
>   “单块中断但整候选存活”的情形可行（1/5 块脏，其余干净，见 D2）；由于部分值只高估，
>   放宽 threshold 更容易观察（D3 用 threshold=0.35）。
> - fallback 是唯一例外：当**所有**候选都失败时，ascii/utf_8/指定编码会以
>   `CharsetMatch(..., threshold, ...)` 造一个 chaos **恒等于 threshold=0.2** 的占位结果
>   （`api.py:478`），它确实出现在最终 `CharsetMatches` 中（F1），但这不是“部分实测值
>   参与比较”，而是固定常量。

## 2. coherence：每个 chunk 怎么算、怎么合并

- `coherence_ratio(seq, threshold=0.1, lg_inclusion)`（`cd.py:383`）：
  1. `alpha_unicode_split()` 按 Unicode range 把字母分“层”（`cd.py:255`，层间用
     `is_suspiciously_successive_range` 判断是否同层）；
  2. 层字符数 `<= TOO_SMALL_SEQUENCE(32)` 直接跳过（`cd.py:419`）——这是短文本没有语言结果
     的真实原因，与 `from_bytes` 的 32 字节日志阈值是同一个常量但不同用途；
  3. 单字节候选只在 `encoding_languages(enc)` 推断的语族内选语言（`cd.py:72`；
     纯拉丁代码页推断为 `["Latin Based"]` 时不限制语言）；多字节用
     `mb_encoding_languages()`（`cd.py:94`，按编码名前缀映射 日/中/韩）；
  4. `characters_popularity_compare()`（`cd.py:144`）比较字符频次排名，返回 0..1；
     `>=0.8` 计一次 sufficient，同层凑满 3 个高分语言即停止追加（`cd.py:437`）；
  5. `filter_alt_coherence_matches()`（`cd.py:346`）合并 “English” 与 “English—” 之类替代名。
- **合并**：`merge_coherence_ratios(list_of_per_chunk_results)`（`cd.py:313`）对同一语言在
  各 chunk 的分数做**简单算术平均**（`round(...,4)`），再降序排序。不是取最大值，也没有加权。
- `CharsetMatch.coherence` 只取合并列表第 0 项的分数；语言列表为空则 coherence=0
  （`models.py:170`）。`language` 属性在无语言结果时按编码反推（ascii→English，
  单语族编码→该语言，否则 Unknown，`models.py:139`）。

诊断输出的 G1 用西班牙语 cp1252（378 字节，steps=1，只有 1 个 chunk）展示了
“每 chunk 列表 → 合并列表”的形状（单 chunk 时平均值就是自身，Spanish 0.84）；
G2 用同一批字节的 3000 字节版本展示真正的多 chunk 合并：5 个 chunk 的 Spanish 分数
`[0.72, 0.80, 0.80, 0.72, 0.84]` 算术平均为 0.776，与 D2 中 cp1252 候选的 coherence 一致。

## 3. fingerprint 去重到底保留什么

`CharsetMatches.append()`（`models.py:330`）在 payload `< TOO_BIG_SEQUENCE` 时，
对每个已有顶层候选比较 `match.fingerprint == item.fingerprint and match.chaos == item.chaos`
（`models.py:335`）。`fingerprint = hash(str(self))`（`models.py:362`），即**解码后 str 的
Python 哈希**（进程内一致，跨进程盐值不同——实现现状）。

命中时调 `match.add_submatch(item)`（`models.py:96`）：

- 顶层候选保留：**先出现者的编码名**（顶层身份不变）、它自己的 chaos/coherence/languages/
  bom 标志、`alphabets`（按需对其 `str(self)` 做 `unicode_range` 聚合，`models.py:214`）、
  解码 payload（submatch 的 `_string` 被置 `None` 释放内存，`models.py:104`）。
- 被吸收者保留：自己的 `_encoding`（原名）、自己的 chaos/coherence/languages 与 bom 标志；
  出现在顶层的 `submatch` 列表与 `could_be_from_charset`（`models.py:198`）。
- `encoding_aliases` 是**实时**从标准库 `encodings.aliases` 反查的（`models.py:113`），
  与 dedup 无关：查任何别名（如 `r["latin1"]`）都会命中同一个顶层对象
  （`__getitem__` 同时查顶层名与所有 leaf 名，`models.py:283`）。
- 大文件（≥10 MB）**完全关闭** submatch 归并（`models.py:333`），同 payload 也各自成顶层结果。
- `__eq__` 同时比较编码名与 fingerprint（`models.py:44`），所以“同名不同 payload”不算重复。

E1（法语 cp1252 文本，只用各编码映射相同的字符）：cp1252/cp1254/cp1258/iso8859_15/
iso8859_9/latin_1 解码出**逐字符相同的 str**，且 chaos 都为 0，最终只剩 1 个顶层候选
cp1252（它在 IANA 字母序中最先被测到），其余 5 个成为 submatch；顶层 coherence=0.92、
language=French、alphabets=`['Basic Latin','Latin-1 Supplement']`，leaf 自带各自的
languages 列表但 `_string is None`。

## 4. 三类最小输入的实际决策记录

下表数字来自诊断脚本（仓库内 fixture，hex 头见 `analysis/fixtures/` 与第 6 节）。
`cp_isolation` 只做候选过滤，不改变任何阈值/排序/合并规则（过滤在循环最前面，`api.py:187`）。

### A. 纯 ASCII / 大部分 ASCII

**A1 `ascii_plain.txt`（65 字节，hex 头 `546865207175...`）**
- 无 BOM、无声明；候选建立顺序 ascii → utf_8 → 其余。
- 65 ≤ 2560 ⇒ `steps=1, chunk_size=65`，只检查 **1 个 chunk**。
- ascii 整段解码成功；纯 ASCII chunk 的 mess=0.0（快速路径）。
- ascii 是优先编码之一且 mess==0.0 ⇒ 命中 `api.py:600` 的即时返回
  （`if mean_mess_ratio == 0.0: return CharsetMatches([current_match])`）。
  **utf_8 及其余 98 个编码根本不会被测**。CD 对 ascii 跳过（`api.py:524`），coherence=0，
  `language` 由反推得 "English"。
- `<32` 字节时同样在 ascii 处返回（如 `b"Hi there!"`），区别只是打
  “tiny portion”TRACE 日志；空字节走专门分支返回 utf_8/chaos=0（`api.py:91`）。

### B. 两个单字节编码都能严格解码，coherence 不同

**B1 `spanish_cp1252.bin`（378 字节西班牙语，cp_isolation=[cp1252, cp1257]）**
- 1 个 chunk（378≤2560）；两者都是单字节、deferred 解码，均无 hard/soft failure。
- mess 均为 0.0；两者都跑 CD：cp1252 合并后 `[('Spanish',0.84),('Portuguese',0.72),...]`，
  cp1257 为 `[('Spanish',0.80),('Portuguese',0.68),...]`（ñ 在两种代码页都映射成合法字母，
  但重音字母排名分布略偏，故语言分数不同；payload 也不同，fingerprint 不同，不会 dedup）。
- 排序（`__lt__`，下节详述）：`|chaos 差|=0 < 0.005` 且 coherence 差 0.04 > 0.02
  ⇒ 按 coherence 降序，cp1252 在前。`best()` = cp1252。
- B2（不隔离）展示同一输入在完整工作流中的情况：ascii/utf_8 hard fail，多字节编码多数
  hard fail；cp1250/cp1252/iso8859_10 等都以 chaos=0 建候选，其中若干因 fingerprint 相同
  被 dedup；最终 `best()` 是 cp1250（与 cp1252 chaos、coherence 完全相同，按
  multi-byte-usage 再到**稳定排序的插入序**决胜——实现现状，见第 5 节）。
  这也说明：“真实编码是 cp1252”不等于检测结果是 cp1252；检测器只保证输出符合规则的最靠前候选。

### C. UTF 家族：BOM 与内嵌声明

**C1 `utf8_bom.bin`：`efbbbf` + UTF-8 文本（32 字节）**
- `identify_sig_or_bom` → `("utf_8", b"\xef\xbb\xbf")`，插入候选最前；`should_strip_sig_or_bom`
  对 utf_8 为 True（`utils.py:275`，只排除 utf_16/utf_32）⇒ 解码前**剥掉 3 字节 BOM**，
  chunk 偏移也从 `len(sig_payload)=3` 开始。
- utf_8 mess=0.0 ⇒ `api.py:600` 即时返回；`bom=True`，`str(match)` 不含 `\ufeff`。

**C2 `utf16be_bom.bin`：`feff` + UTF-16-BE（32 字节）**
- 识别为 `utf_16`；`should_strip_sig_or_bom("utf_16")=False` ⇒ **不剥 BOM 字节**，
  Python 解码器自己消费 BOM；chunk 从 2 开始，且 `bom_or_sig_available and not strip`
  使 chunk 循环在首个 chunk 后立即 break（`api.py:413`）。
- `len(decoded)<length` 触发 multi_byte_bonus（`api.py:388`，仅日志/排序用）；
  末尾 `encoding_iana == sig_encoding` 的分支（`api.py:615`）也会直接返回 utf_16。
  `str(match)` 无 BOM 字符（由解码器去除），alphabets 仅 Basic Latin。

**C3 `decl_iso8859_5.bin`：XML 头声明 ISO-8859-5 + 西里尔文本（98 字节，无 BOM）**
- `any_specified_encoding` 在前 8192 字节 ASCII 忽略解码后用正则抓到 `ISO-8859-5`
  → 归一化 `iso8859_5`，放到优先级最前（BOM 位置之后）。
- 注意 ascii/utf_8 仍在优先级列表里，但都 hard fail；iso8859_5 严格解码成功、mess=0.0
  ⇒ `api.py:600` 即时返回。即“声明优先但不盲从”：若声明的编码解码失败或 chaos>0，
  流程不会在它身上提前结束（coherence 这里是 Ukrainian 0.625——语言检测不受声明影响，
  只按字符频次排名）。
- meta charset 形式（`<meta charset="iso-8859-7">`）走完全相同的正则路径。

**其他提示位**：UTF-7 SIG（`+/v8-` 等四种，`constant.py` ENCODING_MARKS）被识别时
走“整段解码后删首字符 `\ufeff`”的专门分支（`api.py:327`，issue #716/#718），
因为按原始字节切 SIG 会破坏 base64 边界；无 SIG 时 utf_7 不测试（`api.py:228`）。
gb18030 有 4 字节 SIG（`84319533`）。utf_16/utf_32 无 BOM 时只以 `_be/_le` 子编码出现，
裸 `utf_16/utf_32` 被跳过（`api.py:218`）。

### D. 提前退出专题（fixture 均为 3000 字节，5 个采样 chunk，偏移 0/600/1200/1800/2400）

**D1 `chunks_early_exit.bin`：chunk 级 gave-up 提前退出，部分均值不参与比较**
- 构造：清洁法语文本中，在偏移 600 与 1200 处各植入 5 个 `,\x80. ` 单元（共 20 字节/块）。
  0x80 在 latin_1 是 C1 控制符（`UnprintablePlugin` 权重 8，且邻接标点使
  SuspiciousRange 复位）；在 cp1252 是 €。
- cp1252：5 个 chunk mess 全 0.0 ⇒ 候选存活，chaos=0。
- latin_1：chunk 值 `[0, 0.625(块内中断；完整值仅 0.078), 0.625(同)]`，第 4、5 个 chunk **从未被消费**；
  `max_chunk_gave_up=max(5//4,2)=2`，第 2 个脏块后生成器循环立即 break（实测只检查 3 个 chunk），
  soft failure。**该候选没有 CharsetMatch，任何 chunk 值都不进排序**；最终只剩 cp1252。

**D2 `intra_chunk_partial.bin`：chunk 内块级中断的部分值随存活候选进入排序（默认阈值）**
- 构造：西班牙语文本最前 6 字节替换为 `a\x80`×3（Basic Latin↔Currency 可疑相邻 ×3，
  紧跟清洁文本稀释）。
- threshold=0.2、cp_isolation=[latin_1, cp1252]：
  - cp1252 chunk1 reported=0.279（32 字符块即中断），完整值仅 0.034；
    5 块均值 0.0558 < 0.2，gave_up=1 < 2 ⇒ **存活**。
  - latin_1 chunk1 reported=0.375（同位置 C1 控制符更重），完整值 0.047；均值 0.075 ⇒ 存活。
  - 两者都是“由 chunk 内中断产生的部分 mess 值”被写进 `CharsetMatch.chaos` 并参与
    `__lt__`：cp1252 chaos 0.0558 < latin_1 0.075 ⇒ cp1252 排前。
- **D3 同字节 threshold=0.35**：块内中断点后移，cp1252 chunk1 变为完整 0.034
  （检查点 0.312<0.35，扫描跑完全块），latin_1 仍在 0.375 处中断；最终 chaos 变为
  0.0068 vs 0.075。对照 D2/D3 可以直接看到“同一份输入，仅仅改阈值，候选 chaos 值由
  部分值变成完整值”，而两者在这两种情况下都进入跨候选比较。

**F1 `boxdraw_utf8.bin`（U+2500..U+257F 重复，UTF-8，3840 字节）：fallback 占位**
- utf_8 严格解码成功但每 chunk mess=2.0（符号/超界插件）⇒ 软败；没有任何编码成为正常候选。
- fallback 分支（`api.py:626`）因 `fallback_u8` 存在，造 `chaos=threshold=0.2` 的 utf_8
  `CharsetMatch` 返回；其 coherence=0、language=Unknown。**这是 0.2 常量，不是实测均值**，
  且只有在“结果集为空”的兜底路径中才出现。

## 5. 排序比较链、提前退出与 `best()` 的关系（实现现状）

### 5.1 `CharsetMatch.__lt__` 的真实比较链（`models.py:53`）

设 `chaos_difference=abs(self.chaos-other.chaos)`、
`coherence_difference=abs(self.coherence-other.coherence)`：

1. `chaos_difference < 0.005` 且 `coherence_difference > 0.02`
   ⇒ coherence 高者排前（`self.coherence > other.coherence`）。
2. `chaos_difference < 0.005` 且 `coherence_difference <= 0.02`
   ⇒ 视为难分：大文件（payload ≥ `TOO_BIG_SEQUENCE`）比 chaos；
   否则比 `multi_byte_usage = 1 - len(str)/len(raw)`（`models.py:78`，多字节“压缩率”高者前）。
3. 其他情况（chaos 差 ≥0.005）⇒ **只比 chaos**，coherence 完全不参与
   （`self.chaos < other.chaos`）。

注意三点实现现状：

- coherence 只在“chaos 几乎相同”时才起作用；差 0.005 的 chaos 就足以压过任意 coherence 差距。
- 第 2 档的 multi-byte-usage 对单字节候选恒为 0，比较结果为 False（`self<other` 与
  `other<self` 都 False）时，`sorted()`（Timsort，稳定）保持**插入顺序**，即
  `IANA_SUPPORTED_MB_FIRST` 的测试先后。B2 中 cp1250 赢 cp1252 就是这种稳定排序产物，
  不是“cp1250 更可信”。
- `CharsetMatches` 在构造时 `sorted()` 一次、`append` 标脏、迭代/`best()` 时再排序
  （`models.py:255`,`models.py:341`）；`best()` 严格等价于 `matches[0]`（`models.py:344`）。
  即 `best()` 不做任何额外判断，只是上述比较链排序后的第一项。

### 5.2 提前退出点（按代码顺序）

| 位置 | 条件 | 返回内容 |
|---|---|---|
| `api.py:91` | 空字节 | 单个 utf_8 / chaos=0 / bom=False |
| `api.py:600` | 当前候选 ∈ {指定编码, ascii, utf_8} 且 `chaos==0.0` | **单候选** `CharsetMatches([m])`，立即结束 |
| `api.py:611` | early_stop_results 非空、ascii/utf_8/指定编码均已测 | early_stop_results 中 chaos<0.1 的最佳者（单候选） |
| `api.py:615` | 当前候选 == BOM/SIG 编码（无论 chaos） | 单候选立即返回（utf_16/utf_32/utf_8-sig/utf_7/gb18030 SIG 走此路径） |
| `api.py:411` | 单候选 gave_up 达 `max(len(chunks)/4, 2)` | 仅中断该候选的 chunk 循环，软败后继续测下一个编码 |
| `md.py:972` | chunk 内块和 ≥ threshold | 仅中断该 chunk 的字符扫描，返回前缀部分值 |
| `api.py:626` | 全部编码测完仍无正常结果 | 仅使用 ascii/utf_8/指定编码的 fallback（chaos 固定 threshold） |

优化性提前跳过（不产生结果、只减少工作量）：`definitive_match_found`（单字节候选
coherence≥0.5 后跳过不相关语族，`api.py:259`；同族 SB 成功计数 ≥7 后跳过尾部，
`api.py:284`）、`mb_definitive_match_found`（非 UTF 多字节且解码长度 <98% 原文后跳过所有
单字节，`api.py:296`）、`soft_failure_skip`（相似代码页连带跳过）、ascii/utf_8 命中 0.0 时
根本不会遍历到这些优化点。这些都不改变已创建候选的 chaos/coherence/fingerprint，
因此不影响 `best()` 的选择规则，只影响“哪些编码会被测到”。

## 6. 采样、大文件、SIG/BOM 剥离与 explain

- **小文件**（≤2560 字节，默认参数）：`steps=1, chunk_size=length`，单个 chunk 覆盖整段；
  多字节代码页还要先做整段严格解码。32 字节以下只是日志不同。
- **长文件**（>2560 且 <10 MB）：5 个 chunk、步长 `length//5`（如 3000 字节 →
  偏移 0/600/1200/1800/2400，每块 512）；单字节 deferred 解码，通过探测后才整段解码。
- **超大文件**（≥10 MB，诊断 F2，11 MB 运行时合成、不入库）：
  - 单字节编码只先解码前 `50e4` 字节做合法性验证（`api.py:316`），chunk 仍按 5×512 抽取；
  - 通过 chunk 探测后再验证 `sequences[50e3:]` 的剩余部分（`api.py:428`）；
  - payload 只为 {指定编码, ascii, utf_8} 保留（`api.py:553`），且 submatch 归并被关闭
    （`models.py:333`）；大文件 tie-break 改为比 chaos 而非 multi-byte-usage（`models.py:66`）。
- **SIG/BOM 剥离**（`should_strip_sig_or_bom`，`utils.py:275`）：
  - utf_8/gb18030/utf_7：剥离（utf_7 特殊：整段解码后删首字符）；
  - utf_16/utf_32：**保留 BOM 字节**交给解码器，chunk 循环首块即停；
  - 无论剥离与否，`CharsetMatch.bom` 都为 True，且 `str(match)` 都不含 `\ufeff`。
- **explain / logging**：`explain=True` 只在 logger 上挂/摘一个 `StreamHandler` 并把级别调到
  TRACE=5（`api.py:83` 与每个返回点的摘处理器代码）。它唯一触及计算的地方是
  `mess_ratio(chunk, threshold, explain and 1<=len(cp_isolation)<=2)` 的 `debug` 参数
  （`api.py:403`），而 `debug` 只增加日志输出（`md.py:998` 之后），不改变任何分支与数值。
  诊断 G/测试断言 `explain=True/False` 结果的 `(encoding, chaos, coherence)` 序列完全一致。
  外部自行 `logging.getLogger("charset_normalizer").setLevel(...)` 同理。

## 7. 复现方式

```bash
uv sync --group dev
uv run python analysis/diagnose.py            # 人类可读报告
uv run python analysis/diagnose.py --json analysis/trace.json --huge
uv run pytest -q tests/test_decision_trace.py
```

- 检测只调用公开 API：`from_bytes`（diagnose.py 内还只读地调用公开工具
  `identify_sig_or_bom / any_specified_encoding / should_strip_sig_or_bom` 与
  `cd.coherence_ratio / cd.merge_coherence_ratios` 展示合并输入）。
- 受控 instrumentation 仅在 `analysis/trace_tools.py` 中**包装** `cut_sequence_chunks`
  与 `mess_ratio`：参数与返回值原样透传，只额外用同一 chunk 以 `threshold=1e9` 再算一次
  完整值，并挂一个 logging handler 解析 hard/soft failure 日志；运行后恢复原引用。
- Fixture（仓库内，小文件）：

| 文件 | 字节数 | hex 头 | 预期分支 |
|---|---:|---|---|
| `fixtures/ascii_plain.txt` | 65 | `54686520717569636b...` | ascii chaos=0 即时返回（A1） |
| `fixtures/spanish_cp1252.bin` | 378 | `45737061f161206573...` | 双候选 chaos=0，coherence 0.84 vs 0.80（B1） |
| `fixtures/utf8_bom.bin` | 32 | `efbbbf636166c3a9...` | SIG 识别+剥离，utf_8 即时返回（C1） |
| `fixtures/utf16be_bom.bin` | 32 | `feff00480065006c...` | BOM 保留给解码器，首 chunk 即返回 utf_16（C2） |
| `fixtures/decl_iso8859_5.bin` | 98 | `3c3f786d6c207665...` | 正则声明提示 iso8859_5，0.0 即时返回（C3） |
| `fixtures/chunks_early_exit.bin` | 3000 | `4d6f6c69e8726520...` | latin_1 消费 3/5 chunk 后 gave_up=2 软败、部分均值不入排序（D1） |
| `fixtures/intra_chunk_partial.bin` | 3000 | `6180618061802065...` | chunk 内部分值 0.279/0.375 随候选入排序（D2/D3） |
| `fixtures/french_shared_cp1252.bin` | 326 | `4c61204672616e63...` | 6 编码同 payload，dedup 为 cp1252+5 submatch（E1） |
| `fixtures/boxdraw_utf8.bin` | 3840 | `e29480e29481e294...` | 全软败后 utf_8 fallback chaos=0.2（F1） |

11 MB 超大输入由 `trace_tools.huge_payload()` 用同一法语句型运行时拼出（F2），
避免向仓库提交大文件。

## 8. 明确的边界：哪些是公开契约，哪些是实现现状

- **公开行为**（docstring/类型可依赖）：空输入返回 utf_8；无结果时可给 ascii/utf_8/声明编码
  fallback；`best()` 是排序第一；同名/别名可从 `CharsetMatches` 取出；BOM 检测与剥离的总原则
  （utf_16/utf_32 例外）；`explain` 仅为调试输出。
- **实现现状（勿当契约）**：`__lt__` 的 0.005/0.02 常数与 multi-byte-usage 决胜；
  Timsort 稳定序导致的同分时字母序/测试序偏好；`fingerprint=hash(str)`（受哈希随机化影响，
  仅保证同进程内 dedup 正确）；`definitive`/mb-definitive/POST_DEFINITIVE_SB_CAP=7 等性能
  优化的具体阈值；各 mess 插件权重与门限；10 个 mess 插件直接相加可以超过 1.0（实测
  出现过 1.37/2.75/8.0 等 chaos 值）；IANA 列表内容随 Python 版本变化。
