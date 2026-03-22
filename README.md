# ◈ SIGNAL_CORE — AI 合約信號站

一個純前端的加密貨幣合約分析工具，整合 Binance API + Claude AI，自動分析 BTC / ETH / SOL 並寄送交易信號到你的信箱。

## ✨ 功能

- 📊 **即時 K線數據** — 從 Binance 公開 API 取得，無需 API Key
- 🤖 **Claude AI 分析** — 計算 RSI、MACD、布林帶、EMA、ATR 等指標後送 Claude 分析
- 📬 **自動寄信通知** — 透過 EmailJS 將做多/做空建議寄到你的信箱
- ⏰ **定時自動掃描** — 開啟後每小時自動執行一次分析
- 🎨 **終端機風格 UI** — 深色賽博龐克風格介面

## 🚀 部署到 GitHub Pages

```bash
# 1. 建立 GitHub repository
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/你的帳號/signal-core.git
git push -u origin main

# 2. 開啟 GitHub Pages
# Settings → Pages → Source: Deploy from a branch → Branch: main / (root)
```

部署後透過 `https://你的帳號.github.io/signal-core/` 訪問

## 🔧 設定說明

### 1. Claude API Key
- 前往 https://console.anthropic.com 取得 API Key
- 格式：`sk-ant-api03-...`

### 2. EmailJS 設定（免費寄信）

1. 前往 https://www.emailjs.com 註冊（免費方案每月 200 封）
2. **Add Email Service** → 選擇 Gmail / Outlook 等
3. **Email Templates** → 建立新範本，內容設定：
   ```
   Subject: {{subject}}
   Body: {{message}}
   To: {{to_email}}
   ```
4. 取得三個值填入介面：
   - **Service ID**：Email Services 頁面
   - **Template ID**：Email Templates 頁面  
   - **Public Key**：Account → API Keys

### 3. 時間框架選擇
| 時間框架 | 適合策略 |
|---------|---------|
| 15m | 短線 / 日內交易 |
| 1h | 波段交易（推薦） |
| 4h | 中線趨勢跟蹤 |
| 1d | 長線持倉 |

## 📋 信號說明

| 信號 | 說明 |
|------|------|
| ▲ LONG | 看多，考慮做多 |
| ▼ SHORT | 看空，考慮做空 |
| ◆ NEUTRAL | 訊號不明，建議觀望 |

信心指數 ≥ 70% 才建議進場

## ⚠️ 風險聲明

本工具僅供參考，不構成投資建議。合約交易風險極高，請自行評估風險並做好資金管理。

## 🛠 技術架構

```
Binance Public API (K線數據)
    ↓
技術指標計算 (RSI/MACD/BB/EMA/ATR)
    ↓
Claude API (claude-sonnet-4-20250514)
    ↓
解析 JSON 信號 → 渲染介面
    ↓
EmailJS → 寄信通知
```

所有運算在瀏覽器本地執行，無後端伺服器，隱私安全。
