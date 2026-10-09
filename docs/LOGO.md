# TokenShare 徽标

主徽标是纯图形图标：三股卖家配额流（绿 / Monad 紫 / 琥珀）汇入一枚 `>_` 终端枢纽——先验证、再转发，呼应整站终端美学。图标无文字，64px 头像尺寸仍可辨认。

## 文件清单

| 文件 | 尺寸 | 大小 | 用途 |
|---|---|---|---|
| [assets/tokenshare-logo.png](assets/tokenshare-logo.png) | 1024×1024 PNG，不透明深色底 `#0b0f15` | ~22.5 KB | **主徽标，直接上传**（≤2MB、≥500px、≤400 万像素均满足；圆形裁切安全，所有元素距边 ≥10%） |
| [assets/tokenshare-logo.svg](assets/tokenshare-logo.svg) | 矢量母版，viewBox 1024×1024 | ~1.0 KB | 可编辑源文件（纯几何图形，无字体依赖） |
| [assets/tokenshare-logo-light.png](assets/tokenshare-logo-light.png) | 1024×1024 PNG，不透明浅色底 `#f2f5f7` | ~22.0 KB | 浅色背景场景（流线/描边已加深保证对比度） |
| [assets/tokenshare-logo-light.svg](assets/tokenshare-logo-light.svg) | 矢量 | ~0.8 KB | 浅色版源文件 |
| [assets/tokenshare-logo-wordmark.png](assets/tokenshare-logo-wordmark.png) | 1024×1024 PNG，深色底 | ~22.6 KB | 图标 + TOKENSHARE 词标（Menlo 粗体），用于文档头图/横幅 |
| [assets/tokenshare-logo-wordmark.svg](assets/tokenshare-logo-wordmark.svg) | 矢量 | ~1.0 KB | 词标版源文件 |
| [assets/tokenshare-logo-preview-128.png](assets/tokenshare-logo-preview-128.png) | 128×128 PNG | ~3.8 KB | 头像尺寸实际像素预览 |
| [assets/tokenshare-logo-preview-64.png](assets/tokenshare-logo-preview-64.png) | 64×64 PNG | ~1.9 KB | 最小头像实际像素预览 |
| [assets/tokenshare-illustration.svg](assets/tokenshare-illustration.svg) / [.png](assets/tokenshare-illustration.png) / [-light.png](assets/tokenshare-illustration-light.png) | 1024×1024 | ~314–317 KB | **架构示意图，非徽标**：带 TEE 边界/文字标注的机制讲解图，仅适合正文插图 |

## 设计含义

- **三色流线 → 单点汇入**：多个独立卖家把官方套餐密钥登记进同一个中继。
- **`>_` 终端枢纽**：中继本体——每次调用先过链上 `isValid` 校验再转发；光标块表示在线执行。
- 无硬币、无机器人、无盾牌；画的是配额汇流与中继，不是币。
- 配色取自 `web/styles.css`：`#0b0f15` 底、`#3ddc97` 信号绿、`#9d8cff` Monad 紫、`#e8b44a` 琥珀、面板 `#111a26`。

## 意象与安全声明（重要）

徽标与架构示意图中的"密钥汇入中继""TEE 边界""单一出口"等均为**设计意象**，用于传达产品机制，**不是安全证明，也不是绝对保证**。请注意：

- TEE（Phala TDX）用于**降低**运营者接触卖家密钥的风险，但**不能消除**该风险；信任假设与残余风险以 [policy 页](../web/policy.html) 和代码为准，不以徽标图形为准。
- 架构示意图（`tokenshare-illustration*`）中的 "TEE RELAY · KEYS SEALED" 等文字是对机制的简化标注，不构成对密钥不可达性的承诺。
- 主徽标本身不含任何文字，不做任何安全声明。

## 使用建议

- 上传/头像/favicon：`tokenshare-logo.png`（深色）。圆形头像裁切已验证安全（图标主体位于中心内接圆内）。
- 浅色页面/打印：`tokenshare-logo-light.png`。
- README/文档头图：`tokenshare-logo-wordmark.png`。
- 改形改色：编辑对应 SVG 后本地重渲染（见下）。

## 来源与重渲染

徽标为手工编写的 SVG，本地用 Chrome headless 截图渲染为 PNG，再用 sips 缩放生成预览；**未使用任何 AI 图像生成服务**（无 gpt-image 等），未安装新依赖。

```sh
# 重渲染（wrap.html：内联 SVG 的最小 HTML，body margin:0）
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --force-device-scale-factor=1 \
  --window-size=1024,1024 --screenshot=out.png wrap.html
# 预览尺寸
sips -Z 128 out.png --out preview-128.png
sips -Z 64  out.png --out preview-64.png
```
