# AstrBot Discord Game Monitor Plugin

🎮 **主動監控 Discord 使用者的實時遊戲狀態，透過 AI Persona 生成符合人設的互動回覆。**

[English](#english) | [繁體中文](#繁體中文)

---

## 繁體中文

### 📋 功能特性

- **實時遊戲監控**：透過 Discord Gateway，獲取指定用戶的實時 Presence 狀態
- **AI 驅動回覆**：將遊戲信息交由 AstrBot 的 AI Persona 進行符合人設的互動回覆
- **網路搜尋增強**（可選）：啟用時，LLM 可利用網路搜尋結果補充遊戲資訊
- **零硬編碼**：所有配置均可透過 WebUI 自訂，完全符合開源標準
- **完整日誌**：詳細的日誌輸出，便於除錯和監控

### 🚀 快速開始

#### 1. 克隆並安裝插件

```bash
# 克隆到 AstrBot 的插件目錄
cd AstrBot/data/plugins
git clone https://github.com/<your-account>/astrbot-plugin-discord-game-monitor.git
cd astrbot-plugin-discord-game-monitor

# 安裝依賴
pip install -r requirements.txt
```

#### 2. 在 AstrBot WebUI 配置插件

進入 AstrBot WebUI → **插件管理** → 找到 **Discord Game Monitor** → **配置**

填寫以下欄位：

| 配置項 | 說明 | 格式 |
|--------|------|------|
| **Discord Bot Token** | 用於查詢用戶狀態的 Bot Token（需要 Identify & Presence Intent） | `[your_bot_token]` |
| **目標伺服器 ID (Guild ID)** | 目標用戶所在的 Discord 伺服器 ID | `[target_guild_id]` |
| **目標使用者 ID** | 要監控的用戶的 Discord ID | `[target_discord_id]` |
| **關心訊息發送頻道 ID（可選）** | 自動觸發關心時主動發送到指定頻道 | `[target_channel_id]` |
| **啟用網路搜尋提示** | 若啟用，LLM 可利用網路搜尋補充遊戲資訊 | `false` |

#### 🔍 如何獲取這些值？

##### Discord Bot Token

參考 [Discord 官方開發者文檔](https://discord.com/developers/applications)

1. 進入 Discord Developer Portal
2. 點擊「New Application」建立應用
3. 左側選單 → **Bot** → **Add Bot**
4. 在「TOKEN」區域點擊「Copy」
5. ⚠️ 妥善保管 Token，勿在公開代碼或文檔中暴露

**Bot 權限設定**：
- 在 Developer Portal 的「OAuth2」→「Scopes」中勾選 `bot`
- 在「Bot Permissions」中勾選：
  - ✅ `Read Messages/View Channels`
  - ✅ `Read Message History`
  - ✅ `Send Messages`（可選，若插件需要主動發言）

**Intents 配置**（需在 Discord Developer Portal 開啟，且 AstrBot Discord 客戶端必須啟用）：
- Identify / Members Intent（讀取成員信息）
- Presence Intent（讀取用戶在線狀態和活動）

##### Guild ID（伺服器 ID）

1. 在 Discord 中啟用「開發者模式」
   - 進入 **User Settings** → **Advanced** → 開啟 **Developer Mode**
2. 在要監控的伺服器中**右擊伺服器名稱**
3. 點擊「Copy Server ID」
4. 將複製的 ID 粘貼到配置中

##### Target Discord ID（目標用戶 ID）

1. 在 Discord 中啟用「開發者模式」（同上）
2. **右擊要監控的用戶**（可以是消息、用戶卡片等）
3. 點擊「Copy User ID」
4. 將複製的 ID 粘貼到配置中

**⚠️ 重要**：目標用戶必須在你指定的伺服器中

#### 3. 使用指令

在 Discord 中發送指令：

```
/查遊戲
```

**範例回覆**（取決於當前掛載的 Persona）：

*如果用戶正在玩 Elden Ring：*
> "哦，又是那個讓人奮鬥的遊戲啊，加油！"

*如果用戶正在玩 Minecraft：*
> "創造下一個像素藝術傑作嗎？"

---

### 🔧 技術架構

```
┌─ AstrBot Main Process (Discord Client)
│
├─ Event: /查遊戲 command
│
├─ GameMonitorPlugin.query_game_status()
│   ├─ 從 AstrMessageEvent 提取底層 Discord Client
│   │   (避免 WebSocket 衝突，複用已有連線)
│   │
│   ├─ guild.get_member(target_discord_id)
│   │   └─ 讀取 member.activities[0].name
│   │
│   ├─ 組裝中立 Prompt（無硬編碼角色名稱）
│   │   例："The user is currently playing {game_name}..."
│   │
│   └─ await context.llm_generate(prompt=...)
│       └─ 當前 Persona 自然生成個性化回覆
│
└─ yield event.plain_result(response)
```

### 📝 配置檔案說明

#### `metadata.yaml`
- 插件元數據：名稱、版本、作者、支持平台等
- 聲明最低 AstrBot 版本 >= 4.16

#### `_conf_schema.json`
- 定義 WebUI 中可配置的欄位
- 支持默認值、提示信息、可見性控制

#### `requirements.txt`
- `discord.py>=2.0.0`：Discord Bot 庫（AstrBot 依賴）

### 🛡️ 錯誤處理與日誌

插件提供完整的錯誤處理和日誌記錄：

```python
logger.info("[GameMonitor] 查詢到遊戲：Elden Ring，準備調用 LLM...")
logger.error("[GameMonitor] 無法獲取 Discord 客戶端，請確保插件運行在 Discord 平台上。")
logger.warning("[GameMonitor] Discord Bot Token 未配置，部分功能將不可用。")
```

### ⚠️ 重要注意事項

1. **不建立新的 Discord 連線**
   - 插件複用 AstrBot 主程序的 Discord Client
   - 避免 WebSocket 衝突導致主程序斷線

2. **Presence 必須在 Guild 上下文中**
   - 配置中須同時提供 `target_guild_id` 和 `target_discord_id`
   - Discord API 限制：用戶的 Presence 狀態只在伺服器中可見

3. **Bot 權限需求**
   - `Identify Intent`：讀取用戶基本信息
   - `Presence Intent`：讀取用戶在線狀態和活動

### 🔒 私隱與資料範圍

- 本插件只監控你指定的 `target_discord_id`（可搭配 `target_guild_id`）。
- 主要保存的是遊戲狀態與時間戳（例如 current_game、last_presence_event、game_duration_minutes），存放在本地 `data/state.json`。
- 不會主動上傳 Discord 訊息內容到第三方服務；只有在觸發 LLM 生成功能時，才會把必要提示詞送到你已配置的模型供應商。
- 若啟用網路搜尋，會依 AstrBot 的 `provider_settings.web_search` 與供應商能力執行搜尋，請自行評估所使用供應商的隱私政策。

### 🧪 常見故障排查

#### 狀態看起來已連線，但 `last_discord_update` / `last_presence_event` 一直是 `null`

這通常表示「有連線但沒有收到 Presence 事件」，常見原因：

1. `target_discord_id` 填錯（或不是同一個伺服器的成員）
2. `target_guild_id` 填錯
3. Discord Bot 未開啟 `Presence Intent`
4. AstrBot Discord 客戶端未啟用 `intents.presences`

可用 `/遊戲監控診斷` 檢查 `client_intents.presences` 是否為 `True`。

#### 啟動時出現 429 / `error code: 30034`（每日 Application Command 建立上限）

這是 Discord 指令註冊配額限制，與 Presence 事件本身不同層級：

- 影響：Slash 指令同步可能失敗
- 不代表：Presence Gateway 一定不可用

若你在密集熱重載測試，容易觸發此限制。建議先停止頻繁重新註冊指令，待配額恢復後再同步。

#### 啟用網搜後回覆出現 `<tool_code>...</tool_code>`

若看到這類輸出，表示模型在「描述工具呼叫」，而不是實際執行工具。

- 強制測試指令路徑（`/遊戲監控強制認知`）在啟用 `use_web_search` 時會走 `tool_loop_agent`，可實際調用 AstrBot 原生工具鏈。
- 自動 Presence 觸發路徑若缺少當前事件上下文，會回退到一般 LLM 呼叫，不保證能執行工具。
- 請同時確認 AstrBot 的 `provider_settings.web_search=true`，且模型供應商支援 `tool_use`。

### ⚠️ 已知問題（Known Issues）

1. 當人格（Persona）由其他插件接管或保管時，本插件的認知提示與回覆語氣可能無法完整聯動。
2. 目前預設網搜來源偏向 Google/Bing 類結果，評論語氣較正式；若能補充 bilibili / X(Twitter) / Steam 的玩家評論來源，遊戲關心內容會更接地氣。

4. **零硬編碼承諾**
   - 代碼中沒有任何特定的 User ID、Guild ID 或角色名稱寫死
   - 所有參數均透過配置 Schema 管理

### 📖 開發指南

#### 調試本地插件

```bash
# 在 AstrBot 主程序環境中
cd AstrBot
python -m astrbot run

# 在 WebUI 插件管理中點擊「重載插件」進行熱重載
```

#### 代碼格式化（必須）

你的插件在發佈到 GitHub 前，**必須** 使用 [ruff](https://docs.astral.sh/ruff/) 進行格式化。

##### 安裝 ruff

```bash
pip install ruff
```

##### 格式化代碼

```bash
# 格式化 main.py（檢查並修復）
cd /Users/jackson/game_monitor
ruff check --fix main.py

# 一鍵格式化整個插件目錄
ruff check --fix .
```

##### 檢查格式問題（不修復）

```bash
ruff check .
```

**官方要求**：任何提交前都應該執行 `ruff check --fix .` 確保代碼符合 AstrBot 編碼規範。

#### 修改 Prompt 模板

編輯 `main.py` 中的 `_assemble_casual_prompt()` 方法：

```python
prompt_variants = [
    f"Variant 1: You are observing {game_name}...",
    f"Variant 2: Someone mentioned {game_name}...",
    # 自訂更多變體
]
```

#### 擴展功能

- 添加更多 Activity 類型支持（如 Streaming、Listening）
- 集成遊戲信息 API（如 IGDB、Steam API）
- 實現用戶遊戲時間統計

---

### 🚀 發佈檢查清單

在將插件發佈到 GitHub 前，請確保完成以下檢查：

- [ ] 代碼已使用 `ruff check --fix .` 格式化
- [ ] metadata.yaml 已填寫完整（name, version, author, support_platforms, astrbot_version）
- [ ] display_name 已設定（用於 WebUI 展示）
- [ ] 所有敏感信息（Token、ID）已移至配置 Schema，代碼中無硬編碼
- [ ] requirements.txt 列出了所有依賴
- [ ] 已在本地 AstrBot 實例中測試過 `/查遊戲` 指令
- [ ] README.md 完整且清晰
- [ ] .gitignore 已配置，確保敏感文件不被上傳
- [ ] 代碼有充分的中文/英文註解
- [ ] 錯誤處理完整，無未捕捉的異常

---

## English

### 📋 Features

- **Real-time Game Monitoring**: Fetch Discord user Presence status via Discord Gateway
- **AI-Powered Responses**: Let AstrBot's AI Persona generate persona-aligned replies
- **Web Search Enhancement** (Optional): Enable LLM to incorporate game information from web search
- **Zero Hardcoding**: All configs are user-customizable via WebUI, following open-source standards
- **Comprehensive Logging**: Detailed logs for debugging and monitoring

### 🚀 Quick Start

#### 1. Clone and Install

```bash
cd AstrBot/data/plugins
git clone https://github.com/<your-account>/astrbot-plugin-discord-game-monitor.git
cd astrbot-plugin-discord-game-monitor
pip install -r requirements.txt
```

#### 2. Configure in AstrBot WebUI

Navigate to **Plugin Management** → **Discord Game Monitor** → **Configure**

| Field | Description | Example |
|-------|-------------|---------|
| **Discord Bot Token** | Bot token with Identify & Presence Intent | `MTk4NjIyNDgzMjExNjc3NzI4.Cl...` |
| **Target Guild ID** | Discord server ID where the target user is | `606020001144635424` |
| **Target User ID** | Discord ID of the user to monitor | `349211305196126221` |
| **Enable Web Search Hint** | Allow LLM to use web search for game info | `false` |

#### 3. Usage

In Discord, send:

```
/查遊戲
```

Example responses depend on your configured Persona.

### 📄 License

This project is open-source and available under the MIT License.

### 🤝 Contributing

Contributions are welcome! Please feel free to submit PRs or open issues for bug reports and feature requests.

---

**Created with ❤️ for AstrBot Community**
