中文 | [English](README.en.md)

# Edge 历史记录迁移到 Chrome

这个工具把 Microsoft Edge 导出的浏览记录迁移到 macOS 上的 Google Chrome，同时避开对 Chrome `History` 数据库的直接修改。

工具先生成一个临时 Firefox 资料。Chrome 使用内置 Firefox 导入器读取该资料，再通过自己的历史服务写入访问记录。

## 支持范围

第一版读取包含以下列名的 CSV：

```text
DateTime,NavigatedToUrl,PageTitle
```

`DateTime` 必须包含 ISO 8601 时区，例如 `2026-08-30T10:00:00Z`。

工具导入 `http://` 和 `https://` 访问记录。第一版不处理密码、Cookie、书签、自动填充、地址或支付资料。

## 环境要求

- macOS
- Python 3.9 或更高版本
- Google Chrome
- 使用上述列名的 Microsoft Edge 历史 CSV

我们已在 macOS 上使用 Microsoft Edge 152 和 Google Chrome 152 验证这条流程。

## 使用方法

### 1. 从 Edge 导出历史记录 CSV

1. 在 Edge 地址栏打开 `edge://history/all`。
2. 点击历史记录页面顶部的**导出浏览数据**。
3. 将 Edge 生成的 CSV 保存到本机。下面的命令示例使用 `~/Downloads/BrowserHistory.csv`。

### 2. 使用本仓库准备临时资料

克隆仓库，然后把 CSV 路径传给 `prepare`：

```bash
git clone https://github.com/JJasonSun/edge-history-to-chrome.git
cd edge-history-to-chrome
python3 edge_history_to_chrome.py prepare ~/Downloads/BrowserHistory.csv
```

如需在导入前检查记录数量和数据库状态，可运行 `status`。这个命令不会打印网址：

```bash
python3 edge_history_to_chrome.py status
```

### 3. 在 Chrome 中导入

在 Chrome 地址栏打开 `chrome://settings/importData`，选择 Mozilla Firefox，只勾选**浏览记录**，然后导入一次。若 Chrome 列出多个 Firefox 资料，请选择名称以 **Edge History To Chrome** 开头并带有本次运行编号的资料。Chrome 的历史页面可能需要等待片刻才会刷新。

### 4. 清理临时资料

确认 Chrome 已显示记录后，清理临时资料：

```bash
python3 edge_history_to_chrome.py cleanup
```

再次处理另一个文件前，请先运行 `cleanup`。重复导入同一个资料可能生成重复记录。

## 从 Chrome 迁移到 Edge

Edge 已内置 Chrome 数据导入功能，反向迁移不需要本仓库：

1. 在 Edge 地址栏打开 `edge://settings/profiles/importBrowsingData`。
2. 找到**从 Google Chrome 导入数据**，点击**导入**。
3. 选择 Chrome 资料和浏览记录等需要迁移的数据，然后开始导入。

微软也在[官方支持文档](https://support.microsoft.com/en-us/microsoft-edge/what-s-imported-to-microsoft-edge-ab7d9fa1-4586-23ce-8116-e46f44987ac2)中说明了这个入口。

## 安全设计

程序只在本机运行，不发起网络请求，也不会修改 Chrome 的资料数据库。源 CSV 保持不变。

`prepare` 在 `~/Library/Application Support/Firefox/` 下创建临时资料。若该位置已有 `profiles.ini`，程序先复制原文件，再追加一个资料段。`cleanup` 只移除工具添加的内容；若恢复操作可能覆盖后续改动，程序会停止并保留备份。

程序会在创建历史数据库前记录运行状态。若 `prepare` 或 `cleanup` 中断，可以再次运行 `cleanup`。资料名称、状态文件、符号链接或目录内容不符合预期时，清理命令会停止。

生成的 `places.sqlite` 包含网址、标题和访问时间。请把它视作私密数据，不要把数据库或真实 CSV 上传到 GitHub Issue。

运行 `prepare` 或 `cleanup` 前请退出 Firefox，避免 Firefox 同时修改 `profiles.ini`。

## 限制

- Chrome 的 Firefox 导入器把时间截断到整秒。
- Chrome 152 的本地历史后端会清理超过约 90 天的记录，因此较早的导入记录可能无法持续显示。
- Chrome 可能在后续版本修改内部导入格式。
- 用户需要在 Chrome 内部设置页完成一次手动导入。
- 工具只支持上文列出的 Edge CSV 列名。

## 技术原理

Chrome 152 会连接 Firefox 的 `moz_places` 和 `moz_historyvisits`，再把结果交给自己的历史服务。相关实现位于 Chromium 的 [`firefox_importer.cc`](https://github.com/chromium/chromium/blob/152.0.7977.65/chrome/utility/importer/firefox_importer.cc#L173-L220)。Chrome 通过 `profiles.ini` 发现 Firefox 资料，macOS 路径定义在 [`firefox_importer_utils_mac.mm`](https://github.com/chromium/chromium/blob/152.0.7977.65/chrome/common/importer/firefox_importer_utils_mac.mm#L10-L20)。

仓库使用独立编写的兼容实现，不包含 Chromium 或 Firefox 源代码。Google、Microsoft 和 Mozilla 未赞助或背书本项目。

## 开发

运行标准库测试：

```bash
python3 -m unittest discover -s tests -v
```

测试只使用临时目录和匿名网址，不读取已安装浏览器的资料。

## 许可证

[MIT](LICENSE)
